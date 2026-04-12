import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.user_prefs import PrefsStore, UserPreferences


@pytest.fixture()
def prefs_path(tmp_path):
    path = tmp_path / "prefs.json"
    with patch.object(PrefsStore, "get_path", return_value=path):
        yield path


def test_defaults(prefs_path):
    prefs = PrefsStore.load()
    assert prefs.author_name == ""
    assert prefs.author_email == ""
    assert prefs.github_username == ""
    assert prefs.default_output_dir is None
    assert prefs.default_preset == "python_basic"


def test_save_and_load_roundtrip(prefs_path):
    original = UserPreferences(
        author_name="Antti",
        author_email="antti@example.com",
        github_username="virppa",
        default_output_dir=Path("/tmp/repos"),
        default_preset="full_agentic",
    )
    PrefsStore.save(original)
    loaded = PrefsStore.load()
    assert loaded.author_name == "Antti"
    assert loaded.author_email == "antti@example.com"
    assert loaded.github_username == "virppa"
    assert loaded.default_output_dir == Path("/tmp/repos")
    assert loaded.default_preset == "full_agentic"


def test_save_creates_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b" / "prefs.json"
    with patch.object(PrefsStore, "get_path", return_value=nested):
        PrefsStore.save(UserPreferences(author_name="X"))
    assert nested.exists()


def test_load_ignores_unknown_fields(prefs_path):
    prefs_path.write_text(
        json.dumps({"author_name": "Bob", "unknown_field": "ignored"}),
        encoding="utf-8",
    )
    prefs = PrefsStore.load()
    assert prefs.author_name == "Bob"


def test_load_malformed_json_returns_defaults(prefs_path):
    prefs_path.write_text("not-valid-json", encoding="utf-8")
    prefs = PrefsStore.load()
    assert prefs == UserPreferences()


def test_get_path_windows():
    with (
        patch("platform.system", return_value="Windows"),
        patch("pathlib.Path.home", return_value=Path("C:/Users/test")),
    ):
        path = PrefsStore.get_path()
    assert "AppData" in str(path)
    assert "Roaming" in str(path)
    assert path.name == "prefs.json"


def test_get_path_posix():
    with (
        patch("platform.system", return_value="Linux"),
        patch("pathlib.Path.home", return_value=Path("/home/test")),
    ):
        path = PrefsStore.get_path()
    assert ".config" in str(path)
    assert path.name == "prefs.json"


def test_save_blocked_inside_git_repo(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    prefs_path = tmp_path / "prefs.json"
    with patch.object(PrefsStore, "get_path", return_value=prefs_path):
        with pytest.raises(
            RuntimeError, match="Refusing to write prefs inside a git repository"
        ):
            PrefsStore.save(UserPreferences())
