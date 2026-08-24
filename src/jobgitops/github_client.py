"""GitHub API client wrapper for JobGitOps."""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("jobgitops.github_client")


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


class GitHubProjectPermissionError(GitHubClientError):
    """Raised when a Projects V2 operation fails due to insufficient permissions."""


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
        project_token: str | None = None,
    ) -> None:
        """Initialize the GitHub client.

        Args:
            token: GitHub Personal Access Token or installation token.
            repo: GitHub repository in the format "owner/repo".
            project_id: Optional GitHub Projects V2 node ID.
            status_field_name: Name of the Projects V2 single-select field
                for column status.
            timeout: Network request timeout in seconds.
            project_token: Optional separate token for Projects V2 mutations.

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
        self.project_token = project_token or os.environ.get("GH_PAT")

        # Number of attempts for transient failures (429/5xx), including the
        # initial try. Kept as an instance attribute so tests can shrink it.
        self._max_retries = 3
        self._retryable_status_codes = (429, 500, 502, 503, 504)

        # Cache project field IDs and option mapping to save network round-trips.
        # Keyed by (project_id, status_field_name) -> (field_id,
        # {option_name: {"id", "color", "description"}})
        self._project_fields_cache: dict[
            tuple[str, str], tuple[str, dict[str, dict[str, str]]]
        ] = {}

    def _retry_delay(self, error: "urllib.error.HTTPError", attempt: int) -> float:
        """Compute the backoff delay before retrying a transient failure.

        Honors the server's ``Retry-After`` header when present (seconds);
        otherwise falls back to exponential backoff capped at 8 seconds.
        """
        retry_after = None
        if error.headers is not None:
            retry_after = error.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except (TypeError, ValueError):
                pass
        return float(min(2**attempt, 8))

    def _read_error_body(self, error: "urllib.error.HTTPError") -> str:
        """Read and decode an HTTP error response body, tolerating failures."""
        try:
            body_bytes = error.read()
            return body_bytes.decode("utf-8")
        except Exception:
            return "No detailed body"
        finally:
            # Explicitly close the error stream to prevent resource leakage.
            error.close()

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

        # Transient HTTP failures (rate limits and server hiccups) are retried
        # with backoff so one 429 does not permanently drop a webhook event.
        # Anything else is surfaced immediately to the caller.
        for attempt in range(self._max_retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp_data = resp.read().decode("utf-8")
                    return json.loads(resp_data) if resp_data else {}
            except urllib.error.HTTPError as e:
                if e.code not in self._retryable_status_codes or (
                    attempt == self._max_retries - 1
                ):
                    body = self._read_error_body(e)
                    raise GitHubClientError(
                        f"GitHub API request failed: {e.code} {e.reason}. "
                        f"Detail: {body}",
                        status_code=e.code,
                        response_body=body,
                    ) from e
                delay = self._retry_delay(e, attempt)
                e.close()
                time.sleep(delay)
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

        headers = {}
        if self.project_token:
            headers["Authorization"] = f"Bearer {self.project_token}"
        return self._request("POST", url, data=data, headers=headers)

    def _raise_graphql_error(self, errors: list[Any], context: str) -> None:
        """Helper to inspect GraphQL errors and raise the appropriate exception.

        Args:
            errors: List of error dictionaries returned by the GraphQL endpoint.
            context: Description of the action (e.g. "adding item to project").

        Raises:
            GitHubProjectPermissionError: If the error represents a permission
                failure (FORBIDDEN / Resource not accessible).
            GitHubClientError: For all other GraphQL errors.
        """
        is_forbidden = any(
            isinstance(err, dict)
            and (
                err.get("type") == "FORBIDDEN"
                or "Resource not accessible" in err.get("message", "")
            )
            for err in errors
        )
        if is_forbidden:
            raise GitHubProjectPermissionError(
                f"GraphQL project permission denied: {errors}"
            )
        raise GitHubClientError(f"GraphQL error {context}: {errors}")

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

    def update_issue_title(self, issue_number: int, title: str) -> dict[str, Any]:
        """Update the title of a GitHub issue.

        Args:
            issue_number: The number of the issue.
            title: The new title for the issue.

        Returns:
            The updated issue object from the API.
        """
        url = f"https://api.github.com/repos/{self.repo}/issues/{issue_number}"
        res = self._request("PATCH", url, data={"title": title})
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

    def resolve_issue_number(self, node_id: str) -> int:
        """Resolve a GraphQL node ID to the number of its linked issue.

        Webhook events that carry Projects V2 item data (``projects_v2_item``)
        expose the issue only as a GraphQL ``content_node_id``, never as a
        numeric issue number. This bridges that gap so REST label/comment calls
        can be made against the resolved issue.

        Args:
            node_id: The GraphQL node ID of an issue.

        Returns:
            The numeric issue number.

        Raises:
            GitHubClientError: If the node cannot be resolved to an issue or
                the GraphQL request fails.
        """
        query = """
        query ResolveIssueNumber($nodeId: ID!) {
          node(id: $nodeId) {
            ... on Issue {
              number
            }
          }
        }
        """
        res = self._graphql(query, {"nodeId": node_id})
        if "errors" in res:
            self._raise_graphql_error(res["errors"], "resolving issue node ID")

        node = (res.get("data") or {}).get("node") or {}
        number = node.get("number")
        if number is None:
            raise GitHubClientError(
                f"Node ID {node_id!r} does not resolve to an issue."
            )
        return int(number)

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

    def update_project_status(self, issue_node_id: str, status_name: str) -> bool:
        """Add the issue to the Project V2 and transition its status.

        Does nothing if project_id is not set.

        Args:
            issue_node_id: The GraphQL node ID of the issue.
            status_name: The name of the target status column/option.

        Returns:
            True when the column move was applied (or the project is
            unconfigured); False when Projects V2 rejected the write due to
            permissions and tracking degraded to label-only. Callers that
            must distinguish "written" from "degraded" check this value;
            ignoring it preserves the old fire-and-forget behavior.

        Raises:
            GitHubClientError: If any non-permission GraphQL operation fails.
        """
        if not self.project_id:
            return True

        try:
            # 1. Add/retrieve the item in the project
            add_mutation = """
            mutation AddProjectV2Item($projectId: ID!, $contentId: ID!) {
              addProjectV2ItemById(input: {
                projectId: $projectId,
                contentId: $contentId
              }) {
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
                self._raise_graphql_error(res["errors"], "adding item to project")

            data = res.get("data") or {}
            add_res = data.get("addProjectV2ItemById") or {}
            item = add_res.get("item") or {}
            item_id = item.get("id")
            if not item_id:
                raise GitHubClientError(
                    "Failed to add or retrieve item ID from project."
                )

            # 2. Get status field ID and option ID
            field_id, options_map = self._resolve_status_field()

            option_id = (options_map.get(status_name) or {}).get("id")
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
                self._raise_graphql_error(
                    update_res["errors"], "updating project status"
                )
        except GitHubProjectPermissionError as e:
            logger.warning(
                "GitHub Projects V2 permission denied: %s. "
                "Degrading to label-only tracking.",
                e,
            )
            return False
        return True

    def get_project_item_status(self, issue_node_id: str) -> str | None:
        """Return the issue's current Status column name on the project.

        Looks the value up through the issue's project memberships and filters
        to this client's project and status field, so issues shared across
        several projects resolve to the column this pipeline manages.

        Returns:
            The selected option name, or None when the project is
            unconfigured, the issue is not on the board, or no status value
            is set.

        Args:
            issue_node_id: The GraphQL node ID of the issue.

        Raises:
            GitHubClientError: If the GraphQL query fails.
        """
        if not self.project_id:
            return None

        query = """
        query GetIssueProjectStatus($nodeId: ID!) {
          node(id: $nodeId) {
            ... on Issue {
              projectItems(first: 10) {
                nodes {
                  project { id }
                  fieldValues(first: 20) {
                    nodes {
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        name
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        res = self._graphql(query, {"nodeId": issue_node_id})
        if "errors" in res:
            self._raise_graphql_error(res["errors"], "reading project item status")

        issue = (res.get("data") or {}).get("node") or {}
        items = ((issue.get("projectItems") or {}).get("nodes")) or []
        for item in items:
            if not isinstance(item, dict):
                continue
            project = item.get("project") or {}
            if project.get("id") != self.project_id:
                continue
            values = ((item.get("fieldValues") or {}).get("nodes")) or []
            for value in values:
                if not isinstance(value, dict):
                    continue
                field = value.get("field") or {}
                if field.get("name") == self.status_field_name:
                    name = value.get("name")
                    return name if isinstance(name, str) else None
        return None

    def ensure_project_status(
        self,
        issue_node_id: str,
        status_name: str,
        *,
        settle_seconds: float = 5.0,
        attempts: int = 3,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> bool:
        """Re-assert a column move after competing project automations settle.

        Closing an issue triggers GitHub's built-in "item closed -> Done"
        workflow about a second after our own write lands, so whoever writes
        last wins. This waits out that window, then verifies the column and
        rewrites it when something flipped it — turning a lost race into a
        seconds-long blip instead of up-to-30-minutes of wrong state.

        Never raises: the scheduled backfill remains the convergence net for
        any failure here, so close paths stay resilient. Non-convergence and
        permission-degraded writes are logged as warnings so operators can
        see a lost race without grepping debug output.

        Args:
            issue_node_id: The GraphQL node ID of the issue.
            status_name: The Status option that should be showing.
            settle_seconds: How long to wait for external automations before
                each verification.
            attempts: Maximum verify-and-rewrite rounds.
            _sleep: Sleep function, injectable for tests.

        Returns:
            True when the column was verified holding ``status_name``; also
            True immediately when no project is configured (nothing to do).
        """
        if not self.project_id:
            return True

        _sleep(settle_seconds)
        for attempt in range(attempts):
            try:
                current = self.get_project_item_status(issue_node_id)
            except GitHubClientError as e:
                logger.warning(
                    "Status re-assert read failed for node %s: %s", issue_node_id, e
                )
                current = None
            if current == status_name:
                return True
            logger.info(
                "Re-asserting project status '%s' (found '%s', attempt %d/%d).",
                status_name,
                current,
                attempt + 1,
                attempts,
            )
            try:
                updated = self.update_project_status(issue_node_id, status_name)
            except GitHubClientError as e:
                logger.warning(
                    "Status re-assert write failed for node %s: %s", issue_node_id, e
                )
                return False
            if not updated:
                # update_project_status already warned; retrying cannot help
                # while the token lacks Projects V2 scopes.
                logger.warning(
                    "Status re-assert skipped for node %s: project writes "
                    "unavailable (label-only tracking).",
                    issue_node_id,
                )
                return False
            if attempt < attempts - 1:
                _sleep(settle_seconds)
        logger.warning(
            "Status re-assert did not converge to '%s' after %d attempts for "
            "node %s; the scheduled backfill will reconcile the column.",
            status_name,
            attempts,
            issue_node_id,
        )
        return False

    def _resolve_status_field(self) -> tuple[str, dict[str, str]]:
        """Resolve the status field ID and its option-name-to-ID map.

        Cached by (project_id, status_field_name) so repeated column moves and
        option syncs share one field-fetch round-trip.

        Returns:
            A tuple of (field_id, {option_name: option_id}).

        Raises:
            GitHubClientError: If the project is unconfigured or the status
                field cannot be found.
        """
        if not self.project_id:
            raise GitHubClientError("project_id is not configured.")

        cache_key = (self.project_id, self.status_field_name)
        if cache_key in self._project_fields_cache:
            return self._project_fields_cache[cache_key]

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
                      color
                      description
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
            self._raise_graphql_error(fields_res["errors"], "fetching project fields")

        node = fields_res.get("data") or {}
        node = node.get("node")
        if node is None:
            raise GitHubClientError(
                f"Project V2 '{self.project_id}' not found. "
                "Check projects_v2.project_id in config/settings.yaml."
            )
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
                options_map[opt["name"]] = {
                    "id": opt["id"],
                    "color": opt.get("color"),
                    "description": opt.get("description"),
                }

        self._project_fields_cache[cache_key] = (field_id, options_map)
        return field_id, options_map

    def get_status_field_options(self) -> list[str]:
        """Return the current Status field option names, in board order.

        Returns an empty list when no project is configured.
        """
        if not self.project_id:
            return []
        _, options_map = self._resolve_status_field()
        return list(options_map.keys())

    def update_status_field_options(self, option_names: list[str]) -> None:
        """Replace the status field's single-select options with the given names.

        Existing options keep their IDs, colors, and descriptions; new names are
        added with a default color and blank description. GitHub requires every
        option to carry a color and description (with no server-side default),
        and rejects removing options that are still in use, so callers should
        add missing options, move items onto them, and only then prune old ones.

        Args:
            option_names: The full desired option-name list.

        Raises:
            GitHubClientError: If the GraphQL mutation fails (including when a
                removed option is still in use).
        """
        if not self.project_id:
            return

        # Rotate through the palette so new columns are visually distinct from
        # their neighbours instead of all rendering in the same gray.
        palette = ("BLUE", "GREEN", "YELLOW", "ORANGE", "PURPLE", "PINK", "RED")
        field_id, options_map = self._resolve_status_field()
        options = []
        for index, name in enumerate(option_names):
            existing = options_map.get(name) or {}
            options.append(
                {
                    "name": name,
                    **({"id": existing["id"]} if existing.get("id") else {}),
                    "color": existing.get("color") or palette[index % len(palette)],
                    "description": existing.get("description") or "",
                }
            )

        mutation = """
        mutation UpdateProjectV2FieldOptions(
          $fieldId: ID!,
          $options: [ProjectV2SingleSelectFieldOptionInput!]!
        ) {
          updateProjectV2Field(
            input: { fieldId: $fieldId, singleSelectOptions: $options }
          ) {
            projectV2Field {
              ... on ProjectV2FieldCommon {
                id
              }
            }
          }
        }
        """
        res = self._graphql(mutation, {"fieldId": field_id, "options": options})
        if "errors" in res:
            self._raise_graphql_error(res["errors"], "updating status field options")

        # The mutation changed the option set, so the cached option map is
        # stale; drop it so a later call in the same process re-fetches.
        self._project_fields_cache.pop((self.project_id, self.status_field_name), None)

    def list_project_items(self) -> dict[int, str | None]:
        """Map issue numbers to their current Status column for the project.

        Used by backfill to skip items already in the correct column and to
        reconcile the reverse (column -> label) direction without an API call
        per board card. Items that are not issues, or lack a Status value, are
        omitted (or mapped to ``None``) respectively.

        Only issue cards belonging to this repository (matching nameWithOwner)
        are returned to prevent status collisions when the project board tracks
        issues across multiple repositories.

        Returns:
            Mapping of issue number to Status option name (or ``None`` when the
            card has no Status value).

        Raises:
            GitHubClientError: If the GraphQL query fails.
        """
        if not self.project_id:
            return {}

        statuses: dict[int, str | None] = {}
        after: str | None = None

        while True:
            query = """
            query ProjectItems($projectId: ID!, $first: Int!, $after: String) {
              node(id: $projectId) {
                ... on ProjectV2 {
                  items(first: $first, after: $after) {
                    pageInfo {
                      hasNextPage
                      endCursor
                    }
                    nodes {
                      content {
                        __typename
                        ... on Issue {
                          number
                          repository {
                            nameWithOwner
                          }
                        }
                      }
                      fieldValues(first: 50) {
                        nodes {
                          ... on ProjectV2ItemFieldSingleSelectValue {
                            name
                            field {
                              ... on ProjectV2FieldCommon {
                                name
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """
            res = self._graphql(
                query,
                {"projectId": self.project_id, "first": 100, "after": after},
            )
            if "errors" in res:
                self._raise_graphql_error(res["errors"], "listing project items")

            node = (res.get("data") or {}).get("node") or {}
            items = ((node.get("items") or {}).get("nodes")) or []

            for item in items:
                content = item.get("content") or {}
                number = content.get("number")
                if content.get("__typename") != "Issue" or number is None:
                    continue
                # Filter by repository to prevent collisions/overwriting
                # if the board tracks multiple repos
                repo_name = (content.get("repository") or {}).get("nameWithOwner")
                if repo_name and repo_name.lower() != self.repo.lower():
                    continue
                status: str | None = None
                for field_value in ((item.get("fieldValues") or {}).get("nodes")) or []:
                    field = (field_value.get("field") or {}).get("name")
                    if field == self.status_field_name:
                        status = field_value.get("name")
                statuses[int(number)] = status

            page_info = ((node.get("items") or {}).get("pageInfo")) or {}
            if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
                break
            after = page_info["endCursor"]

        return statuses
