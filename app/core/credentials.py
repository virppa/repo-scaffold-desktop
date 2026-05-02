import os
import sys

import keyring
from keyring.errors import KeyringError, NoKeyringError

_SERVICE = "repo-scaffold-desktop"
_ACCOUNT = "github_token"


def save_token(token: str) -> None:
    """Store a GitHub token in the OS credential store."""
    keyring.set_password(_SERVICE, _ACCOUNT, token)


def get_token() -> str | None:
    """Retrieve the stored GitHub token.

    Falls back to the GITHUB_TOKEN environment variable when keyring
    returns None (no stored token) or raises (e.g. headless CI).
    """
    try:
        token = keyring.get_password(_SERVICE, _ACCOUNT)
    except (NoKeyringError, KeyringError):
        token = None
    if token:
        return str(token)
    return os.environ.get("GITHUB_TOKEN")


def delete_token() -> None:
    """Remove the stored GitHub token from the credential store."""
    try:
        keyring.delete_password(_SERVICE, _ACCOUNT)
    except (NoKeyringError, KeyringError):
        pass


def cli_set_token() -> int:
    """Prompt for a GitHub token via getpass and store it."""
    try:
        token = __import__("getpass", fromlist=["getpass"]).getpass(
            "GitHub token: ",
            stream=sys.stderr,
        )
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return 1

    if not token:
        print("Error: empty token", file=sys.stderr)
        return 1

    try:
        save_token(token)
        print("Done.", file=sys.stderr)
    except (NoKeyringError, KeyringError) as exc:
        print(
            f"Error: unable to store token ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1

    return 0


def cli_delete_token() -> int:
    """Remove the stored GitHub token."""
    try:
        delete_token()
    except (NoKeyringError, KeyringError) as exc:
        print(
            f"Error: unable to delete token ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1

    print("Done.", file=sys.stderr)
    return 0
