# circleci.py
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

"""Handle communication with CircleCI"""

import json
import requests

from enum import Enum
from requests.auth import HTTPBasicAuth
from requests.exceptions import ConnectionError, Timeout
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple, Set

API_URL = f"https://circleci.com/api"


class APIVersion(Enum):
    v11 = 1.1
    v20 = 2


class FilteredPipelines(NamedTuple):
    pipelines: List[Dict[str, Any]]
    found_stopping_pipeline: bool


class CircleCI(object):
    def __init__(self, api_token, project_slug):
        self.api_token = api_token
        self.project_slug = project_slug

    def fetch_pipelines(
        self,
        branch: Optional[str] = None,
        containing_workflows: Optional[List[str]] = None,
        limit: Optional[int] = None,
        multipage: bool = False,
        not_containing_workflows: Optional[List[str]] = None,
        stopping_pipeline_id: Optional[str] = None,
        successful_only=False,
    ) -> List[Dict[str, Any]]:
        """Fetch pipelines from CircleCI. For more information about the data format of the method
        return value, see https://circleci.com/docs/api/v2/#get-all-pipelines

        :param branch: if specified, only return the pipelines executed from this branch.
        :param containing_workflows: if specified, only return the pipelines containing these
        workflows.
        :param limit: if set, only return the first matching `limit` pipelines.
        :param multipage: if True, retrieve pipeline data from all available pages; otherwise, stop
        at the first.
        :param not_containing_workflows: if specified, only return the pipelines not containing
        these workflows.
        :param stopping_pipeline_id: if specified, only return the pipelines that have been executed
        after this one.
        :param successful_only: if True, only retrieve pipelines whose workflows succeeded. If
        `containing_workflows` is specified, only those workflows are checked for success.
        :return: a list of pipelines.
        """
        params = {"branch": branch} if branch else {}
        retrieved_pipelines: List[Dict[str, Any]] = []
        stopping = False

        # Retrieve CircleCI pipelines with pagination
        while not stopping:
            response = self._get(
                APIVersion.v20, f"project/{self.project_slug}/pipeline", params=params
            )
            filtered_pipelines, found_stopping_pipeline = self.filter_pipelines(
                response["items"], stopping_pipeline_id
            )

            if containing_workflows or not_containing_workflows or successful_only:
                for pipeline in filtered_pipelines:
                    # Don't retrieve more than `limit` matching pipelines if limit is enforced
                    if limit is not None and len(retrieved_pipelines) >= limit:
                        found_stopping_pipeline = True
                        break
                    # Verify matching conditions
                    matching = True
                    workflows = self.get_pipeline_workflows(pipeline["id"])
                    if containing_workflows:
                        matching = matching and all(
                            name in [w["name"] for w in workflows] for name in containing_workflows
                        )
                    if not_containing_workflows:
                        matching = matching and all(
                            name not in [w["name"] for w in workflows]
                            for name in not_containing_workflows
                        )
                    if successful_only:
                        workflows_to_check = (
                            workflows
                            if not containing_workflows
                            else [w for w in workflows if w["name"] in containing_workflows]
                        )
                        matching = matching and all(
                            w["status"] == "success" for w in workflows_to_check
                        )
                    if matching:
                        retrieved_pipelines.append(pipeline)
            else:
                retrieved_pipelines.extend(filtered_pipelines)

            # Check if we have to stop or if more pages are needed
            stopping = found_stopping_pipeline or not response["next_page_token"] or not multipage
            # If next_page_token is returned, remove all other params
            params = {"page-token": response["next_page_token"]}

        return retrieved_pipelines

    @staticmethod
    def filter_pipelines(
        pipelines: Iterable[Dict[str, Any]], stopping_pipeline_id: Optional[str] = None
    ) -> FilteredPipelines:
        """Filter a list of CircleCI pipelines. If stopping_pipeline_id is specified, only return
        the pipelines that appear before it in the list; otherwise, all pipelines are returned.

        For more information about the data format of the method return value, see
        https://circleci.com/docs/api/v2/#get-all-pipelines

        :param pipelines: a list of pipelines.
        :param stopping_pipeline_id: the identifier of the first pipeline that should not be
        returned.
        :return: a list of pipelines.
        """
        filtered_pipelines = []
        found_stopping_pipeline = False

        for pipeline in pipelines:
            if stopping_pipeline_id and pipeline["id"] == stopping_pipeline_id:
                found_stopping_pipeline = True
                break
            filtered_pipelines.append(pipeline)

        return FilteredPipelines(filtered_pipelines, found_stopping_pipeline)

    def get_job_prs(self, job_number: str) -> Set[int]:
        """Get the set of pull request numbers associated with the specified job.

        :param job_number: the number of the job to be queried. Note that this is different from the
        job's id.
        :return: the set of pull request numbers associated with the specified job.
        """
        job_data = self._get(APIVersion.v11, f"project/{self.project_slug}/{job_number}")
        pull_requests = job_data.get("pull_requests", [])

        return set([int(pr["url"].split("pull/")[1]) for pr in pull_requests])

    def get_pipeline_config(self, pipeline_id: str) -> Dict[str, Any]:
        """Get the pipeline configuration (original and compiled).

        :param pipeline_id: the identifier of the pipeline.
        :return: the pipeline configuration (original and compiled).
        """
        return self._get(APIVersion.v20, f"pipeline/{pipeline_id}/config")

    def get_pipeline_workflows(self, pipeline_id: str) -> List[Dict[str, Any]]:
        """Return the pipeline workflows. It only returns the first page of workflows for each
        pipeline.

        :param pipeline_id: the identifier of the pipeline.
        :return: the pipeline workflows (only the first page).
        """
        return self._get(APIVersion.v20, f"pipeline/{pipeline_id}/workflow")["items"]

    def get_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Return the workflow data.

        :param workflow_id: the identifier of the workflow.
        :return: the data of the specified workflow.
        """
        return self._get(APIVersion.v20, f"workflow/{workflow_id}")

    def get_workflow_jobs(self, workflow_id: str) -> List[Dict[str, Any]]:
        """Return the workflow jobs. It only returns the first page of jobs for each workflow.

        :param workflow_id: the identifier of the workflow.
        :return: the workflow jobs (only the first page).
        """
        return self._get(APIVersion.v20, f"workflow/{workflow_id}/job")["items"]

    def get_workflow_prs(self, workflow_id: str) -> Set[int]:
        """Return the PRs associated with a workflow. By construction, all jobs of a given workflow
        share the same PRs, so the first job is picked for reference.

        :param workflow_id: the identifier of the workflow.
        :return: the set of pull request numbers associated with the specified workflow.
        """
        jobs = self.get_workflow_jobs(workflow_id)
        if not jobs:
            return set()

        return self.get_job_prs(jobs[0]["job_number"])

    def rerun_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Re-run the specified workflow.

        :param workflow_id: the identifier of the workflow.
        :return: the result of the re-run request.
        """
        return self._post(APIVersion.v20, f"workflow/{workflow_id}/rerun")

    def _perform_request(
        self, api_version: APIVersion, endpoint_url: str, method: Callable, **kwargs: Any
    ) -> Dict[str, Any]:
        """Perform an HTTPS request to the CircleCI API.

        :param endpoint_url: the relative url of the endpoint to be called, starting from the
        project slug.
        :param method: the requests function to be called on the specified endpoint. It can be a
        get, post, put, or delete operation.
        :param kwargs: a dictionary of additional named parameters to be passed to the method
        function.
        :return: the result of the API call.
        """
        try:
            url = f"{API_URL}/v{api_version.value}/{endpoint_url}"
            if "headers" not in kwargs:
                kwargs["headers"] = {}

            # API v1 and v2 have different authentication methods
            if api_version == APIVersion.v11:
                kwargs["auth"] = HTTPBasicAuth(username=self.api_token, password="")
            else:
                kwargs["headers"].update({"Circle-Token": self.api_token})

            if "data" in kwargs:
                kwargs["data"] = json.dumps(kwargs["data"])

            kwargs["headers"].update({"Content-type": "application/json"})

            result = method(url, **kwargs)

            if result.status_code > 299:
                raise Exception(f"Unable to contact CircleCI. Status code: {result.status_code}")

            return result.json()
        except ConnectionError:
            raise Exception(f"Unable to contact CircleCI (connection error).")
        except Timeout:
            raise Exception(f"Unable to contact CircleCI (connection timeout).")

    def _get(self, api_version: APIVersion, endpoint_url: str, **kwargs) -> Dict[str, Any]:
        """Perform a GET operation on the CircleCI API.

        :param api_version: the version of the CircleCI API to use.
        :param endpoint_url: the relative url of the endpoint to be called, starting from the
        project slug.
        :param kwargs: a dictionary of additional named parameters to be passed to the get function.
        :return: the result of the API call.
        """
        return self._perform_request(api_version, endpoint_url, requests.get, **kwargs)

    def _post(self, api_version: APIVersion, endpoint_url: str, **kwargs) -> Dict[str, Any]:
        """Perform a POST operation on the CircleCI API.

        :param api_version: the version of the CircleCI API to use.
        :param endpoint_url: the relative url of the endpoint to be called, starting from the
        project slug.
        :param kwargs: a dictionary of additional named parameters to be passed to the post
        function.
        :return: the result of the API call.
        """
        return self._perform_request(api_version, endpoint_url, requests.post, **kwargs)
