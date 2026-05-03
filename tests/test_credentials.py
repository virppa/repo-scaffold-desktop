from unittest.mock import patch

import pytest
from keyring.errors import KeyringError

import app.core.credentials as cred

SERVICE = "repo-scaffold-desktop"
ACCOUNT = "github_token"


def test_save_token():
    with patch.object(cred, "keyring", create=True) as kr:
        cred.save_token("ghp_abc123")
        kr.set_password.assert_called_once_with(SERVICE, ACCOUNT, "ghp_abc123")


def test_get_token_from_keyring():
    with patch.object(cred, "keyring", create=True) as kr:
        kr.get_password.return_value = "ghp_from_keyring"
        result = cred.get_token()
    kr.get_password.assert_called_once_with(SERVICE, ACCOUNT)
    assert result == "ghp_from_keyring"


def test_get_token_falls_back_to_env_when_keyring_returns_none():
    with patch.object(cred, "keyring", create=True) as kr:
        kr.get_password.return_value = None
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_from_env"}):
            result = cred.get_token()
    assert result == "ghp_from_env"


def test_get_token_returns_keyring_value_over_env():
    with patch.object(cred, "keyring", create=True) as kr:
        kr.get_password.return_value = "ghp_from_keyring"
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_from_env"}):
            result = cred.get_token()
    assert result == "ghp_from_keyring"


def test_get_token_falls_back_to_env_on_no_keyring_error():
    from keyring.errors import NoKeyringError

    with patch.object(cred, "keyring", create=True) as kr:
        kr.get_password.side_effect = NoKeyringError("no keyring")
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_from_env"}):
            result = cred.get_token()
    assert result == "ghp_from_env"


def test_get_token_returns_none_when_keyring_fails_and_no_env():
    from keyring.errors import NoKeyringError

    with patch.object(cred, "keyring", create=True) as kr:
        kr.get_password.side_effect = NoKeyringError("no keyring")
        with patch.dict("os.environ", {}, clear=True):
            result = cred.get_token()
    assert result is None


def test_delete_token():
    with patch.object(cred, "keyring", create=True) as kr:
        cred.delete_token()
        kr.delete_password.assert_called_once_with(SERVICE, ACCOUNT)


def test_delete_token_ignores_keyring_errors():
    from keyring.errors import NoKeyringError

    with patch.object(cred, "keyring", create=True) as kr:
        kr.delete_password.side_effect = NoKeyringError("no keyring")
        cred.delete_token()  # should not raise


def test_delete_token_ignores_generic_keyring_error():
    with patch.object(cred, "keyring", create=True) as kr:
        kr.delete_password.side_effect = KeyringError("generic keyring failure")
        cred.delete_token()  # should not raise


def test_delete_token_ignores_nonexistent_password():
    from keyring.errors import PasswordDeleteError

    with patch.object(cred, "keyring", create=True) as kr:
        kr.delete_password.side_effect = PasswordDeleteError("password does not exist")
        cred.delete_token()  # should not raise


def test_save_token_propagates_keyring_error():
    with patch.object(cred, "keyring", create=True) as kr:
        kr.set_password.side_effect = KeyringError("keyring unavailable")
        with pytest.raises(KeyringError):
            cred.save_token("ghp_test")


def test_save_token_empty_string():
    with patch.object(cred, "keyring", create=True) as kr:
        cred.save_token("")
        kr.set_password.assert_called_once_with(SERVICE, ACCOUNT, "")


def test_get_token_empty_string_from_keyring_falls_back_to_env():
    with patch.object(cred, "keyring", create=True) as kr:
        kr.get_password.return_value = ""
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_from_env"}):
            result = cred.get_token()
    assert result == "ghp_from_env"


def test_get_token_empty_string_from_keyring_no_env():
    with patch.object(cred, "keyring", create=True) as kr:
        kr.get_password.return_value = ""
        with patch.dict("os.environ", {}, clear=True):
            result = cred.get_token()
    assert result is None


def test_get_token_bytes_from_keyring():
    with patch.object(cred, "keyring", create=True) as kr:
        kr.get_password.return_value = b"ghp_bytes_token"
        result = cred.get_token()
    # str(bytes) produces repr-style string, not decoded value
    assert result == "b'ghp_bytes_token'"


def test_get_token_whitespace_only_from_keyring():
    with patch.object(cred, "keyring", create=True) as kr:
        kr.get_password.return_value = "   "
        result = cred.get_token()
    assert result == "   "


def test_get_token_empty_github_token_env():
    with patch.object(cred, "keyring", create=True) as kr:
        kr.get_password.return_value = None
        with patch.dict("os.environ", {"GITHUB_TOKEN": ""}):
            result = cred.get_token()
    assert result == ""


def test_cli_set_token_success(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("app.core.credentials.getpass", return_value="ghp_live"):
        with patch.object(cred, "save_token") as mock_save:
            result = cred.cli_set_token()
    assert result == 0
    mock_save.assert_called_once_with("ghp_live")
    assert "Done." in capsys.readouterr().err


def test_cli_set_token_empty_input(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("app.core.credentials.getpass", return_value=""):
        result = cred.cli_set_token()
    assert result == 1
    assert "empty token" in capsys.readouterr().err


def test_cli_set_token_eof(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("app.core.credentials.getpass", side_effect=EOFError):
        result = cred.cli_set_token()
    assert result == 1


def test_cli_set_token_keyring_error(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("app.core.credentials.getpass", return_value="ghp_live"):
        with patch.object(
            cred, "save_token", side_effect=KeyringError("keyring unavailable")
        ):
            result = cred.cli_set_token()
    assert result == 1
    assert "unable to store token" in capsys.readouterr().err
