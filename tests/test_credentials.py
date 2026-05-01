from unittest.mock import patch

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
