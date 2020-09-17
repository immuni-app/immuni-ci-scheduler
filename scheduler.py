# scheduler.py
# Copyright (C) 2020 Presidenza del Consiglio dei Ministri.
# Please refer to the AUTHORS file for more information.
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Check pending CircleCI pipelines and, if correctly verified, run Danger checks"""
import datetime
import hashlib
import json
import markdown_strings
import os
import subprocess
import tempfile

from concurrent.futures.process import ProcessPoolExecutor
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass
from decouple import config
from git import CommandError, Repo
from github import Github
from helpers import utils
from helpers.circleci import CircleCI
from typing import Dict, Iterable, List, Optional, Set, Tuple


@dataclass
class DangerCandidatePipeline:
    """Class for keeping track of a candidate Danger execution"""

    check_details: str
    commit: str
    pipeline_nr: int
    pull_requests: Optional[Set[int]]
    repo_dir: tempfile.TemporaryDirectory
    safe: bool
    should_run_danger: bool


@dataclass
class DangerPRExecution:
    """Class for keeping track of a Danger execution on a PR"""

    commit: str
    pull_request: int
    repo_dir: tempfile.TemporaryDirectory


# Constants
MAX_PROCESSES = 4
MAX_THREADS = 4
REFERENCE_BRANCH = config("REFERENCE_BRANCH", "master")
SCHEDULER_BRANCH = config("SCHEDULER_BRANCH", "master")
SCHEDULER_CONFIG_FILE = "config.json"
SCHEDULER_SUBMODULE_NAME = "scheduler"
SCHEDULER_WORKFLOW = "scheduler"
VERIFIER_BOT_NAME = config("GITHUB_USERNAME")

# Configuration
CURRENT_SCHEDULER_WORKFLOW = config("CIRCLE_WORKFLOW_ID", "")
GITHUB_TOKEN = config("GITHUB_TOKEN")
PROJECT_PATH = config("PROJECT_PATH", os.getcwd())
REPOSITORY = config("REPOSITORY")
with open(SCHEDULER_CONFIG_FILE) as f:
    SCHEDULER_CONFIG = json.load(f)

# Configure CircleCI manager
circleci = CircleCI(api_token=config("CIRCLECI_API_TOKEN"), project_slug=f"gh/{REPOSITORY}")

# Configure GitHub
gh = Github(GITHUB_TOKEN)
repo = gh.get_repo(REPOSITORY)

# Files to check
PROTECTED_FILES: Set = set(SCHEDULER_CONFIG["protected_files"])

# Messages
SAFETY_CHECK_PASS_MESSAGE = (
    f"✅ All configuration files are in line with the {REFERENCE_BRANCH} branch."
)
SAFETY_CHECK_FAIL_MESSAGE = (
    f"⚠️ Some configuration files don't match the {REFERENCE_BRANCH} branch. If you did not "
    f"perform these changes, **please rebase on the {REFERENCE_BRANCH} branch**."
)
SAFETY_CHECK_NO_FILES_SPECIFIED = (
    f"⚠️ No files of the {REFERENCE_BRANCH} branch have been specified for check."
)


def check_and_schedule():
    """Check every pipeline that has been submitted since the last execution of the scheduler.

    If a pipeline is found to contain modifications to the configuration files, the changes are
    reported to the maintainers for additional review and no further action is taken.

    If a pipeline does not contain any modifications to the configuration files, its associated
    pull request is a forked one, and the pull request has not already been checked by Danger
    during this run of the scheduler, a Danger session is run on its commit.

    Pipelines are checked from the latest to the oldest, so that every PR is checked by Danger
    only once on its latest commit.
    """
    reference_repo_dir = tempfile.TemporaryDirectory()

    # Retrieve the latest pipeline on the reference branch, if any. We must exclude pipelines
    # triggered by cron jobs, though, as they contain a different compiled CircleCI configuration
    # (due to limitations of how CircleCI works). To avoid this, the scheduler workflow is never
    # run on commit, and therefore we can identify "non-cron" pipelines by asking that they should
    # not contain the scheduler workflow.
    reference_pipelines = circleci.fetch_pipelines(
        branch=REFERENCE_BRANCH,
        not_containing_workflows=[SCHEDULER_WORKFLOW],
        limit=1,
        multipage=True,
    )

    # This script requires a reference configuration to exist
    if not reference_pipelines:
        print(f"Unable to fetch pipelines for reference branch {REFERENCE_BRANCH}, halting.")
        exit(1)

    # Retrieve the reference configuration
    latest_reference_pipeline = reference_pipelines[0]
    reference_config = circleci.get_pipeline_config(latest_reference_pipeline["id"])["compiled"]

    # Initialize the reference git repo
    reference_repo = Repo.clone_from(
        latest_reference_pipeline["vcs"]["target_repository_url"], reference_repo_dir.name
    )
    reference_repo.git.checkout(latest_reference_pipeline["vcs"]["revision"])

    # Compute the SHA256 of every protected file
    reference_protected_files = utils.compute_files_hash(reference_repo_dir.name, PROTECTED_FILES)
    reference_scheduler_sha = utils.get_submodule_sha(reference_repo, SCHEDULER_SUBMODULE_NAME)

    # Retrieve the latest successful execution of the scheduler pipeline, if any
    scheduler_pipelines = circleci.fetch_pipelines(
        branch=SCHEDULER_BRANCH,
        containing_workflows=[SCHEDULER_WORKFLOW],
        multipage=False,
        successful_only=True,
    )
    latest_scheduler_pipeline = scheduler_pipelines[0] if scheduler_pipelines else None

    # Retrieve the pipelines to check
    pipelines_to_check = circleci.fetch_pipelines(
        multipage=True,
        stopping_pipeline_id=(
            latest_scheduler_pipeline["id"] if latest_scheduler_pipeline else None
        ),
    )

    # If this variable is not set, the script is running outside CircleCI.
    # In that case, don't filter out pipelines that have been launched soon after the execution
    # of this scheduler run.
    current_scheduler_workflow = {"pipeline_id": "devmode", "pipeline_number": "devmode"}
    if CURRENT_SCHEDULER_WORKFLOW:
        current_scheduler_workflow = circleci.get_workflow(CURRENT_SCHEDULER_WORKFLOW)
        starting_pipeline_id = current_scheduler_workflow["pipeline_id"]
        pipelines_to_check = reversed(
            circleci.filter_pipelines(
                reversed(pipelines_to_check), stopping_pipeline_id=starting_pipeline_id
            ).pipelines
        )

    # Check recently submitted pipelines for integrity, and retrieve the sublist of safe ones
    check_args = [
        [p, reference_config, reference_protected_files, reference_scheduler_sha]
        for p in pipelines_to_check
    ]
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        results: Iterable[DangerCandidatePipeline] = executor.map(
            lambda x: _check_pipeline(*x), check_args
        )

    # Sort retrieved pipelines in descending order of submission (newest pipelines come first)
    sorted_pipelines = sorted(
        list(results), key=lambda danger_pipeline: danger_pipeline.pipeline_nr, reverse=True
    )

    # Keep track of which PRs have to be checked, and create a DangerPRExecution for each of them
    danger_pr_executions: List[DangerPRExecution] = []
    prs_to_check = set()

    # Only run Danger once per PR, on the latest commit, and if the commit has been verified as safe
    for p in sorted_pipelines:
        if p.pull_requests:
            for pull_request in p.pull_requests:
                if pull_request not in prs_to_check:
                    prs_to_check.add(pull_request)
                    _notify_safety_check(
                        p.check_details,
                        p.commit,
                        pull_request,
                        safe=p.safe,
                        scheduler_workflow=current_scheduler_workflow,
                    )
                    if p.should_run_danger:
                        danger_pr_executions.append(
                            DangerPRExecution(
                                commit=p.commit, pull_request=pull_request, repo_dir=p.repo_dir,
                            )
                        )

    # Print a recap of the Danger jobs we're about to run:
    print(f"The following forked PRs have been deemed safe and will be checked by Danger:", end=" ")
    print(", ".join([str(pr_execution.pull_request) for pr_execution in danger_pr_executions]))

    # Run Danger on the identified PRs. Use processes instead of threads since we need to cd
    # into a directory.
    with ProcessPoolExecutor(max_workers=MAX_PROCESSES) as executor:
        executor.map(_run_danger, danger_pr_executions)

    # Cleanup the temporary repository directories
    repo_dirs = set(result.repo_dir for result in results)
    for repo_dir in repo_dirs:
        repo_dir.cleanup()

    reference_repo_dir.cleanup()


def _check_pipeline(
    pipeline: Dict,
    reference_config: str,
    reference_protected_files: Dict[str, Optional[str]],
    reference_scheduler_sha: str,
) -> DangerCandidatePipeline:
    """Check the pipeline configuration for integrity.

    :param pipeline: a pipeline object.
    :param reference_config: the CircleCI configuration of the reference branch.
    :param reference_protected_files: a dictionary mapping the protected files present in the
    original repository of the organization, on the reference branch, to their SHA256 hash.
    :param reference_scheduler_sha: the SHA of the scheduler submodule on the reference branch, if
    the submodule exists; an empty string otherwise.
    :return: a DangerPipeline object containing the number of the verified pipeline, the commit on
    which it was run, its associated pull request (if any), a reference to the temporary directory
    in which the repository has been cloned, and a boolean describing whether the pipeline is safe
    for Danger to be run on or not.
    """
    commit = pipeline["vcs"]["revision"]
    # Create a temporary directory to clone the repository. This operation is thread safe, as long
    # as we use the default temp dir (which is computed with an absolute path)
    repo_dir = tempfile.TemporaryDirectory()
    response = circleci.get_pipeline_config(pipeline["id"])

    # Initialize the original git repo
    try:
        contributor_repo = Repo.clone_from(pipeline["vcs"]["origin_repository_url"], repo_dir.name)
        contributor_repo.git.checkout(commit)
    except CommandError:
        check_details = (
            f"Unable to checkout revision {commit} "
            f"on contributor repo for pipeline #{pipeline['number']} ({pipeline['id']})!"
        )
        _log_safety_check(check_details, pipeline, False)
        return DangerCandidatePipeline(
            check_details=check_details,
            commit=commit,
            pipeline_nr=pipeline["number"],
            pull_requests=None,
            repo_dir=repo_dir,
            safe=False,
            should_run_danger=False,
        )

    # Compute the file hash of the new versions of the protected files
    new_protected_files = utils.compute_files_hash(repo_dir.name, reference_protected_files.keys())
    new_scheduler_sha = utils.get_submodule_sha(contributor_repo, SCHEDULER_SUBMODULE_NAME)

    # Check the pipeline's integrity
    safe, check_details = _safety_check(
        new_protected_files,
        new_scheduler_sha,
        response["compiled"],
        reference_config,
        reference_protected_files,
        reference_scheduler_sha,
    )
    _log_safety_check(check_details, pipeline, safe)

    # Try to fetch the PR(s) associated with the pipeline. If present, post the result of the
    # analysis as a comment. If the PR is external, Danger should be run, too.
    internal = pipeline["vcs"]["origin_repository_url"] == pipeline["vcs"]["target_repository_url"]
    pull_requests = set()
    try:
        if internal:
            # This is a PR on the internal repo. Danger will not be executed by the scheduler,
            # as it is already been run on commit.
            workflows = circleci.get_pipeline_workflows(pipeline["id"])
            if not workflows:
                return DangerCandidatePipeline(
                    check_details=check_details,
                    commit=commit,
                    pipeline_nr=pipeline["number"],
                    pull_requests=None,
                    repo_dir=repo_dir,
                    safe=False,
                    should_run_danger=False,
                )
            pull_requests = circleci.get_workflow_prs(workflows[0]["id"])
        else:
            # This is a PR from a forked repo.
            # Detect the PR number from the branch, post the message, and schedule Danger.
            pull_request = int(pipeline["vcs"]["branch"].split("pull/")[1])
            pull_requests.add(pull_request)
    except Exception as e:
        # If anything goes wrong, don't crash, but log the error
        print(f"Unable to retrieve PR for pipeline #{pipeline['number']} ({pipeline['id']})!\n{e}")
        return DangerCandidatePipeline(
            check_details=check_details,
            commit=commit,
            pipeline_nr=pipeline["number"],
            pull_requests=None,
            repo_dir=repo_dir,
            safe=False,
            should_run_danger=False,
        )

    # If the pipeline passed the integrity check and we verified it's associated to a forked PR,
    # schedule a Danger run. Otherwise, Danger should not be executed.
    return DangerCandidatePipeline(
        check_details=check_details,
        commit=commit,
        pipeline_nr=pipeline["number"],
        pull_requests=pull_requests,
        repo_dir=repo_dir,
        safe=safe,
        should_run_danger=not internal and safe,
    )


def _run_danger(pr_execution: DangerPRExecution):
    """Run Danger on the specified pull request.
    Since Danger expects to be run in a dedicated CI environment, simulate a Bitrise CI pipeline
    for the specified pull request.

    :param pr_execution: a pull request execution object, specifying the commit that Danger should
    analyze, the PR number, and the path to the cloned repository.
    """
    # Simulate a dedicated Bitrise CI pipeline for Danger
    ci_env = {
        key: value
        for key, value in os.environ.copy().items()
        if not ("CIRCLE_" in key or key == "CI" or key == "CIRCLECI")
    }
    ci_env.update(
        {
            "BITRISE_GIT_COMMIT": pr_execution.commit,
            "BITRISE_IO": "TRUE",
            "BITRISE_PULL_REQUEST": str(pr_execution.pull_request),
            "DANGER_GITHUB_API_TOKEN": GITHUB_TOKEN,
            "GIT_REPOSITORY_URL": f"https://github.com/{REPOSITORY}.git",
        }
    )
    # Symlink the node modules that Danger requires to run from the repository root
    if os.path.exists(os.path.join(PROJECT_PATH, "node_modules")):
        os.symlink(
            os.path.join(PROJECT_PATH, "node_modules"),
            os.path.join(pr_execution.repo_dir.name, "node_modules"),
            target_is_directory=True,
        )
    else:
        print(
            f"Encounted error while running Danger on PR #{pr_execution.pull_request}.\n"
            f"This repository has an outdated config.yml that does not install the required "
            f"dependencies to run Danger within Scheduler. Skipping Danger execution."
        )
        return
    # Run Danger on the cloned repository. In case of issues, fail gracefully.
    with utils.cd(pr_execution.repo_dir.name):
        try:
            subprocess.run(["yarn", "run", "danger", "ci"], check=True, env=ci_env)
            print(f"Danger executed successfully on PR #{pr_execution.pull_request}.")
        except subprocess.CalledProcessError as e:
            print(f"Danger exited with non-zero code on PR #{pr_execution.pull_request}.\n{e}")
        except Exception as e:
            print(f"Unexpected error while running Danger on PR #{pr_execution.pull_request}.\n{e}")


def _log_safety_check(check_details: str, pipeline: Dict, safe: bool):
    """Log the result of the safety check.

    :param check_details: a message detailing the safety check result.
    :param pipeline: a pipeline object.
    :param safe: True if the specified pipeline passed the safety check, False otherwise.
    """
    print(
        f"Safety check for CircleCI pipeline: #{pipeline['number']} (id: {pipeline['id']}) \n"
        f"{SAFETY_CHECK_PASS_MESSAGE if safe else SAFETY_CHECK_FAIL_MESSAGE}\n"
        f"{check_details}"
    )


def _notify_safety_check(
    check_details: str, commit: str, pull_request: int, safe: bool, scheduler_workflow: Dict
):
    """Post the result of the safety check as a comment on a pull request.

    :param check_details: a message detailing the safety check result.
    :param commit: the checked commit.
    :param pull_request: the number of a pull request.
    :param safe: True if the latest pipeline execution associated with the specified pull request
    passed the safety check, False otherwise.
    :param scheduler_workflow: the CircleCI workflow associated with the current scheduler run.
    """
    # Check if we already left a comment. If so, we should edit it, but only if it's the first
    # time we encounter the PR inside this scheduler run.
    title = "🚔 **Safety Check** 🚔"
    message = f"{title}\n"
    message += "\n🔰 **Result** 🔰\n"
    message += f"{check_details}\n"
    message += f"\n{SAFETY_CHECK_PASS_MESSAGE if safe else SAFETY_CHECK_FAIL_MESSAGE}\n"
    message += "\n🛠 **Diagnostic information** 🛠\n"
    message += f"- CircleCI scheduler pipeline: #{scheduler_workflow['pipeline_number']} (id: {scheduler_workflow['pipeline_id']})\n"
    message += f"- Last verified commit: {commit}\n"
    message += f"- Time of check: {datetime.datetime.utcnow().strftime('%d/%m/%Y, %H:%M:%S')} UTC\n"
    if PROTECTED_FILES:
        escaped_files = markdown_strings.esc_format(", ".join(sorted(PROTECTED_FILES)))
        message += (
            f"- The following protected files have been checked for changes: {escaped_files}.\n"
        )
    else:
        message += f"\n{SAFETY_CHECK_NO_FILES_SPECIFIED}\n"

    issue = repo.get_issue(pull_request)
    comments = issue.get_comments()

    # Check the existence of a previous safety check comment
    left_comments = list(
        filter(lambda c: c.user.login == VERIFIER_BOT_NAME and title in c.body, comments)
    )

    if left_comments:
        # This should be the only comment, by construction
        comment = issue.get_comment(left_comments[0].id)
        comment.edit(message)
    else:
        issue.create_comment(message)


def _safety_check(
    current_protected_file_hashes: Dict[str, Optional[str]],
    current_scheduler_sha: str,
    pipeline_config: str,
    reference_config: str,
    reference_protected_file_hashes: Dict[str, Optional[str]],
    reference_scheduler_sha: str,
) -> Tuple[bool, str]:
    """Check the pipeline configuration for integrity.

    :param current_protected_file_hashes: a dictionary mapping the protected files present in the
    cloned repository from which the pipeline should run to their SHA256 hash.
    :param current_scheduler_sha: the SHA of the scheduler submodule of the cloned repository, if
    the submodule exists; an empty string otherwise.
    :param pipeline_config: the configuration of the pipeline to be tested.
    :param reference_config: the configuration of the reference pipeline.
    :param reference_protected_file_hashes: a dictionary mapping the protected files present in the
    original repository of the organization, on the reference branch, to their SHA256 hash.
    :param reference_scheduler_sha: the SHA of the scheduler submodule on the reference branch, if
    the submodule exists; an empty string otherwise.
    :return: a tuple describing the output of the check. The first return value is a boolean, which
    is True if the configurations match, False otherwise. The second return value is a detailed
    description of the check result.
    """
    message = ""
    safe = True

    # Identify which files have a non-null SHA256 (i.e., they actually exist in the repo)
    current_protected_files = utils.get_files_by_hash_map(current_protected_file_hashes)
    reference_protected_files = utils.get_files_by_hash_map(reference_protected_file_hashes)

    # Compute differences
    added_files = current_protected_files - reference_protected_files
    common_files = reference_protected_files.intersection(current_protected_files)
    deleted_files = reference_protected_files - current_protected_files
    modified_files = []

    for filename in common_files:
        if reference_protected_file_hashes[filename] != current_protected_file_hashes[filename]:
            modified_files.append(filename)

    if added_files:
        escaped_added_files = markdown_strings.esc_format(", ".join(added_files))
        safe = False
        message += f"- The following files have been added: {escaped_added_files}.\n"

    if deleted_files:
        escaped_deleted_files = markdown_strings.esc_format(", ".join(deleted_files))
        safe = False
        message += f"- The following files have been deleted: {escaped_deleted_files}.\n"

    if modified_files:
        escaped_modified_files = markdown_strings.esc_format(", ".join(modified_files))
        safe = False
        message += f"- The following files have been modified: {escaped_modified_files}.\n"

    pipeline_config_hash = hashlib.sha256(pipeline_config.encode("utf-8")).hexdigest()
    reference_config_hash = hashlib.sha256(reference_config.encode("utf-8")).hexdigest()

    if pipeline_config_hash != reference_config_hash:
        safe = False
        message += f"- The CircleCI configuration file has been modified.\n"

    if current_scheduler_sha != reference_scheduler_sha:
        safe = False
        message += f"- The revision of the scheduler submodule has changed.\n"

    return safe, message


if __name__ == "__main__":
    check_and_schedule()
