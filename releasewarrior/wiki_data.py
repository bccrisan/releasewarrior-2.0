import json

import os
from copy import deepcopy

from git import Repo
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from releasewarrior.click_input import generate_inflight_task_from_input
from releasewarrior.click_input import generate_prereq_task_from_input
from releasewarrior.click_input import generate_inflight_issue_from_input
from releasewarrior.collections import Release
from releasewarrior.git import commit
from releasewarrior.helpers import get_branch, load_json, get_remaining_items


def order_data(data):
    # order prereqs by deadline
    prereqs = data["preflight"]["human_tasks"]
    data["preflight"]["human_tasks"] = sorted(prereqs, key=lambda x: x["deadline"])
    # order buildnums by most recent
    builds = data["inflight"]
    data["inflight"] = sorted(builds, key=lambda x: x["buildnum"], reverse=True)
    return data


def generate_wiki(data, release, logger, config):
    logger.info("generating wiki from template and config")

    # TODO convert issues to bugs

    wiki_template = config['templates']["wiki"][release.product][release.branch]

    env = Environment(loader=FileSystemLoader(config['templates_dir']),
                      undefined=StrictUndefined, trim_blocks=True)

    template = env.get_template(wiki_template)
    return template.render(**data)


def write_data(data_path, content, release, logger, config):
    logger.info("writing to data file: %s", data_path)
    with open(data_path, 'w') as data_file:
        json.dump(content, data_file, indent=4, sort_keys=True)

    return data_path


def write_wiki(wiki_path, content, release, logger, config):
    logger.info("writing to wiki file: %s", wiki_path)
    with open(wiki_path, 'w') as wp:
        wp.write(content)

    return wiki_path


def get_release_files(release, logging, config):
    upcoming_path = os.path.join(config['release_pipeline_repo'],
                                 config['releases']['upcoming'][release.product])
    inflight_path = os.path.join(config['release_pipeline_repo'],
                                 config['releases']['inflight'][release.product])
    data_file = "{}-{}-{}.json".format(release.product, release.branch, release.version)
    wiki_file = "{}-{}-{}.md".format(release.product, release.branch, release.version)
    release_path = upcoming_path
    if os.path.exists(os.path.join(inflight_path, data_file)):
        release_path = inflight_path
    return [
        os.path.join(release_path, data_file),
        os.path.join(release_path, wiki_file)
    ]


def get_incomplete_releases(config, logger, inflight=True):
    logger.info("getting incomplete releases")
    for release_path in config['releases']['inflight' if inflight else 'upcoming'].values():
        search_dir = os.path.join(config['release_pipeline_repo'], release_path)
        for root, dirs, files in os.walk(search_dir):
            for f in [data_file for data_file in files if data_file.endswith(".json")]:
                abs_f = os.path.join(search_dir, f)
                with open(abs_f) as data_f:
                    data = json.load(data_f)
                    if inflight:
                        tasks = data["inflight"][-1]["human_tasks"]
                    else:
                        tasks = data["preflight"]["human_tasks"]
                    if not all(task["resolved"] for task in tasks):
                        # this release is complete!
                        yield data


def get_release_info(product, version, logger, config):
    branch = get_branch(version, logger)
    release = Release(product=product, version=version, branch=branch)
    data_path, wiki_path = get_release_files(release, logger, config)
    logger.debug("release info: %s", release)
    logger.debug("data path: %s", data_path)
    logger.debug("wiki path: %s", wiki_path)
    return release, data_path, wiki_path


def write_and_commit(data, release, data_path, wiki_path, commit_msg, logger, config):
    data = order_data(data)
    wiki = generate_wiki(data, release, logger, config)
    data_path = write_data(data_path, data, release, logger, config)
    wiki_path = write_wiki(wiki_path, wiki, release, logger, config)
    logger.debug(data_path)
    logger.debug(wiki_path)
    commit([data_path, wiki_path], commit_msg, logger, config)


