#!/usr/bin/env python3

import csv
import logging
import subprocess
import os
import json
import sys

from github import Github

from s3_helper import S3Helper
from get_robot_token import get_best_robot_token
from pr_info import PRInfo
from build_download_helper import download_all_deb_packages
from upload_result_helper import upload_results
from docker_pull_helper import get_image_with_version
from commit_status_helper import post_commit_status
from clickhouse_helper import ClickHouseHelper, mark_flaky_tests, prepare_tests_results_for_clickhouse
from stopwatch import Stopwatch


def get_run_command(build_path, result_folder, server_log_folder, image):
    cmd = "docker run -e S3_URL='https://clickhouse-datasets.s3.amazonaws.com' " + \
          f"--volume={build_path}:/package_folder "  \
          f"--volume={result_folder}:/test_output " \
          f"--volume={server_log_folder}:/var/log/clickhouse-server {image}"

    return cmd

def process_results(result_folder, server_log_path, run_log_path):
    test_results = []
    additional_files = []
    # Just upload all files from result_folder.
    # If task provides processed results, then it's responsible for content of result_folder.
    if os.path.exists(result_folder):
        test_files = [f for f in os.listdir(result_folder) if os.path.isfile(os.path.join(result_folder, f))]
        additional_files = [os.path.join(result_folder, f) for f in test_files]

    if os.path.exists(server_log_path):
        server_log_files = [f for f in os.listdir(server_log_path) if os.path.isfile(os.path.join(server_log_path, f))]
        additional_files = additional_files + [os.path.join(server_log_path, f) for f in server_log_files]

    additional_files.append(run_log_path)

    status_path = os.path.join(result_folder, "check_status.tsv")
    if not os.path.exists(status_path):
        return "failure", "check_status.tsv doesn't exists", test_results, additional_files

    logging.info("Found check_status.tsv")
    with open(status_path, 'r', encoding='utf-8') as status_file:
        status = list(csv.reader(status_file, delimiter='\t'))

    if len(status) != 1 or len(status[0]) != 2:
        return "error", "Invalid check_status.tsv", test_results, additional_files
    state, description = status[0][0], status[0][1]

    results_path = os.path.join(result_folder, "test_results.tsv")
    with open(results_path, 'r', encoding='utf-8') as results_file:
        test_results = list(csv.reader(results_file, delimiter='\t'))
    if len(test_results) == 0:
        raise Exception("Empty results")

    return state, description, test_results, additional_files


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    stopwatch = Stopwatch()
    temp_path = os.getenv("TEMP_PATH", os.path.abspath("."))
    repo_path = os.getenv("REPO_COPY", os.path.abspath("../../"))
    reports_path = os.getenv("REPORTS_PATH", "./reports")

    check_name = sys.argv[1]

    if not os.path.exists(temp_path):
        os.makedirs(temp_path)

    with open(os.getenv('GITHUB_EVENT_PATH'), 'r', encoding='utf-8') as event_file:
        event = json.load(event_file)

    pr_info = PRInfo(event)

    gh = Github(get_best_robot_token())

    docker_image = get_image_with_version(reports_path, 'clickhouse/stress-test')

    packages_path = os.path.join(temp_path, "packages")
    if not os.path.exists(packages_path):
        os.makedirs(packages_path)

    download_all_deb_packages(check_name, reports_path, packages_path)

    server_log_path = os.path.join(temp_path, "server_log")
    if not os.path.exists(server_log_path):
        os.makedirs(server_log_path)

    result_path = os.path.join(temp_path, "result_path")
    if not os.path.exists(result_path):
        os.makedirs(result_path)

    run_log_path = os.path.join(temp_path, "runlog.log")

    run_command = get_run_command(packages_path, result_path, server_log_path, docker_image)
    logging.info("Going to run func tests: %s", run_command)

    with open(run_log_path, 'w', encoding='utf-8') as log:
        with subprocess.Popen(run_command, shell=True, stderr=log, stdout=log) as process:
            retcode = process.wait()
            if retcode == 0:
                logging.info("Run successfully")
            else:
                logging.info("Run failed")

    subprocess.check_call(f"sudo chown -R ubuntu:ubuntu {temp_path}", shell=True)


    s3_helper = S3Helper('https://s3.amazonaws.com')
    state, description, test_results, additional_logs = process_results(result_path, server_log_path, run_log_path)
    ch_helper = ClickHouseHelper()
    mark_flaky_tests(ch_helper, check_name, test_results)

    report_url = upload_results(s3_helper, pr_info.number, pr_info.sha, test_results, [run_log_path] + additional_logs, check_name)
    print(f"::notice ::Report url: {report_url}")

    post_commit_status(gh, pr_info.sha, check_name, description, state, report_url)

    prepared_events = prepare_tests_results_for_clickhouse(pr_info, test_results, state, stopwatch.duration_seconds, stopwatch.start_time_str, report_url, check_name)
    ch_helper.insert_events_into(db="gh-data", table="checks", events=prepared_events)
