"""Tests for LinearClient — all network calls are mocked via urllib."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.core.linear_client import LinearClient, LinearError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(data: dict) -> MagicMock:
    body = json.dumps(data).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _client(api_key: str = "test-key") -> LinearClient:
    return LinearClient(api_key=api_key)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_raises_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(LinearError, match="LINEAR_API_KEY"):
        LinearClient()


def test_accepts_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "env-key")
    client = LinearClient()
    assert client._api_key == "env-key"  # pragma: allowlist secret


def test_constructor_arg_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "env-key")
    client = LinearClient(api_key="arg-key")
    assert client._api_key == "arg-key"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# list_ready_for_local
# ---------------------------------------------------------------------------


def test_list_ready_for_local_returns_nodes() -> None:
    nodes = [
        {
            "id": "abc",
            "identifier": "WOR-10",
            "title": "Test",
            "relations": {"nodes": []},
        }
    ]
    response = {"data": {"issues": {"nodes": nodes}}}

    with patch("urllib.request.urlopen", return_value=_mock_response(response)):
        result = _client().list_ready_for_local()

    assert result == nodes


def test_list_ready_for_local_raises_on_graphql_error() -> None:
    response = {"errors": [{"message": "unauthorized"}]}

    with patch("urllib.request.urlopen", return_value=_mock_response(response)):
        with pytest.raises(LinearError, match="unauthorized"):
            _client().list_ready_for_local()


# ---------------------------------------------------------------------------
# get_open_blockers
# ---------------------------------------------------------------------------


def test_get_open_blockers_filters_done_issues() -> None:
    response = {
        "data": {
            "issue": {
                "relations": {
                    "nodes": [
                        {
                            "relatedIssue": {
                                "identifier": "WOR-5",
                                "state": {"type": "completed"},
                            }
                        },
                        {
                            "relatedIssue": {
                                "identifier": "WOR-6",
                                "state": {"type": "started"},
                            }
                        },
                    ]
                }
            }
        }
    }

    with patch("urllib.request.urlopen", return_value=_mock_response(response)):
        blockers = _client().get_open_blockers("issue-id-123")

    assert blockers == ["WOR-6"]


def test_get_open_blockers_returns_empty_when_no_issue() -> None:
    response = {"data": {"issue": None}}

    with patch("urllib.request.urlopen", return_value=_mock_response(response)):
        blockers = _client().get_open_blockers("nonexistent")

    assert blockers == []


# ---------------------------------------------------------------------------
# set_state
# ---------------------------------------------------------------------------


def test_set_state_resolves_state_id_and_mutates() -> None:
    states_response = {
        "data": {
            "teams": {
                "nodes": [
                    {
                        "states": {
                            "nodes": [
                                {"id": "state-abc", "name": "InProgressLocal"},
                                {"id": "state-xyz", "name": "MergedToEpic"},
                            ]
                        }
                    }
                ]
            }
        }
    }
    mutation_response = {"data": {"issueUpdate": {"success": True}}}

    responses = [states_response, mutation_response]
    call_idx = 0

    def fake_urlopen(req: object, timeout: int = 30) -> MagicMock:
        nonlocal call_idx
        resp = _mock_response(responses[call_idx])
        call_idx += 1
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _client().set_state("issue-id-123", "InProgressLocal")


def test_set_state_raises_for_unknown_state() -> None:
    states_response = {
        "data": {
            "teams": {
                "nodes": [{"states": {"nodes": [{"id": "state-abc", "name": "Todo"}]}}]
            }
        }
    }

    with patch("urllib.request.urlopen", return_value=_mock_response(states_response)):
        with pytest.raises(LinearError, match="NoSuchState"):
            _client().set_state("issue-id-123", "NoSuchState")


def test_set_state_caches_state_ids() -> None:
    states_response = {
        "data": {
            "teams": {
                "nodes": [
                    {
                        "states": {
                            "nodes": [{"id": "state-abc", "name": "InProgressLocal"}]
                        }
                    }
                ]
            }
        }
    }
    mutation_response = {"data": {"issueUpdate": {"success": True}}}

    call_count = 0

    def fake_urlopen(req: object, timeout: int = 30) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _mock_response(states_response)
        return _mock_response(mutation_response)

    client = _client()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.set_state("id-1", "InProgressLocal")  # states fetch + mutate = 2 calls
        client.set_state("id-2", "InProgressLocal")  # cached — mutate only = 1 call

    assert call_count == 3  # 1 state lookup + 2 mutations