def generate_newbuild_data(data, graphid, release, data_path, wiki_path, logger, config):
    is_first_gtb = "upcoming" in data_path
    if is_first_gtb:
        #   delete json and md files from upcoming dir, and set new dest paths to be inflight
        repo = Repo(config['release_pipeline_repo'])
        inflight_dir = os.path.join(config['release_pipeline_repo'],
                                    config['releases']['inflight'][release.product])
        moved_files = repo.index.move([data_path, wiki_path, inflight_dir])
        # set data and wiki paths to new dest (inflight) dir
        # moved_files is a list of tuples representing [files_moved][destination_location]
        # TODO
        data_path = os.path.join(config['release_pipeline_repo'], moved_files[0][1])
        wiki_path = os.path.join(config['release_pipeline_repo'], moved_files[1][1])
    else:
        #  kill latest buildnum add new buildnum based most recent buildnum
        logger.info("most recent buildnum has been aborted, starting a new buildnum")
        newbuild = deepcopy(data["inflight"][-1])
        # abort the now previous buildnum
        data["inflight"][-1]["aborted"] = True
        for task in newbuild["human_tasks"]:
            if task["alias"] == "shipit":
                continue  # leave submitted to shipit as resolved
            # reset all tasks to unresolved
            task["resolved"] = False
        # carry forward only unresolved issues
        newbuild["issues"] = get_remaining_items(newbuild["issues"])
        # increment buildnum
        newbuild["buildnum"] = newbuild["buildnum"] + 1
        # add new buildnum based on previous to current release
        data["inflight"].append(newbuild)
    data["inflight"][-1]["graphids"] = [_id for _id in graphid]

    return data, data_path, wiki_path


def get_tracking_release_data(release, gtb_date, logger, config):
    logger.info("generating data from template and config")
    data_template = os.path.join(
        config['templates_dir'],
        config['templates']["data"][release.product][release.branch]
    )
    data = load_json(data_template)
    data["version"] = release.version
    data["date"] = gtb_date
    return data


def update_inflight_human_tasks(data, resolve, logger):
    data = deepcopy(data)
    if resolve:
        for human_task_id in resolve:
            # attempt to use id as alias
            for index, task in enumerate(data["inflight"][-1]["human_tasks"]):
                if human_task_id == task['alias']:
                    data["inflight"][-1]["human_tasks"][index]["resolved"] = True
                    break
            else:
                # use id as index
                # 0 based index so -1
                human_task_id = int(human_task_id) - 1
                data["inflight"][-1]["human_tasks"][human_task_id]["resolved"] = True
    else:
        logger.info("Current existing inflight tasks:")
        for index, task in enumerate(data["inflight"][-1]["human_tasks"]):
            logger.info("ID: %s - %s", index + 1, task["description"])
        # create a new inflight human task through interactive inputs
        new_human_task = generate_inflight_task_from_input()
        data["inflight"][-1]["human_tasks"].insert(new_human_task.position,
                                                   {
                                                       "alias": "", "description": new_human_task.description,
                                                       "docs": new_human_task.docs, "resolved": False
                                                   }
                                                   )
    return data


def update_prereq_human_tasks(data, resolve):
    data = deepcopy(data)
    if resolve:
        for human_task_id in resolve:
            # 0 based index so -1
            human_task_id = int(human_task_id) - 1
            data["preflight"]["human_tasks"][human_task_id]["resolved"] = True
    else:
        # create a new prerequisite task through interactive inputs
        new_prereq = generate_prereq_task_from_input()
        data["preflight"]["human_tasks"].append(
            {
                "bug": new_prereq.bug, "deadline": new_prereq.deadline,
                "description": new_prereq.description, "resolved": False
            }
        )
    return data


def update_inflight_issue(data, resolve):
    data = deepcopy(data)
    if resolve:
        for issue_id in resolve:
            # 0 based index so -1
            issue_id = int(issue_id) - 1
            data["inflight"][-1]["issues"][issue_id]["resolved"] = True
    else:
        # create a new issueuisite task through interactive inputs
        new_issue = generate_inflight_issue_from_input()
        data["inflight"][-1]["issues"].append(
            {
                "who": new_issue.who, "bug": new_issue.bug, "description": new_issue.description,
                "resolved": False, "future_threat": True
            }
        )
    return data