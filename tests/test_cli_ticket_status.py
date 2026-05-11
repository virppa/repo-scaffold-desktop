"""CLI integration tests for the ticket-status subcommand."""

import json
import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from app.cli import main

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture as CapSys
else:
    CapSys = pytest.CaptureFixture  # type: ignore[misc,assignment]

# ── Environment setup ──────────────────────────────────────────────────────────

FAKE_API_KEY = "fake_linear_key_for_tests"  # pragma: allowlist secret


def _clear_linear_key(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of env without LINEAR_API_KEY."""
    result = dict(env)
    result.pop("LINEAR_API_KEY", None)
    return result


# ── Missing env tests ──────────────────────────────────────────────────────────


class TestTicketStatusMissingEnv:
    def test_missing_api_key_exits_with_error(self, capsys: CapSys) -> None:
        env = _clear_linear_key(os.environ)
        # WOR-427: main() calls load_dotenv() which re-populates LINEAR_API_KEY
        # from .env if the file exists locally — defeating the env clearing.
        # Patch load_dotenv to a no-op so the test isolates the env state.
        with (
            patch.dict("os.environ", env, clear=True),
            patch("app.cli.main.load_dotenv"),
        ):
            rc = main(["ticket-status", "WOR-999"])

        assert rc == 1
        err = capsys.readouterr().err
        assert "LINEAR_API_KEY" in err


# ── Linear error tests ─────────────────────────────────────────────────────────


def _mock_linear_client() -> MagicMock:
    """Create a LinearClient mock that returns a standard issue dict."""
    client = MagicMock()
    client.get_issue.return_value = {
        "title": "Test ticket",
        "state": {"name": "InProgressLocal", "createdAt": "2026-05-10T06:19:32.000Z"},
    }
    return client


class TestTicketStatusNotFound:
    def test_linear_error_shows_message(self, capsys: CapSys) -> None:
        client = MagicMock()
        client.get_issue.side_effect = Exception("issue not found")
        with (
            patch.dict("os.environ", {"LINEAR_API_KEY": FAKE_API_KEY}, clear=True),
            patch("app.cli.operator.LinearClient", return_value=client),
        ):
            rc = main(["ticket-status", "WOR-999"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "error fetching" in err

    def test_unknown_ticket_id_exits_with_error(self, capsys: CapSys) -> None:
        """A valid LinearClient that returns None for an unknown ticket id."""
        client = MagicMock()
        client.get_issue.return_value = None
        with (
            patch.dict("os.environ", {"LINEAR_API_KEY": FAKE_API_KEY}, clear=True),
            patch("app.cli.operator.LinearClient", return_value=client),
        ):
            rc = main(["ticket-status", "WOR-999"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "ticket not found" in err


# ── --json flag tests ──────────────────────────────────────────────────────────


class TestTicketStatusJson:
    def test_json_output_is_valid(self, capsys: CapSys) -> None:
        client = _mock_linear_client()
        with (
            patch.dict("os.environ", {"LINEAR_API_KEY": FAKE_API_KEY}, clear=True),
            patch("app.cli.operator.LinearClient", return_value=client),
        ):
            rc = main(["ticket-status", "--json", "WOR-123"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["ticket_id"] == "WOR-123"
        assert data["state"] == "InProgressLocal"
        assert data["is_terminal"] is False

    def test_json_output_parsing(self, capsys: CapSys) -> None:
        client = _mock_linear_client()
        with (
            patch.dict("os.environ", {"LINEAR_API_KEY": FAKE_API_KEY}, clear=True),
            patch("app.cli.operator.LinearClient", return_value=client),
        ):
            rc = main(["ticket-status", "--json", "WOR-123"])

        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["ticket_id"] == "WOR-123"
        assert data["state"] == "InProgressLocal"


# ── --brief flag tests ─────────────────────────────────────────────────────────


class TestTicketStatusBrief:
    def test_brief_is_single_line(self, capsys: CapSys) -> None:
        client = _mock_linear_client()
        with (
            patch.dict("os.environ", {"LINEAR_API_KEY": FAKE_API_KEY}, clear=True),
            patch("app.cli.operator.LinearClient", return_value=client),
        ):
            rc = main(["ticket-status", "--brief", "WOR-123"])

        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out.count("\n") == 0
        assert "WOR-123" in out
        assert "InProgressLocal" in out


# ── Normal output tests ────────────────────────────────────────────────────────


class TestTicketStatusNormalOutput:
    def test_normal_output_structure(self, capsys: CapSys) -> None:
        client = _mock_linear_client()
        with (
            patch.dict("os.environ", {"LINEAR_API_KEY": FAKE_API_KEY}, clear=True),
            patch("app.cli.operator.LinearClient", return_value=client),
        ):
            rc = main(["ticket-status", "WOR-123"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Ticket: WOR-123" in out
        assert "Test ticket" in out
        assert "InProgressLocal" in out


# ── Subparser registration tests ───────────────────────────────────────────────


class TestTicketStatusSubparser:
    def test_flag_parsing(self, capsys: CapSys) -> None:
        """All flags should be accepted without error."""
        client = _mock_linear_client()
        with (
            patch.dict("os.environ", {"LINEAR_API_KEY": FAKE_API_KEY}, clear=True),
            patch("app.cli.operator.LinearClient", return_value=client),
        ):
            rc = main(["ticket-status", "--json", "--brief", "WOR-123"])
        assert rc == 0
        # --json takes precedence over --brief: JSON is emitted.
        data = json.loads(capsys.readouterr().out)
        assert data["ticket_id"] == "WOR-123"
