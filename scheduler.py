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

"""Check pending CircleCI pipelines and, if correctly verified, schedule their jobs"""
import datetime
import hashlib
import json
import tempfile

from helpers.circleci import CircleCI
from decouple import config
from git import CommandError, Repo
from github import Github
from typing import Dict, List, Optional, Set, Tuple
from helpers import utils


# Constants
AUTHORIZED_WORKFLOWS = ["pr_check"]
REFERENCE_BRANCH = "master"
SCHEDULER_BRANCH = "master"
SCHEDULER_CONFIG_FILE = "config.json"
SCHEDULER_WORKFLOW = "scheduler"
VERIFIER_BOT_NAME = config("RELEASE_GITHUB_USERNAME")

# Configuration
CURRENT_SCHEDULER_WORKFLOW = config("CIRCLE_WORKFLOW_ID", "")
REPOSITORY = config("REPOSITORY")
with open(SCHEDULER_CONFIG_FILE) as f:
    SCHEDULER_CONFIG = json.load(f)

# Configure CircleCI manager
circleci = CircleCI(api_token=config("CIRCLECI_API_TOKEN"), project_slug=f"gh/{REPOSITORY}")

# Configure GitHub
gh = Github(config("GITHUB_TOKEN"))
repo = gh.get_repo(REPOSITORY)

# Files to check
PROTECTED_FILES: Set = set(SCHEDULER_CONFIG["protected_files"])

# Messages
SAFETY_CHECK_PASS_MESSAGE = (
    f"✅ All configuration files are in line with the {REFERENCE_BRANCH} branch."
)
SAFETY_CHECK_FAIL_MESSAGE = (
    f"⚠️ Some configuration files don't match the {REFERENCE_BRANCH} branch."
)
SAFETY_CHECK_NO_FILES_SPECIFIED = (
    f"⚠️ No files of the {REFERENCE_BRANCH} branch have been specified for check."
)


def check_and_schedule():
    """Check every pipeline that has been submitted since the last execution of the scheduler.

    If a pipeline is found to contain modifications to the configuration files, the changes are
    reported to the maintainers for additional review and no further action is taken.

    If a pipeline does not contain any modifications to the configuration files, its authorized
    workflows that have an "unauthorized" CircleCI status are triggered on behalf of the submitter.

    Only workflows specified in the AUTHORIZED_WORKFLOWS variable are executed.
    """
    checked_prs = []
    reference_repo_dir = tempfile.TemporaryDirectory()

    # Retrieve the latest pipeline on the reference branch, if any
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

    # Retrieve the latest successful execution of the scheduler pipeline, if any
    scheduler_pipelines = circleci.fetch_pipelines(
        branch=SCHEDULER_BRANCH,
        containing_workflows=[SCHEDULER_WORKFLOW],
        multipage=False,
        successful_only=True,
    )
    latest_scheduler_pipeline = scheduler_pipelines[0] if scheduler_pipelines else None

    # Retrieve the pending pipelines to check
    pending_pipelines = circleci.fetch_pipelines(
        multipage=True,
        stopping_pipeline_id=(
            latest_scheduler_pipeline["id"] if latest_scheduler_pipeline else None
        ),
    )

    # If this variable is not set, the script is running outside CircleCI.
    # In that case, don't filter out pipelines that have been launched soon after the execution
    # of this scheduler run.
    if CURRENT_SCHEDULER_WORKFLOW:
        starting_pipeline_id = circleci.get_workflow(CURRENT_SCHEDULER_WORKFLOW)["pipeline_id"]
        pending_pipelines = reversed(
            circleci.filter_pipelines(
                reversed(pending_pipelines), stopping_pipeline_id=starting_pipeline_id
            )[0]
        )

    # Check the pipelines and schedule workflows if appropriate
    for pipeline in pending_pipelines:
        _check_and_schedule_pipeline(
            checked_prs, pipeline, reference_config, reference_protected_files
        )

    reference_repo_dir.cleanup()


