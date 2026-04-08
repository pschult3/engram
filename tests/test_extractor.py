from __future__ import annotations

from engram.ingest.tools import events_from_tool_call


def test_edit_emits_file_edit():
    out = events_from_tool_call(
        "Edit",
        {"file_path": "src/x.py", "old_string": "a", "new_string": "ab"},
        {},
    )
    assert len(out) == 1
    assert out[0]["type"] == "file_edit"
    assert out[0]["payload"]["path"] == "src/x.py"
    assert out[0]["payload"]["delta_chars"] == 1


def test_noisy_bash_dropped():
    assert events_from_tool_call("Bash", {"command": "ls -la"}, {}) == []
    assert events_from_tool_call("Bash", {"command": "cd src && pwd"}, {}) == []


def test_failing_test_command_becomes_test_failure():
    out = events_from_tool_call(
        "Bash",
        {"command": "pytest tests/test_x.py"},
        {"stdout": "", "stderr": "FAILED tests/test_x.py::test_one\nAssertionError: nope"},
    )
    assert len(out) == 1
    assert out[0]["type"] == "test_failure"
    assert "FAILED" in out[0]["payload"]["message"]


def test_passing_test_command_dropped():
    out = events_from_tool_call(
        "Bash",
        {"command": "pytest tests/"},
        {"stdout": "5 passed", "stderr": ""},
    )
    assert out == []


def test_interesting_command_kept():
    out = events_from_tool_call(
        "Bash",
        {"command": "git commit -m 'feat: add memory'"},
        {"stdout": "", "stderr": ""},
    )
    assert len(out) == 1
    assert out[0]["type"] == "command"
    assert out[0]["payload"]["status"] == "ok"


def test_failing_command_kept_as_failure():
    out = events_from_tool_call(
        "Bash",
        {"command": "npm run build"},
        {"stdout": "", "stderr": "Error: ENOENT"},
    )
    assert len(out) == 1
    assert out[0]["payload"]["status"] == "fail"


def test_unrelated_tool_dropped():
    assert events_from_tool_call("Read", {"file_path": "x"}, {}) == []
    assert events_from_tool_call("Grep", {"pattern": "foo"}, {}) == []
