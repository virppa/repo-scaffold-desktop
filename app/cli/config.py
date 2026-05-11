"""User preferences + GitHub token management subcommands."""

import argparse
import getpass
import sys
from pathlib import Path

from keyring.errors import KeyringError, NoKeyringError

from app.core.credentials import cli_delete_token, save_token
from app.core.user_prefs import PrefsStore, UserPreferences

_PREFS_KEYS = set(UserPreferences.model_fields)
_KEY_TO_FIELD = {k.replace("_", "-"): k for k in _PREFS_KEYS}


def _config_get(_args: argparse.Namespace) -> int:
    prefs = PrefsStore.load()
    for field, value in prefs.model_dump().items():
        key = field.replace("_", "-")
        print(f"{key}: {value}")
    return 0


def _config_set(args: argparse.Namespace) -> int:
    if args.key == "github-token":
        raw = args.value
        if not raw:
            raw = getpass.getpass(
                "GitHub token: ",
                stream=sys.stderr,
            )
        if not raw:
            print("Error: empty token", file=sys.stderr)
            return 1
        try:
            save_token(raw)
            print("Done.", file=sys.stderr)
        except (NoKeyringError, KeyringError) as exc:
            print(
                f"Error: unable to store token ({type(exc).__name__})",
                file=sys.stderr,
            )
            return 1
        return 0

    field = _KEY_TO_FIELD[args.key]
    prefs = PrefsStore.load()
    raw = args.value
    field_info = UserPreferences.model_fields[field]
    annotation = field_info.annotation
    # Handle Path | None
    if annotation in (Path, "Path | None") or (
        annotation is not None
        and hasattr(annotation, "__args__")
        and Path in annotation.__args__
    ):
        value = Path(raw) if raw else None
    else:
        value = raw
    updated = prefs.model_copy(update={field: value})
    PrefsStore.save(updated)
    print(f"✓ {args.key} = {value}")
    return 0


def _config_delete(args: argparse.Namespace) -> int:
    if args.key == "github-token":
        return cli_delete_token()
    print(
        f"Error: unknown credential '{args.key}'",
        file=sys.stderr,
    )
    return 1


def _run_config(args: argparse.Namespace) -> int:
    if args.config_cmd == "get":
        return _config_get(args)
    if args.config_cmd == "set":
        return _config_set(args)
    if args.config_cmd == "delete":
        return _config_delete(args)

    print("Usage: scaffold config {get,set,delete}", file=sys.stderr)
    return 1