def _check_and_schedule_pipeline(
    checked_prs: List[int],
    pipeline: Dict,
    reference_config: str,
    reference_protected_files: Dict[str, Optional[str]],
):
    """Check the pipeline configuration for integrity.

    :param checked_prs: a list of already checked PRs.
    :param pipeline: a pipeline object.
    :param reference_config: the CircleCI configuration of the reference branch.
    :param reference_protected_files: a dictionary mapping the protected files present in the
    original repository of the organization, on the reference branch, to their SHA256 hash.
    """
    repo_dir = tempfile.TemporaryDirectory()
    response = circleci.get_pipeline_config(pipeline["id"])

    # Initialize the original git repo
    try:
        contributor_repo = Repo.clone_from(pipeline["vcs"]["origin_repository_url"], repo_dir.name)
        contributor_repo.git.checkout(pipeline["vcs"]["revision"])
    except CommandError:
        check_details = (
            f"Unable to checkout revision {pipeline['vcs']['revision']} "
            f"on contributor repo for pipeline #{pipeline['number']} ({pipeline['id']})!"
        )
        _log_safety_check(check_details, pipeline, False)

    current_protected_files = utils.compute_files_hash(
        repo_dir.name, reference_protected_files.keys()
    )
    repo_dir.cleanup()

    # Check the pipeline's integrity
    is_safe, check_details = _safety_check(
        current_protected_files, response["compiled"], reference_config, reference_protected_files
    )
    _log_safety_check(check_details, pipeline, is_safe)

    # Retrieve the pipeline's workflows.
    workflows = circleci.get_pipeline_workflows(pipeline["id"])
    if not workflows:
        return

    should_trigger_workflows = False

    try:
        if pipeline["vcs"]["origin_repository_url"] == pipeline["vcs"]["target_repository_url"]:
            # This is a PR on the internal repo.
            # Pick a workflow to extract the pipeline's PRs, retrieve them, and leave a comment.
            for pr in circleci.get_workflow_prs(workflows[0]["id"]):
                should_trigger_workflows = should_trigger_workflows or _notify_checked_pr(
                    check_details, checked_prs, pipeline, pr, is_safe
                )
        else:
            # This is a PR from a forked repo.
            # The workflow is unauthorized, but we can detect the PR number from the branch.
            pr = int(pipeline["vcs"]["branch"].split("pull/")[1])
            should_trigger_workflows = should_trigger_workflows or _notify_checked_pr(
                check_details, checked_prs, pipeline, pr, is_safe
            )
    except Exception as e:
        # If anything goes wrong, don't crash, but log the error
        print(f"Unable to retrieve PR for pipeline #{pipeline['number']} ({pipeline['id']})!")
        print(e)
        return

    # If the pipeline did not pass the integrity check, the PR has already been run,
    # or we were unable to retrieve the PR, stop
    if not is_safe or not should_trigger_workflows:
        return

    # If the configurations match and the PR hasn't been run yet, pending jobs can be executed
    for workflow in workflows:
        if workflow["name"] in AUTHORIZED_WORKFLOWS and workflow["status"] == "unauthorized":
            print(f"Executing previously unauthorized workflow")
            print(f"Pipeline: #{pipeline['number']} ({pipeline['id']})")
            print(f"Workflow: {workflow['id']}")
            circleci.rerun_workflow(workflow["id"])


def _log_safety_check(check_details: str, pipeline: Dict, is_safe: bool):
    """Log the result of the safety check.

    :param check_details: a message detailing the safety check result.
    :param pipeline: a pipeline object.
    :param is_safe: True if the specified pipeline passed the safety check, False otherwise.
    """
    print(f"Safety check for CircleCI pipeline: #{pipeline['number']} (id: {pipeline['id']})")
    print(SAFETY_CHECK_PASS_MESSAGE if is_safe else SAFETY_CHECK_FAIL_MESSAGE)
    print(check_details)


