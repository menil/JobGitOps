"""GitHub API client wrapper for JobGitOps."""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class GitHubClientError(Exception):
    """Raised when a GitHub API operation fails."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        """Initialize the GitHub client error.

        Args:
            message: The exception message.
            status_code: The HTTP status code if available.
            response_body: The raw HTTP response body if available.
        """
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def extract_label_names(labels_raw: list) -> list[str]:
    """Extract label names from a GitHub issue's labels field.

    Filters out malformed entries so callers can trust the result contains
    only usable label name strings.

    Args:
        labels_raw: The raw ``labels`` array from an issue object or event.

    Returns:
        List of label name strings.
    """
    return [
        label["name"]
        for label in labels_raw
        if isinstance(label, dict) and "name" in label
    ]


class GitHubClient:
    """Client for interacting with the GitHub REST and GraphQL APIs."""

    def __init__(
        self,
        token: str,
        repo: str,
        project_id: str | None = None,
        status_field_name: str = "Status",
        timeout: int = 10,
    ) -> None:
        """Initialize the GitHub client.

        Args:
            token: GitHub Personal Access Token or installation token.
            repo: GitHub repository in the format "owner/repo".
            project_id: Optional GitHub Projects V2 node ID.
            status_field_name: Name of the Projects V2 single-select field
                for column status.
            timeout: Network request timeout in seconds.

        Raises:
            ValueError: If the repository identifier format is invalid.
        """
        if not re.match(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", repo):
            raise ValueError(
                f"Invalid repository format: {repo}. Expected 'owner/repo'."
            )

        self.token = token
        self.repo = repo
        self.project_id = project_id
        self.status_field_name = status_field_name
        self.timeout = timeout

        # Cache project field IDs and option mapping to save network round-trips.
        # Keyed by (project_id, status_field_name) ->
        # (field_id, {option_name: option_id})
        self._project_fields_cache: dict[
            tuple[str, str], tuple[str, dict[str, str]]
        ] = {}

    def __repr__(self) -> str:
        """Provide a secure string representation redacting the API token.

        Returns:
            Redacted string representation.
        """
        return f"<GitHubClient repo={self.repo!r} token='***'>"

    def _request(
        self,
        method: str,
        url: str,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send an HTTP request to the GitHub API.

        Args:
            method: HTTP method (GET, POST, PATCH, DELETE, etc.).
            url: The request URL.
            data: Optional JSON body.
            headers: Optional HTTP headers to merge.

        Returns:
            The parsed JSON response or None.

        Raises:
            GitHubClientError: If the API request fails.
        """
        req_headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "JobGitOps",
        }
        if headers:
            # Normalize headers by updating base headers dict
            req_headers.update(headers)

        req_data = None
        if data is not None:
            req_data = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            url, data=req_data, headers=req_headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_data = resp.read().decode("utf-8")
                return json.loads(resp_data) if resp_data else {}
        except urllib.error.HTTPError as e:
            try:
                body_bytes = e.read()
                body = body_bytes.decode("utf-8")
            except Exception:
                body = "No detailed body"
            finally:
                # Explicitly close the error stream to prevent resource leakage.
                e.close()
            raise GitHubClientError(
                f"GitHub API request failed: {e.code} {e.reason}. Detail: {body}",
                status_code=e.code,
                response_body=body,
            ) from e
        except Exception as e:
            raise GitHubClientError(f"GitHub API communication failed: {e}") from e

    def _graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Perform a GitHub GraphQL API request.

        Args:
            query: The GraphQL query/mutation string.
            variables: Optional variables mapping.

        Returns:
            The parsed GraphQL response dictionary.
        """
        url = "https://api.github.com/graphql"
        data: dict[str, Any] = {"query": query}
        if variables:
            data["variables"] = variables
        return self._request("POST", url, data=data)

    def post_comment(self, issue_number: int, body: str) -> dict[str, Any]:
        """Post a comment to a GitHub issue.

        Args:
            issue_number: The number of the issue.
            body: The markdown body of the comment.

        Returns:
            The created comment object from the API.
        """
        url = f"https://api.github.com/repos/{self.repo}/issues/{issue_number}/comments"
        res = self._request("POST", url, data={"body": body})
        if not isinstance(res, dict):
            raise GitHubClientError(f"Unexpected response format: {res}")
        return res

    def add_labels(self, issue_number: int, labels: list[str]) -> list[str]:
        """Add one or more labels to a GitHub issue.

        Args:
            issue_number: The number of the issue.
            labels: List of label names to add.

        Returns:
            List of all label names currently on the issue.
        """
        url = f"https://api.github.com/repos/{self.repo}/issues/{issue_number}/labels"
        res = self._request("POST", url, data={"labels": labels})
        if not isinstance(res, list):
            raise GitHubClientError(f"Unexpected response format: {res}")
        return [
            label["name"]
            for label in res
            if isinstance(label, dict) and "name" in label
        ]

    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from a GitHub issue.

        Silently ignores label-not-found (HTTP 404) errors to ensure idempotency.

        Args:
            issue_number: The number of the issue.
            label: The name of the label to remove.
        """
        # Pass safe="" to urllib.parse.quote to ensure slashes are properly escaped.
        quoted_label = urllib.parse.quote(label, safe="")
        url = f"https://api.github.com/repos/{self.repo}/issues/{issue_number}/labels/{quoted_label}"
        try:
            self._request("DELETE", url)
        except GitHubClientError as e:
            if e.status_code == 404:
                # The label was not present on the issue; return cleanly.
                return
            raise

    def close_issue(self, issue_number: int) -> dict[str, Any]:
        """Close a GitHub issue.

        Args:
            issue_number: The number of the issue.

        Returns:
            The updated issue object from the API.
        """
        url = f"https://api.github.com/repos/{self.repo}/issues/{issue_number}"
        res = self._request("PATCH", url, data={"state": "closed"})
        if not isinstance(res, dict):
            raise GitHubClientError(f"Unexpected response format: {res}")
        return res

    def get_issue(self, issue_number: int) -> dict[str, Any]:
        """Get a single issue from the repository.

        Args:
            issue_number: The number of the issue.

        Returns:
            The issue details dictionary.
        """
        url = f"https://api.github.com/repos/{self.repo}/issues/{issue_number}"
        res = self._request("GET", url)
        if not isinstance(res, dict):
            raise GitHubClientError(f"Unexpected response format: {res}")
        return res

    def list_comments(
        self,
        issue_number: int,
        per_page: int = 100,
        page: int | None = None,
    ) -> list[dict[str, Any]]:
        """List comments on a GitHub issue.

        Args:
            issue_number: The number of the issue.
            per_page: Number of comments to retrieve per page (max 100).
            page: Optional page number for pagination.

        Returns:
            List of comment objects from the API.
        """
        params: dict[str, Any] = {"per_page": per_page}
        if page is not None:
            params["page"] = page
        query = urllib.parse.urlencode(params)
        url = f"https://api.github.com/repos/{self.repo}/issues/{issue_number}/comments?{query}"
        res = self._request("GET", url)
        if not isinstance(res, list):
            raise GitHubClientError(f"Unexpected response format: {res}")
        return res

    def get_labels(self, issue_number: int) -> list[str]:
        """Get the names of all labels on a GitHub issue.

        Convenience wrapper around `get_issue` for the comment flow's guard.

        Args:
            issue_number: The number of the issue.

        Returns:
            List of label names on the issue.
        """
        issue = self.get_issue(issue_number)
        return extract_label_names(issue.get("labels", []))

    def list_issues(
        self,
        state: str = "all",
        per_page: int = 100,
        page: int | None = None,
        labels: str | None = None,
    ) -> list[dict[str, Any]]:
        """List issues for the repository.

        Args:
            state: State of issues to list ('open', 'closed', or 'all').
            per_page: Number of issues to retrieve per page (max 100).
            page: Optional page number for pagination.
            labels: Optional comma-separated label names to filter by.

        Returns:
            List of issues.
        """
        url = f"https://api.github.com/repos/{self.repo}/issues?state={state}&per_page={per_page}"
        if labels:
            url += f"&labels={urllib.parse.quote(labels)}"
        if page is not None:
            url += f"&page={page}"
        res = self._request("GET", url)
        if not isinstance(res, list):
            raise GitHubClientError(f"Unexpected response format: {res}")
        return res

    def create_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any]:
        """Create a new issue in the repository.

        Args:
            title: Title of the issue.
            body: Markdown body description of the issue.
            labels: Optional list of labels to assign.

        Returns:
            The created issue object from the API.
        """
        url = f"https://api.github.com/repos/{self.repo}/issues"
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        res = self._request("POST", url, data=payload)
        if not isinstance(res, dict):
            raise GitHubClientError(f"Unexpected response format: {res}")
        return res

    def update_project_status(self, issue_node_id: str, status_name: str) -> None:
        """Add the issue to the Project V2 and transition its status.

        Does nothing if project_id is not set.

        Args:
            issue_node_id: The GraphQL node ID of the issue.
            status_name: The name of the target status column/option.

        Raises:
            GitHubClientError: If any GraphQL operations fail.
        """
        if not self.project_id:
            return

        # 1. Add/retrieve the item in the project
        add_mutation = """
        mutation AddProjectV2Item($projectId: ID!, $contentId: ID!) {
          addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
            item {
              id
            }
          }
        }
        """
        res = self._graphql(
            add_mutation,
            {"projectId": self.project_id, "contentId": issue_node_id},
        )
        if "errors" in res:
            raise GitHubClientError(
                f"GraphQL error adding item to project: {res['errors']}"
            )

        data = res.get("data") or {}
        add_res = data.get("addProjectV2ItemById") or {}
        item = add_res.get("item") or {}
        item_id = item.get("id")
        if not item_id:
            raise GitHubClientError("Failed to add or retrieve item ID from project.")

        # 2. Get status field ID and option ID
        cache_key = (self.project_id, self.status_field_name)
        if cache_key in self._project_fields_cache:
            field_id, options_map = self._project_fields_cache[cache_key]
        else:
            fields_query = """
            query GetProjectFields($projectId: ID!) {
              node(id: $projectId) {
                ... on ProjectV2 {
                  fields(first: 100) {
                    nodes {
                      ... on ProjectV2SingleSelectField {
                        id
                        name
                        options {
                          id
                          name
                        }
                      }
                    }
                  }
                }
              }
            }
            """
            fields_res = self._graphql(fields_query, {"projectId": self.project_id})
            if "errors" in fields_res:
                raise GitHubClientError(
                    f"GraphQL error fetching project fields: {fields_res['errors']}"
                )

            data = fields_res.get("data") or {}
            node = data.get("node") or {}
            fields_container = node.get("fields") or {}
            fields = fields_container.get("nodes") or []

            status_field = None
            for f in fields:
                if isinstance(f, dict) and f.get("name") == self.status_field_name:
                    status_field = f
                    break

            if not status_field:
                raise GitHubClientError(
                    f"Status field '{self.status_field_name}' not found in project."
                )

            field_id = status_field.get("id")
            options = status_field.get("options") or []

            options_map = {}
            for opt in options:
                if isinstance(opt, dict) and "name" in opt and "id" in opt:
                    options_map[opt["name"]] = opt["id"]

            self._project_fields_cache[cache_key] = (field_id, options_map)

        option_id = options_map.get(status_name)
        if not option_id:
            raise GitHubClientError(
                f"Status option '{status_name}' not found in status field options."
            )

        # 3. Update the field value
        update_mutation = """
        mutation UpdateProjectV2ItemFieldValue(
          $projectId: ID!,
          $itemId: ID!,
          $fieldId: ID!,
          $optionId: String!
        ) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $projectId,
            itemId: $itemId,
            fieldId: $fieldId,
            value: {
              singleSelectOptionId: $optionId
            }
          }) {
            projectV2Item {
              id
            }
          }
        }
        """
        update_res = self._graphql(
            update_mutation,
            {
                "projectId": self.project_id,
                "itemId": item_id,
                "fieldId": field_id,
                "optionId": option_id,
            },
        )
        if "errors" in update_res:
            raise GitHubClientError(
                f"GraphQL error updating project status: {update_res['errors']}"
            )
