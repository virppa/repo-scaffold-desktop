"""Thin Linear GraphQL client backed by stdlib urllib.

Requires LINEAR_API_KEY in the environment (or passed at construction time).
No third-party HTTP dependencies — uses only urllib.request from stdlib.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

_LINEAR_API_URL = "https://api.linear.app/graphql"

_DONE_STATE_TYPES = frozenset({"completed", "cancelled"})


class LinearError(Exception):
    pass


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
        data = self._query(
            """
            query ListReadyForLocal($teamName: String!, $stateName: String!) {
              issues(
                filter: {
                  team: { name: { eq: $teamName } }
                  state: { name: { eq: $stateName } }
                }
                first: 50
              ) {
                nodes {
                  id
                  identifier
                  title
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
            }
            """,
            {"teamName": self._team, "stateName": "ReadyForLocal"},
        )
        return data["issues"]["nodes"]

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
            and node["relatedIssue"]["state"]["type"] not in _DONE_STATE_TYPES
        ]

    def set_state(self, issue_id: str, state_name: str) -> None:
        """Move *issue_id* to the workflow state with the given name."""
        state_id = self._resolve_state_id(state_name)
        self._query(
            """
            mutation SetState($issueId: String!, $stateId: String!) {
              issueUpdate(id: $issueId, input: { stateId: $stateId }) {
                success
              }
            }
            """,
            {"issueId": issue_id, "stateId": state_id},
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(  # nosec B310
            _LINEAR_API_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            body = json.loads(resp.read())
        if "errors" in body:
            raise LinearError(f"Linear API error: {body['errors']}")
        return body["data"]  # type: ignore[no-any-return]