def _notify_checked_pr(
    check_details: str, checked_prs: List[int], pipeline: Dict, pull_request: int, is_safe: bool
) -> bool:
    """Verify if the specified PR has already been checked.
    If not, post the result of the safety check as a comment on a pull request.

    :param check_details: a message detailing the safety check result.
    :param checked_prs: the list of already checked pull requests.
    :param pipeline: the CircleCI pipeline associated with the specified pull request.
    :param pull_request: the number of a pull request.
    :param is_safe: True if the latest pipeline execution associated with the specified pull request
    passed the safety check, False otherwise.
    :return:
    """
    if pull_request not in checked_prs:
        _notify_safety_check(check_details, pipeline, pull_request, is_safe)
        checked_prs.append(pull_request)
        return True

    return False


def _notify_safety_check(check_details: str, pipeline: Dict, pull_request: int, is_safe: bool):
    """Post the result of the safety check as a comment on a pull request.

    :param check_details: a message detailing the safety check result.
    :param pipeline: the CircleCI pipeline associated with the specified pull request.
    :param pull_request: the number of a pull request.
    :param is_safe: True if the latest pipeline execution associated with the specified pull request
    passed the safety check, False otherwise.
    """
    # Check if we already left a comment. If so, we should edit it, but only if it's the first
    # time we encounter the PR inside this scheduler run.
    title = "🚔 **Safety Check** 🚔"
    message = f"{title}\n"
    message += "\n🔰 **Result** 🔰\n"
    message += f"{check_details}\n"
    message += f"\n{SAFETY_CHECK_PASS_MESSAGE if is_safe else SAFETY_CHECK_FAIL_MESSAGE}\n"
    message += "\n🛠 **Diagnostic information** 🛠\n"
    message += f"- CircleCI scheduler pipeline: #{pipeline['number']} (id: {pipeline['id']})\n"
    if "vcs" in pipeline and "revision" in pipeline["vcs"]:
        message += f"- Last verified commit: #{pipeline['vcs']['revision']}\n"
    message += f"- Time of check: {datetime.datetime.utcnow().strftime('%d/%m/%Y, %H:%M:%S')} UTC\n"
    if PROTECTED_FILES:
        message += f"- The following protected files have been checked for changes: {', '.join(PROTECTED_FILES)}.\n"
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
    current_protected_files: Dict[str, Optional[str]],
    pipeline_config: str,
    reference_config: str,
    reference_protected_files: Dict[str, Optional[str]],
) -> Tuple[bool, str]:
    """Check the pipeline configuration for integrity.

    :param current_protected_files: a dictionary mapping the protected files present in the cloned
    repository from which the pipeline should run to their SHA256 hash.
    :param pipeline_config: the configuration of the pipeline to be tested.
    :param reference_config: the configuration of the reference pipeline.
    :param reference_protected_files: a dictionary mapping the protected files present in the
    original repository of the organization, on the reference branch, to their SHA256 hash.
    :return: True if the configurations match, False otherwise.
    """
    message = ""
    result = True

    # Identify which files have a non-null SHA256 (i.e., they actually exist in the repo)
    current_protected_existing_files = utils.get_files_by_hash_map(current_protected_files)
    reference_protected_existing_files = utils.get_files_by_hash_map(reference_protected_files)

    # Compute differences
    added_files = current_protected_existing_files - reference_protected_existing_files
    common_files = reference_protected_existing_files.intersection(current_protected_existing_files)
    deleted_files = reference_protected_existing_files - current_protected_existing_files
    modified_files = []

    for f in common_files:
        if reference_protected_files[f] != current_protected_files[f]:
            modified_files.append(f)

    if added_files:
        result = False
        message += f"- The following files have been added: {', '.join(added_files)}.\n"

    if deleted_files:
        result = False
        message += f"- The following files have been deleted: {', '.join(deleted_files)}.\n"

    if modified_files:
        result = False
        message += f"- The following files have been modified: {', '.join(modified_files)}.\n"

    pipeline_config_hash = hashlib.sha256(pipeline_config.encode("utf-8")).hexdigest()
    reference_config_hash = hashlib.sha256(reference_config.encode("utf-8")).hexdigest()

    if pipeline_config_hash != reference_config_hash:
        result = False
        message += f"- The CircleCI configuration file has been modified.\n"

    return result, utils.sanitize_markdown(message)


if __name__ == "__main__":
    check_and_schedule()
