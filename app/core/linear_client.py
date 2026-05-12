"""Thin Linear GraphQL client backed by stdlib urllib.

Requires LINEAR_API_KEY in the environment (or passed at construction time).
No third-party HTTP dependencies — uses only urllib.request from stdlib.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, cast

_LINEAR_API_URL = "https://api.linear.app/graphql"

DONE_STATE_TYPES = frozenset({"completed", "cancelled"})

_RETRY_DELAYS = (1, 2, 4)
_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503})

logger = logging.getLogger(__name__)


class LinearError(Exception):
    pass


def _classify_transient_or_raise(exc: Exception) -> Exception:
    """Return *exc* if it's a retryable transport error; raise LinearError if not.

    Non-retryable HTTPError codes (anything outside _RETRYABLE_HTTP_CODES) are
    promoted to LinearError. URLError and RemoteDisconnected are always
    retryable.
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code not in _RETRYABLE_HTTP_CODES:
            raise LinearError(f"Linear API HTTP {exc.code}: {exc.reason}") from exc
    return exc


class LinearClient:
    """Minimal Linear GraphQL client for watcher use."""

    def __init__(self, api_key: str | None = None, team: str = "Work") -> None:
        key = api_key or os.environ.get("LINEAR_API_KEY", "")
        if not key:
            raise LinearError("LINEAR_API_KEY environment variable not set")
        self._api_key = key
        self._team = team
        self._state_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def list_ready_for_local(self) -> list[dict[str, Any]]:
        """Return issues whose workflow state is 'ReadyForLocal'."""
        return self.list_issues_by_state("ReadyForLocal")

    def list_issues_by_state(self, state_name: str) -> list[dict[str, Any]]:
        """Return issues whose workflow state matches *state_name*."""
        data = self._query(
            """
            query ListByState($teamName: String!, $stateName: String!) {
              issues(
                filter: {
                  team: { name: { eq: $teamName } }
                  state: { name: { eq: $stateName } }
                }
                first: 100
              ) {
                nodes {
                  id
                  identifier
                  title
                  parent { id }
                }
              }
            }
            """,
            {"teamName": self._team, "stateName": state_name},
        )
        return cast(list[dict[str, Any]], data["issues"]["nodes"])

    def get_open_blockers(self, issue_id: str) -> list[str]:
        """Return identifiers of issues that block *issue_id* and are not yet done."""
        data = self._query(
            """
            query GetBlockers($id: String!) {
              issue(id: $id) {
                relations {
                  nodes {
                    type
                    relatedIssue {
                      identifier
                      state { type }
                    }
                  }
                }
              }
            }
            """,
            {"id": issue_id},
        )
        issue = data.get("issue")
        if issue is None:
            return []
        return [
            node["relatedIssue"]["identifier"]
            for node in issue["relations"]["nodes"]
            if node["type"] == "blocked_by"
            and node["relatedIssue"]["state"]["type"] not in DONE_STATE_TYPES
        ]

    def set_state(self, issue_id: str, state_name: str) -> None:
        """Move *issue_id* to the workflow state with the given name."""
        state_id = self._resolve_state_id(state_name)
        data = self._query(
            """
            mutation SetState($issueId: String!, $stateId: String!) {
              issueUpdate(id: $issueId, input: { stateId: $stateId }) {
                success
              }
            }
            """,
            {"issueId": issue_id, "stateId": state_id},
        )
        self._check_success(data, "issueUpdate", issue_id)

    def post_comment(self, issue_id: str, body: str) -> None:
        """Post a comment on *issue_id*."""
        data = self._query(
            """
            mutation CreateComment($issueId: String!, $body: String!) {
              commentCreate(input: { issueId: $issueId, body: $body }) {
                success
              }
            }
            """,
            {"issueId": issue_id, "body": body},
        )
        self._check_success(data, "commentCreate", issue_id)

    def get_issue_state_type(self, identifier: str) -> str | None:
        """Return the Linear state.type for a ticket by its human identifier.

        Returns one of: 'triage', 'backlog', 'unstarted', 'started',
        'completed', 'cancelled'. Returns None if not found.
        """
        data = self._query(
            """
            query GetIssueStateByIdentifier($identifier: String!) {
              issue(id: $identifier) {
                state { type }
              }
            }
            """,
            {"identifier": identifier},
        )
        issue = data.get("issue")
        if issue is None:
            return None
        return cast(str, issue["state"]["type"])

    def get_issue(self, identifier: str) -> dict[str, Any]:
        """Return full issue data for a ticket by its human identifier.

        Returns a dict with `id`, `identifier`, `title`, and `state` (with
        nested `name`, `type`, `createdAt`). Raises LinearError if the
        issue is not found.
        """
        data = self._query(
            """
            query GetIssueByIdentifier($identifier: String!) {
              issue(id: $identifier) {
                id
                identifier
                title
                state {
                  name
                  type
                  createdAt
                }
              }
            }
            """,
            {"identifier": identifier},
        )
        issue = data.get("issue")
        if issue is None:
            raise LinearError(f"Issue {identifier!r} not found")
        return cast(dict[str, Any], issue)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_success(
        self, data: dict[str, Any], mutation_key: str, issue_id: str
    ) -> None:
        if not data[mutation_key]["success"]:
            raise LinearError(
                f"{mutation_key} returned success=false for issue {issue_id!r}"
            )

    def _resolve_state_id(self, state_name: str) -> str:
        if state_name in self._state_cache:
            return self._state_cache[state_name]

        data = self._query(
            """
            query WorkflowStates($teamName: String!) {
              teams(filter: { name: { eq: $teamName } }) {
                nodes {
                  states { nodes { id name } }
                }
              }
            }
            """,
            {"teamName": self._team},
        )
        teams = data["teams"]["nodes"]
        if not teams:
            raise LinearError(f"Team {self._team!r} not found")
        for state in teams[0]["states"]["nodes"]:
            self._state_cache[state["name"]] = state["id"]

        if state_name not in self._state_cache:
            raise LinearError(
                f"Workflow state {state_name!r} not found in team {self._team!r}"
            )
        return self._state_cache[state_name]

    def _query(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        req = self._build_request(query, variables)
        last_exc: Exception | None = None
        max_retries = len(_RETRY_DELAYS)
        for attempt in range(max_retries + 1):
            try:
                return self._attempt_query(req)
            except (urllib.error.URLError, http.client.RemoteDisconnected) as exc:
                last_exc = _classify_transient_or_raise(exc)
            if attempt < max_retries:
                delay = _RETRY_DELAYS[attempt]
                logger.warning(
                    "Linear API transient error on attempt %d/%d — retrying in %ds: %s",
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    last_exc,
                )
                time.sleep(delay)
        raise LinearError(
            f"Linear API failed after {max_retries + 1} attempts: {last_exc}"
        ) from last_exc

    def _build_request(
        self, query: str, variables: dict[str, Any] | None
    ) -> urllib.request.Request:
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        return urllib.request.Request(  # nosec B310
            _LINEAR_API_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._api_key,
            },
            method="POST",
        )

    def _attempt_query(self, req: urllib.request.Request) -> dict[str, Any]:
        """Issue one request and parse the response.

        Raises LinearError on a structural error (GraphQL errors or no data).
        Raises HTTPError/URLError/RemoteDisconnected on transport failures
        (the caller decides whether to retry).
        """
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            body: dict[str, Any] = json.loads(resp.read())
        errors = body.get("errors")
        if errors:
            raise LinearError(f"Linear API error: {errors}")
        data = body.get("data")
        if data is None:
            raise LinearError(f"Linear API returned no data: {body!r}")
        return cast(dict[str, Any], data)
