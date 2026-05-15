"""Tests for .claude/hooks/posttooluse_thrash_detector.py."""

import json
import sys
import time
from pathlib import Path
from types import ModuleType
from unittest import mock

_HOOK_DIR = Path(__file__).resolve().parent.parent / ".claude" / "hooks"
_HOOK_PATH = _HOOK_DIR / "posttooluse_thrash_detector.py"


def _load_module() -> ModuleType:
    """Load the hook module fresh."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_thrash_detector", str(_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_thrash_detector"] = mod
    spec.loader.exec_module(mod)
    del sys.modules["_thrash_detector"]
    return mod


def _make_payload(
    file_path: str = "app/core/example.py",
    tool_name: str = "Edit",
    session_id: str = "sess-1",
) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
        "session_id": session_id,
    }


def _run_main(mod, payload, state_file):
    """Run mod.main(state_path=...) with mocked stdin and print.

    Returns (returncode, list_of_json_output_lines — i.e. lines from _warn).
    """
    captured: list[str] = []

    def capture(*args, **kwargs):
        if args:
            captured.append(str(args[0]))

    json.dumps(payload)

    with (
        mock.patch("builtins.print", side_effect=capture),
        mock.patch("json.load", return_value=payload),
    ):
        rc = mod.main(state_path=state_file)

    # Only keep JSON output lines (from _warn), skip stderr debug prints.
    json_lines = [s for s in captured if s.startswith("{")]
    return rc, json_lines


class TestThrashDetector:
    """Tests for the PostToolUse thrash detector hook."""

    # -- trigger tests --

    def test_no_warning_on_first_edit(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text(
            '{"session_id": "old-session", "edits": {}}', encoding="utf-8"
        )

        payload = _make_payload("app/core/example.py")
        rc, captured = _run_main(mod, payload, state_file)
        assert rc == 0
        data = json.loads("".join(captured)) if captured else {}
        assert "thrash_warning" not in data

    def test_no_warning_on_four_edits(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text('{"session_id": "sess-1", "edits": {}}', encoding="utf-8")

        last = None
        for _ in range(4):
            payload = _make_payload("app/core/example.py")
            last = _run_main(mod, payload, state_file)

        assert last is not None
        rc, captured = last
        data = json.loads("".join(captured)) if captured else {}
        assert "thrash_warning" not in data

    def test_warning_on_fifth_edit(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text('{"session_id": "sess-1", "edits": {}}', encoding="utf-8")

        last = None
        for _ in range(5):
            payload = _make_payload("app/core/example.py")
            last = _run_main(mod, payload, state_file)

        rc, captured = last
        data = json.loads("".join(captured)) if captured else {}
        assert "thrash_warning" in data
        assert "example.py" in data["thrash_warning"]
        assert "5" in data["thrash_warning"]

    def test_edit_and_write_both_count(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text('{"session_id": "sess-1", "edits": {}}', encoding="utf-8")

        last = None
        for tool in ("Edit", "Write", "Edit", "Write", "Edit"):
            payload = _make_payload("app/core/example.py", tool_name=tool)
            last = _run_main(mod, payload, state_file)

        rc, captured = last
        data = json.loads("".join(captured)) if captured else {}
        assert "thrash_warning" in data

    # -- session isolation --

    def test_no_cross_session_bleed(self, tmp_path: Path):
        """Verify that different session_ids produce independent edit counts."""
        # Seed with session-3 state (neither mod uses).
        state_file = tmp_path / ".thrash_state.json"
        state_file.write_text(
            json.dumps(
                {
                    "session_id": "sess-3",
                    "edits": {"app/core/example.py": [time.time() - 1] * 5},
                }
            ),
            encoding="utf-8",
        )

        # mod1 uses session-1 — session mismatches on first call,
        # then state file gets updated to "sess-1" and subsequent
        # calls accumulate. After 5 calls, triggers.
        mod1 = _load_module()
        last = None
        for _ in range(5):
            payload = _make_payload("app/core/example.py", session_id="sess-1")
            last = _run_main(mod1, payload, state_file)

        rc, captured = last
        data = json.loads("".join(captured)) if captured else {}
        assert "thrash_warning" in data  # triggers after accumulating 5

        # mod2 uses session-2 — starts fresh because mod1's file has "sess-1".
        # After 5 calls, triggers again — no bleed from mod1.
        mod2 = _load_module()
        for _ in range(5):
            payload = _make_payload("app/core/example.py", session_id="sess-2")
            last = _run_main(mod2, payload, state_file)

        rc, captured = last
        data = json.loads("".join(captured)) if captured else {}
        assert "thrash_warning" in data  # also triggers independently
        # Verify the edit count is 5, not 10.
        assert "5" in data["thrash_warning"]

    def test_session_id_reset_on_change(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        old_ts = [time.time() - 10] * 5
        state = {"session_id": "sess-1", "edits": {"app/core/example.py": old_ts}}
        state_file.write_text(json.dumps(state), encoding="utf-8")

        # 1 edit under sess-2 should not trigger (reset).
        payload = _make_payload("app/core/example.py", session_id="sess-2")
        rc, captured = _run_main(mod, payload, state_file)

        assert rc == 0
        data = json.loads("".join(captured)) if captured else {}
        assert "thrash_warning" not in data

    # -- per-file independence --

    def test_different_files_dont_mix(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text('{"session_id": "sess-1", "edits": {}}', encoding="utf-8")

        # 4 edits to file A.
        for _ in range(4):
            _run_main(mod, _make_payload("app/core/fileA.py"), state_file)

        # 1 edit to file B = total 5, but split across 2 files.
        payload = _make_payload("app/core/fileB.py")
        rc, captured = _run_main(mod, payload, state_file)

        assert rc == 0
        data = json.loads("".join(captured)) if captured else {}
        assert "thrash_warning" not in data

    def test_window_expiry_clears_count(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        old_ts = [time.time() - 600] * 4  # 10 minutes old
        state = {"session_id": "sess-1", "edits": {"app/core/example.py": old_ts}}
        state_file.write_text(json.dumps(state), encoding="utf-8")

        # New edit should prune old ones and start fresh.
        payload = _make_payload("app/core/example.py")
        rc, captured = _run_main(mod, payload, state_file)

        assert rc == 0
        data = json.loads("".join(captured)) if captured else {}
        assert "thrash_warning" not in data

    # -- non-trigger paths --

    def test_non_edit_tools_ignored(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text('{"session_id": "sess-1", "edits": {}}', encoding="utf-8")

        for tool in ("Bash", "Read", "Grep", "Agent"):
            payload = _make_payload("app/core/example.py", tool_name=tool)
            rc, _ = _run_main(mod, payload, state_file)
            assert rc == 0

    def test_no_file_path_ignored(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text('{"session_id": "sess-1", "edits": {}}', encoding="utf-8")

        payload = {"tool_name": "Edit", "tool_input": {}}
        rc, _ = _run_main(mod, payload, state_file)
        assert rc == 0

    # -- error handling --

    def test_malformed_json(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text('{"session_id": "sess-1", "edits": {}}', encoding="utf-8")

        rc, _ = _run_main(mod, "not json", state_file)
        assert rc == 0

    def test_non_dict_payload(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text('{"session_id": "sess-1", "edits": {}}', encoding="utf-8")

        rc, _ = _run_main(mod, [1, 2, 3], state_file)
        assert rc == 0

    def test_empty_input(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text('{"session_id": "sess-1", "edits": {}}', encoding="utf-8")

        rc, _ = _run_main(mod, "", state_file)
        assert rc == 0

    def test_stale_state_file(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text("this is not json", encoding="utf-8")

        payload = _make_payload("app/core/example.py")
        rc, _ = _run_main(mod, payload, state_file)
        assert rc == 0

    def test_empty_state_dict(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text('{"session_id": "sess-1"}', encoding="utf-8")

        payload = _make_payload("app/core/example.py")
        rc, _ = _run_main(mod, payload, state_file)
        assert rc == 0

    # -- message content --

    def test_warning_message_content(self, tmp_path: Path):
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text('{"session_id": "sess-1", "edits": {}}', encoding="utf-8")

        last = None
        for _ in range(5):
            payload = _make_payload("app/core/watcher.py")
            last = _run_main(mod, payload, state_file)

        rc, captured = last
        data = json.loads("".join(captured)) if captured else {}
        assert "thrash_warning" in data
        msg = data["thrash_warning"]
        assert "mock" in msg.lower()
        assert "watcher.py" in msg
        assert "5" in msg

    def test_no_warning_on_four_edits_strict(self, tmp_path: Path):
        """Edge: exactly 4 edits — no warning, regardless of file."""
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        state_file.write_text('{"session_id": "sess-1", "edits": {}}', encoding="utf-8")

        for _ in range(4):
            payload = _make_payload("app/core/different.py")
            rc, _ = _run_main(mod, payload, state_file)
            assert rc == 0

    def test_stale_edits_dont_carry_to_new_session(self, tmp_path: Path):
        """A stale state file with old session_id is ignored."""
        mod = _load_module()
        state_file = tmp_path / mod.STATE_FILENAME
        old_ts = [time.time() - 5] * 10  # 10 old edits under old session
        state = {"session_id": "old-session", "edits": {"app/core/example.py": old_ts}}
        state_file.write_text(json.dumps(state), encoding="utf-8")

        # New session should see session mismatch and reset.
        payload = _make_payload("app/core/example.py", session_id="new-sess")
        rc, captured = _run_main(mod, payload, state_file)

        assert rc == 0
        data = json.loads("".join(captured)) if captured else {}
        assert "thrash_warning" not in data
