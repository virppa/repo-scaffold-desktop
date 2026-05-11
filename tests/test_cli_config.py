"""Tests for app.cli.config — config get/set/delete subcommand handlers.

WOR-432: coverage fill for the cli/ Phase 3 work. Covers the previously-untested
_config_set and _config_delete code paths (lines 27-45, 68-83 in config.py).
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from keyring.errors import KeyringError, NoKeyringError

from app.cli.config import _config_delete, _config_set, _run_config

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture as CapSys
else:
    CapSys = pytest.CaptureFixture  # type: ignore[misc,assignment]


# ── _config_set: github-token paths ────────────────────────────────────────────


def test_config_set_github_token_via_value(capsys: CapSys) -> None:
    """When --value is supplied, save_token receives it without prompting."""
    args = argparse.Namespace(key="github-token", value="raw-token-abc")
    with patch("app.cli.config.save_token") as mock_save:
        rc = _config_set(args)
    assert rc == 0
    mock_save.assert_called_once_with("raw-token-abc")
    assert "Done." in capsys.readouterr().err


def test_config_set_github_token_via_stdin_prompt(capsys: CapSys) -> None:
    """When --value omitted, getpass prompts and the result is saved."""
    args = argparse.Namespace(key="github-token", value=None)
    with (
        patch("app.cli.config.getpass.getpass", return_value="prompted-token"),
        patch("app.cli.config.save_token") as mock_save,
    ):
        rc = _config_set(args)
    assert rc == 0
    mock_save.assert_called_once_with("prompted-token")
    assert "Done." in capsys.readouterr().err


def test_config_set_github_token_empty_after_prompt(capsys: CapSys) -> None:
    """Empty token (user hit Enter) returns 1 with an error message."""
    args = argparse.Namespace(key="github-token", value=None)
    with (
        patch("app.cli.config.getpass.getpass", return_value=""),
        patch("app.cli.config.save_token") as mock_save,
    ):
        rc = _config_set(args)
    assert rc == 1
    mock_save.assert_not_called()
    assert "empty token" in capsys.readouterr().err


def test_config_set_github_token_keyring_error(capsys: CapSys) -> None:
    """KeyringError from save_token returns 1 with a clear stderr message."""
    args = argparse.Namespace(key="github-token", value="token-x")
    with patch("app.cli.config.save_token", side_effect=KeyringError("boom")):
        rc = _config_set(args)
    assert rc == 1
    assert "KeyringError" in capsys.readouterr().err


def test_config_set_github_token_no_keyring(capsys: CapSys) -> None:
    """NoKeyringError (no backend installed) also returns 1."""
    args = argparse.Namespace(key="github-token", value="token-y")
    with patch("app.cli.config.save_token", side_effect=NoKeyringError("no backend")):
        rc = _config_set(args)
    assert rc == 1
    assert "NoKeyringError" in capsys.readouterr().err


# ── _config_set: regular preference paths ─────────────────────────────────────


def test_config_set_regular_pref_saves_to_prefs_store(
    capsys: CapSys,
    tmp_path,
) -> None:
    """Setting author-name updates PrefsStore."""
    from app.core.user_prefs import UserPreferences

    existing = UserPreferences(author_name="Old")
    args = argparse.Namespace(key="author-name", value="New Name")
    with (
        patch("app.cli.config.PrefsStore.load", return_value=existing),
        patch("app.cli.config.PrefsStore.save") as mock_save,
    ):
        rc = _config_set(args)
    assert rc == 0
    mock_save.assert_called_once()
    saved = mock_save.call_args[0][0]
    assert saved.author_name == "New Name"
    assert "✓ author-name = New Name" in capsys.readouterr().out


# Note: unknown-key validation lives at the argparse level (choices=) — the
# `key` argument is restricted to a known set, so _config_set never sees one
# it can't look up. No test needed here for that path.


# ── _config_delete paths ───────────────────────────────────────────────────────


def test_config_delete_github_token_calls_cli_delete(capsys: CapSys) -> None:
    """delete github-token routes to cli_delete_token (which manages keyring + msg)."""
    args = argparse.Namespace(key="github-token")
    with patch("app.cli.config.cli_delete_token", return_value=0) as mock_del:
        rc = _config_delete(args)
    assert rc == 0
    mock_del.assert_called_once()


def test_config_delete_unknown_key_returns_error(capsys: CapSys) -> None:
    """delete on an unknown credential prints error + returns 1."""
    args = argparse.Namespace(key="not-a-credential")
    rc = _config_delete(args)
    assert rc == 1
    assert "unknown credential" in capsys.readouterr().err


# ── _run_config dispatcher fallthrough ────────────────────────────────────────


def test_run_config_no_subcommand_prints_usage(capsys: CapSys) -> None:
    """When config_cmd is None/unknown, dispatcher prints usage and returns 1."""
    args = argparse.Namespace(config_cmd=None)
    rc = _run_config(args)
    assert rc == 1
    assert "Usage" in capsys.readouterr().err
