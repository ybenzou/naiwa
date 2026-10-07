import io
import json
import os
import subprocess
import sys

import pytest

from naiwa.bus import events_path, read_events
from naiwa.hook import main


def run_hook(tmp_path, source, name, payload, origin=None, *, ide_only=False):
    env = os.environ.copy()
    if origin is not None:
        env["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"] = origin
    else:
        env.pop("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", None)
    command = [sys.executable, "-m", "naiwa.hook", "--source", source, "--event", name, "--data-dir", str(tmp_path)]
    if ide_only:
        command.append("--ide-only")
    return subprocess.run(command, input=json.dumps(payload, ensure_ascii=False).encode(), capture_output=True,
                          timeout=3, env=env)


def test_utf8_prompt_never_leaks_into_bus_or_diagnostic(tmp_path):
    payload = {"hook_event_name": "beforeSubmitPrompt", "conversation_id": "a", "generation_id": "one",
               "prompt": "非常秘密的提示词", "tool_input": {"敏感命令": "正文"}}
    run = run_hook(tmp_path, "cursor", "beforeSubmitPrompt", payload)
    assert run.returncode == 0
    assert json.loads(run.stdout) == {"continue": True}
    assert len(read_events(events_path(tmp_path))) == 1
    combined = b"".join(p.read_bytes() for p in tmp_path.glob("*.jsonl"))
    assert "非常秘密".encode() not in combined
    assert "敏感命令".encode() not in combined


@pytest.mark.parametrize("origin,observed", [("codex_vscode", True), ("codex_cli_rs", False), (None, False)])
def test_installed_codex_hook_filters_other_clients(tmp_path, origin, observed):
    run = run_hook(tmp_path, "codex", "UserPromptSubmit",
                   {"session_id": "thread", "turn_id": "turn"}, origin, ide_only=True)
    assert run.returncode == 0
    assert bool(read_events(events_path(tmp_path))) is observed
    diagnostic = json.loads((tmp_path / "probe-keys.jsonl").read_text().strip())
    assert diagnostic["observed"] is observed


def test_malformed_input_uses_configured_event_to_return_legal_allow(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("invalid JSON"))
    assert main(["--source", "cursor", "--event", "preToolUse", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"permission": "allow"}


@pytest.mark.parametrize("source,name,workspace", [("cursor", "beforeSubmitPrompt", "项目名称"), ("codex", "UserPromptSubmit", "workspace-a")])
def test_workspace_metadata_survives_real_hook_process_without_parent_path(tmp_path, source, name, workspace):
    path = "/private-parent/"+workspace
    payload = {"session_id": "a", "conversation_id": "a", "turn_id": "t", "generation_id": "t",
               "workspace_roots": [path], "cwd": path, "prompt": "私密正文"}
    run = run_hook(tmp_path, source, name, payload, origin="codex_vscode")
    assert run.returncode == 0
    record = read_events(events_path(tmp_path))[0]
    assert record.workspace_name == workspace
    assert record.workspace_id.startswith("ws-")
    combined = b"".join(p.read_bytes() for p in tmp_path.glob("*.jsonl"))
    assert b"private-parent" not in combined
    assert "私密正文".encode() not in combined


@pytest.mark.skipif(os.name != "nt", reason="Windows hook shell parsing")
@pytest.mark.parametrize("source,shell", [("cursor", "powershell"), ("codex", "cmd"), ("codex", "powershell")])
def test_generated_hook_command_works_from_windows_ide_shells_with_spaces(tmp_path, source, shell):
    from naiwa.install import command_for
    target = tmp_path / "bus with spaces"
    command = command_for(source, sys.executable, "preToolUse" if source == "cursor" else "PreToolUse", target)
    # cmd needs a literal command line: Python's argv quoting uses C-runtime escaping,
    # which is not cmd.exe's own quote grammar.
    host = 'cmd.exe /d /s /c "'+command+'"' if shell == "cmd" else [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    env = {**os.environ, "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "codex_vscode"}
    run = subprocess.run(host, input=b'{"conversation_id":"a","generation_id":"one","session_id":"a","turn_id":"one"}',
                         capture_output=True, timeout=4, env=env)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == ({"permission": "allow"} if source == "cursor" else {})
    assert len(read_events(events_path(target))) == 1


@pytest.mark.skipif(os.name != "nt", reason="Installed Cursor Windows temp-file backend")
def test_cursor_actual_temp_file_wrapper_can_deliver_a_large_thought(tmp_path):
    from naiwa.install import command_for
    path = tmp_path/"cursor-hook-payload-test.json"
    path.write_text(json.dumps({"conversation_id": "real-backend-test", "generation_id": "t",
                              "text": "思考正文"*700000, "workspace_roots": ["/private-parent/项目名称"]}, ensure_ascii=False), encoding="utf-8")
    target = tmp_path/"events"
    command = command_for("cursor", sys.executable, "afterAgentThought", target)
    # Exact input wiring from Cursor's installed $executeHookDirect/Tbt code.
    literal = str(path).replace("'", "''")
    wrapper = "$OutputEncoding = [System.Text.Encoding]::UTF8; Get-Content -LiteralPath '"+literal+"' -Raw | & { $input | "+command+" }"
    run = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", wrapper],
                         capture_output=True, timeout=8)
    assert run.returncode == 0, run.stderr
    records = read_events(events_path(target))
    diagnostic = json.loads((target/"probe-keys.jsonl").read_text().strip())
    assert len(records) == 1 and records[0].event == "heartbeat", (diagnostic["input_status"], diagnostic["input_bytes"], diagnostic["outcome"])
    assert diagnostic["input_status"] == "ok" and diagnostic["input_bytes"] > 4*1024*1024
    assert diagnostic["input_mode"] == "file" and records[0].workspace_name == "项目名称"
def test_cursor_remote_workspace_probe_stays_local_and_keeps_only_metadata(tmp_path, monkeypatch):
    import json
    import sys
    from naiwa.hook import handle
    monkeypatch.setenv("CURSOR_CODE_REMOTE", "true")
    handle("cursor", {"hook_event_name":"beforeSubmitPrompt", "conversation_id":"remote-probe",
                      "generation_id":"turn", "workspace_roots":["vscode-remote://ssh-remote+example-server/home/private/assignment-1"],
                      "prompt":"private prompt"}, root=tmp_path)
    diagnostic = json.loads((tmp_path/"probe-keys.jsonl").read_text(encoding="utf-8"))
    assert diagnostic["remote_workspace"] is True
    assert diagnostic["execution_platform"] == sys.platform
    assert diagnostic["workspace_name"] == "assignment-1"
    for name in ("events.jsonl", "probe-keys.jsonl"):
        content = (tmp_path/name).read_text(encoding="utf-8")
        assert "private prompt" not in content and "/home/private" not in content

