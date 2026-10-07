import json

import pytest

from naiwa.install import command_for, install_codex, install_cursor, uninstall_codex, uninstall_cursor


def test_install_merges_and_uninstall_keeps_other_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("NAIWA_HOME", str(tmp_path / "data"))
    cursor = tmp_path / "cursor" / "hooks.json"
    cursor.parent.mkdir()
    cursor.write_text(
        json.dumps({"version": 1, "hooks": {"stop": [{"command": "echo keep"}]}}),
        encoding="utf-8",
    )
    codex = tmp_path / "codex" / "hooks.json"
    backup = tmp_path / "backups"
    python = r"C:\Python\python.exe"
    install_cursor(cursor, python, backup)
    install_cursor(cursor, python, backup)
    install_codex(codex, python, backup)
    cursor_hooks = json.loads(cursor.read_text(encoding="utf-8"))["hooks"]
    assert [item["command"] for item in cursor_hooks["stop"]] == ["echo keep", command_for("cursor", python, "stop")]
    assert cursor_hooks["preToolUse"][0]["timeout"] == 3
    assert cursor_hooks["preToolUse"][0]["failClosed"] is False
    codex_stop = json.loads(codex.read_text(encoding="utf-8"))["hooks"]["Stop"]
    assert len(codex_stop) == 1
    assert codex_stop[0]["hooks"][0]["command"] == command_for("codex", python, "Stop")
    uninstall_cursor(cursor)
    uninstall_codex(codex)
    assert json.loads(cursor.read_text(encoding="utf-8"))["hooks"]["stop"] == [{"command": "echo keep"}]
    assert json.loads(codex.read_text(encoding="utf-8"))["hooks"] == {}
    assert len(list(backup.glob("*.before-naiwa.json"))) == 1


@pytest.mark.parametrize("contents", ['{broken', '[]', '{"hooks":[]}', '{"hooks":{"stop":"bad"}}'])
def test_invalid_config_is_never_overwritten(tmp_path, contents, monkeypatch):
    monkeypatch.setenv("NAIWA_HOME", str(tmp_path / "data"))
    path = tmp_path / "hooks.json"
    path.write_text(contents)
    with pytest.raises(ValueError):
        install_cursor(path, "python", tmp_path / "backups")
    assert path.read_text() == contents


def test_backups_for_cursor_and_codex_do_not_collide_and_groups_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("NAIWA_HOME", str(tmp_path / "data"))
    cursor, codex = tmp_path / "cursor" / "hooks.json", tmp_path / "codex" / "hooks.json"
    cursor.parent.mkdir()
    codex.parent.mkdir()
    cursor.write_text('{"version":1,"hooks":{}}')
    original = {"description": "keep", "hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [
        {"type": "mcp_tool", "server": "audit", "tool": "read"},
        {"type": "command", "command": '"python" -m naiwa.hook --source codex'}]}]}}
    codex.write_text(json.dumps(original))
    backup = tmp_path / "backups"
    install_cursor(cursor, "python", backup)
    install_codex(codex, "python", backup)
    assert len(list(backup.glob("*.before-naiwa.json"))) == 2
    data = json.loads(codex.read_text())
    assert data["description"] == "keep"
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "Read"
    assert data["hooks"]["PreToolUse"][0]["hooks"] == [{"type": "mcp_tool", "server": "audit", "tool": "read"}]
    assert data["hooks"]["Interrupt"][0]["hooks"][0]["timeout"] <= 3
