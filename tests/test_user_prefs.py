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


def test_load_empty_json_object(prefs_path):
    prefs_path.write_text("{}", encoding="utf-8")
    prefs = PrefsStore.load()
    assert prefs == UserPreferences()


def test_load_binary_corrupt_json(prefs_path):
    prefs_path.write_bytes(b"\x00\x01\x02\xff\xfe\x80\x81")
    prefs = PrefsStore.load()
    assert prefs == UserPreferences()


def test_load_deeply_nested_corrupt_json(prefs_path):
    prefs_path.write_text(
        '{"author_name": {"nested": {"deep": "data"}}}', encoding="utf-8"
    )
    prefs = PrefsStore.load()
    assert prefs == UserPreferences()


def test_save_atomic_write_no_partial_file(prefs_path):
    prefs = UserPreferences(author_name="AtomicTest")
    PrefsStore.save(prefs)
    # After a successful save, the file should exist and be valid JSON
    assert prefs_path.exists()
    data = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert data["author_name"] == "AtomicTest"
    # No .tmp or partial file should be left behind
    for suffix in (".tmp", ".bak", ".part", ".new"):
        partials = list(prefs_path.parent.glob(f"{prefs_path.name}{suffix}*"))
        assert len(partials) == 0, f"Found unexpected partial file: {partials}"


def test_pydantic_validation_rejects_invalid_types_directly():
    # model_validate raises ValidationError for invalid types
    # (PrefsStore.load() catches ValueError and returns defaults)
    with pytest.raises(Exception):
        UserPreferences.model_validate({"author_name": 12345})


def test_pydantic_validation_rejects_invalid_output_dir_directly():
    with pytest.raises(Exception):
        UserPreferences.model_validate({"default_output_dir": ["not", "a", "string"]})


def test_pydantic_validation_rejects_invalid_preset_directly():
    with pytest.raises(Exception):
        UserPreferences.model_validate({"default_preset": 42})


def test_load_invalid_type_returns_defaults(prefs_path):
    # PrefsStore.load() catches ValueError (includes Pydantic ValidationError)
    # and returns defaults — this is the actual runtime behaviour.
    prefs_path.write_text(
        json.dumps({"author_name": 12345}),  # int instead of str
        encoding="utf-8",
    )
    prefs = PrefsStore.load()
    assert prefs == UserPreferences()


def test_load_missing_file_returns_defaults(prefs_path):
    # Ensure the file does not exist
    if prefs_path.exists():
        prefs_path.unlink()
    prefs = PrefsStore.load()
    assert prefs == UserPreferences()


def test_load_missing_dir_returns_defaults(tmp_path):
    # Use a path whose parent directory does not exist
    nonexistent = tmp_path / "does" / "not" / "exist" / "prefs.json"
    with patch.object(PrefsStore, "get_path", return_value=nonexistent):
        prefs = PrefsStore.load()
    assert prefs == UserPreferences()
