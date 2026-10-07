from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from naiwa.bus import EventReader, append_event, read_events
from naiwa.phase import PhaseMachine
from naiwa.remote import bundle_bytes, host_id, imported_event, install_host, link_event, receive, ssh_arguments
from naiwa.remote_agent import find_named, journal, merge_hooks, observe, prepare, remove_hooks, self_test
from naiwa.schema import BusEvent


def test_remote_requires_explicit_host_and_install_workspace_before_ssh(monkeypatch):
    from naiwa import remote
    def forbidden(*args, **kwargs):
        pytest.fail("Missing configuration must not connect to SSH")
    monkeypatch.setattr(remote, "receive", forbidden)
    monkeypatch.setattr(remote, "_exchange", forbidden)
    with pytest.raises(SystemExit) as result:
        remote.main([])
    assert result.value.code == 2
    with pytest.raises(SystemExit) as result:
        remote.main(["install", "--host", "example-server"])
    assert result.value.code == 2
    with pytest.raises(ValueError, match="workspace"):
        remote.install_host("example-server")

STAMP = datetime.now(timezone.utc).isoformat()
SAMPLE_PROJECT = "/home/sample_project/SAMPLE_PROJECT"


def remote_event(name="turn_start", **kwargs):
    return BusEvent("cursor", "shared-session", "turn", name, ts=STAMP,
                    workspace_name="workspace-b", workspace_id="ws-"+"a"*24, **kwargs)


def test_remote_hook_merges_idempotently_without_modifying_existing_handlers():
    existing = {"version": 1, "custom": True, "hooks": {"preToolUse": [{"command": "my-guard", "timeout": 7}],
                                                       "otherEvent": [{"command": "my-audit"}]}}
    result = merge_hooks(existing, Path("/home/user/.agent-pet/naiwa-remote/collector.pyz"), "/usr/bin/python3")
    assert result["hooks"]["preToolUse"][0] == existing["hooks"]["preToolUse"][0]
    assert result["hooks"]["otherEvent"] == existing["hooks"]["otherEvent"]
    assert result["custom"] is True and len(existing["hooks"]) == 2
    assert merge_hooks(result, Path("/home/user/.agent-pet/naiwa-remote/collector.pyz"), "/usr/bin/python3") == result
    assert result["hooks"]["preToolUse"][-1]["failClosed"] is False
    removed = remove_hooks(result, Path("/home/user/.agent-pet/naiwa-remote/collector.pyz"))
    assert removed["hooks"]["preToolUse"] == [{"command": "my-guard", "timeout": 7}]
    assert removed["hooks"]["otherEvent"] == [{"command": "my-audit"}]


@pytest.mark.parametrize("raw", ([], {"version": 99}, {"hooks": []}, {"hooks": {"preToolUse": ["bad"]}}))
def test_remote_install_rejects_corrupt_config_without_overwriting(raw):
    with pytest.raises(ValueError):
        merge_hooks(raw, Path("/safe/collector.pyz"), "python3")


def test_collector_streams_only_metadata_and_observes_each_lifecycle(tmp_path):
    payload = {"conversation_id": "one", "generation_id": "g", "workspace_roots": ["/private/workspace-b"],
               "prompt": "private prompt", "tool_input": {"command": "private command"},
               "tool_output": "private output", "transcript_path": "/private/transcript"}
    for name, fields in (("beforeSubmitPrompt", {}), ("preToolUse", {"tool_name": "Read", "tool_use_id": "r"}),
                         ("postToolUse", {"tool_name": "Read", "tool_use_id": "r"}),
                         ("preCompact", {}), ("stop", {"status": "completed"})):
        response = observe(tmp_path, name, io.BytesIO(json.dumps({**payload, **fields, "hook_event_name": name}).encode()))
        assert response.get("permission", "allow") == "allow"
    connection = journal(tmp_path)
    rows = connection.execute("SELECT data FROM events ORDER BY seq").fetchall()
    connection.close()
    assert len(rows) == 5
    serialized = " ".join(row[0] for row in rows)
    for secret in ("private prompt", "private command", "private output", "/private"):
        assert secret not in serialized
    machine = PhaseMachine()
    for row in rows:
        machine.apply(imported_event("example-server", json.loads(row[0])))
    assert machine.view(datetime.fromisoformat(STAMP).timestamp()).pose == "done"
    assert self_test(tmp_path)
    assert len(rows) == 5


def test_remote_thought_uses_transcript_uuid_and_not_the_path(tmp_path, monkeypatch):
    (tmp_path/"allow.json").write_text(json.dumps({"root": SAMPLE_PROJECT}), encoding="utf-8")
    conversation = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setenv("CURSOR_TRANSCRIPT_PATH", f"/secret/projects/{conversation}/{conversation}.jsonl")
    payload = {"hook_event_name": "afterAgentThought", "text": "private thought",
               "workspace_roots": ["vscode-remote://ssh-remote+example-server"+SAMPLE_PROJECT]}
    observe(tmp_path, "afterAgentThought", io.BytesIO(json.dumps(payload).encode()), require_allow=True)
    connection = journal(tmp_path)
    row = json.loads(connection.execute("SELECT data FROM events").fetchone()[0])
    connection.close()
    diagnostic = json.loads((tmp_path/"last-hook.json").read_text(encoding="utf-8"))
    assert row["event"] == "heartbeat" and row["session_id"] == conversation
    assert diagnostic["recovered"] == "transcript"
    blob = json.dumps(row)+json.dumps(diagnostic)
    assert "/secret/projects" not in blob and "private thought" not in blob


def test_sample_project_round_is_kept_and_another_directory_is_not(tmp_path):
    (tmp_path/"allow.json").write_text(json.dumps({"root": SAMPLE_PROJECT}), encoding="utf-8")
    sample_project = {"conversation_id": "chat", "generation_id": "gen", "prompt": "secret prompt",
               "workspace_roots": ["vscode-remote://ssh-remote+example-server"+SAMPLE_PROJECT]}
    other = {**sample_project, "conversation_id": "else", "workspace_roots": ["/home/sample_project/other"]}
    assert observe(tmp_path, "beforeSubmitPrompt", io.BytesIO(json.dumps({**sample_project, "hook_event_name": "beforeSubmitPrompt"}).encode()), require_allow=True) == {"continue": True}
    assert observe(tmp_path, "preToolUse", io.BytesIO(json.dumps({**sample_project, "hook_event_name": "preToolUse", "tool_name": "Grep", "tool_use_id": "g"}).encode()), require_allow=True)["permission"] == "allow"
    assert observe(tmp_path, "beforeSubmitPrompt", io.BytesIO(json.dumps({**other, "hook_event_name": "beforeSubmitPrompt"}).encode()), require_allow=True) == {"continue": True}
    connection = journal(tmp_path)
    rows = [json.loads(row[0]) for row in connection.execute("SELECT data FROM events ORDER BY seq")]
    connection.close()
    assert [row["event"] for row in rows] == ["turn_start", "tool_start"]
    blob = json.dumps(rows)
    assert "/home/sample_project" not in blob and "secret prompt" not in blob and "/home/sample_project/other" not in blob
    machine = PhaseMachine()
    for row in rows:
        machine.apply(imported_event("example-server", row))
    agents = machine.view(datetime.fromisoformat(STAMP).timestamp()).agents
    assert len(agents) == 1
    assert agents[0].workspace_name == "SAMPLE_PROJECT · example-server"
    assert agents[0].pose == "tool"


def test_prepare_writes_only_the_home_allow_file_and_hooks(tmp_path, monkeypatch, capsys):
    home = tmp_path/"home"
    (home/"SAMPLE_PROJECT").mkdir(parents=True)
    (home/"other").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    code = prepare(home/".agent-pet"/"naiwa-remote", "SAMPLE_PROJECT", "", home/".agent-pet"/"naiwa-remote"/"collector.pyz")
    assert code == 0
    message = json.loads(capsys.readouterr().out.strip())
    assert message == {"kind": "ready", "workspace": "SAMPLE_PROJECT"}
    allow = json.loads((home/".agent-pet"/"naiwa-remote"/"allow.json").read_text(encoding="utf-8"))
    assert allow["root"] == str((home/"SAMPLE_PROJECT").resolve())
    hooks = json.loads((home/".cursor"/"hooks.json").read_text(encoding="utf-8"))
    assert hooks["hooks"]["beforeSubmitPrompt"][0]["failClosed"] is False
    assert "other" not in json.dumps(message)


def test_prepare_stops_when_the_name_is_ambiguous(tmp_path, monkeypatch, capsys):
    home = tmp_path/"home"
    (home/"SAMPLE_PROJECT").mkdir(parents=True)
    (home/"work"/"SAMPLE_PROJECT").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    assert len(find_named("SAMPLE_PROJECT")) == 2
    assert prepare(tmp_path/"remote", "SAMPLE_PROJECT", "", tmp_path/"collector.pyz") == 2
    assert json.loads(capsys.readouterr().out)["count"] == 2
    assert not (home/".cursor"/"hooks.json").exists()


def test_prepare_fills_an_empty_hooks_file_and_keeps_corrupt_text(tmp_path, monkeypatch, capsys):
    home = tmp_path/"home"
    (home/"sample_project").mkdir(parents=True)
    hooks_path = home/".cursor"/"hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text("\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    bundle = home/".agent-pet"/"naiwa-remote"/"collector.pyz"
    assert prepare(home/".agent-pet"/"naiwa-remote", "sample_project", "", bundle) == 0
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert hooks["hooks"]["beforeSubmitPrompt"][0]["failClosed"] is False
    assert list((home/".agent-pet"/"naiwa-remote"/"backups").glob("cursor-hooks-*"))
    hooks_path.write_text("{not json", encoding="utf-8")
    assert prepare(home/".agent-pet"/"naiwa-remote", "sample_project", "", bundle) == 2
    assert hooks_path.read_text(encoding="utf-8") == "{not json"
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["reason"] == "hooks"


def test_noninteractive_ssh_cannot_ask_for_a_password():
    import shlex
    args = ssh_arguments("example-server")
    assert "BatchMode=yes" in args and "PreferredAuthentications=publickey" in args
    interactive = ssh_arguments("example-server", interactive=True)
    assert "BatchMode=yes" not in interactive and "PreferredAuthentications=publickey" not in interactive
    assert "password" not in " ".join(args + interactive).lower()
    program, dash_u, dash_c, script = shlex.split(args[-1])
    assert [program, dash_u, dash_c] == ["python3", "-u", "-c"]
    compile(script, "<bootstrap>", "exec")
    assert "from pathlib import Path" in script
    with pytest.raises(ValueError):
        ssh_arguments("example-server;rm")


@pytest.mark.parametrize("count", (1, 33))
def test_read_only_stream_reads_existing_records_without_changing_any_files(tmp_path, count):
    import os
    import hashlib
    import shlex
    root = tmp_path/".agent-pet"/"naiwa-remote"
    connection = journal(root)
    for _ in range(count):
        connection.execute("INSERT INTO events(data) VALUES(?)", (json.dumps(remote_event().to_dict()),))
    connection.commit()
    connection.close()
    def files():
        return {str(p.relative_to(tmp_path)): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in tmp_path.rglob("*") if p.is_file()}
    before = files()
    script = shlex.split(ssh_arguments("example-server", read_only=True)[-1])[-1]
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    try:
        subprocess.run([sys.executable, "-u", "-c", script], input=b'{"seq":0,"epoch":""}\n',
                       capture_output=True, timeout=1.5, env=env)
        pytest.fail("Listener unexpectedly exited")
    except subprocess.TimeoutExpired as result:
        messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert messages[0]["kind"] == "ready" and messages[0]["read_only"] is True
    records = []
    for message in messages:
        if message["kind"] == "event":
            records.append(message)
        elif message["kind"] == "batch":
            assert len(message["records"]) <= 16
            records.extend(message["records"])
    assert [record["seq"] for record in records] == list(range(1, count+1))
    assert all(record["event"]["session_id"] == "shared-session" for record in records)
    assert messages[-1]["kind"] == "pulse"
    assert files() == before


def test_read_only_stream_missing_collector_does_not_create_remote_directory(tmp_path):
    import os
    import shlex
    script = shlex.split(ssh_arguments("example-server", read_only=True)[-1])[-1]
    run = subprocess.run([sys.executable, "-u", "-c", script], input=b'{"seq":0,"epoch":""}\n',
                         capture_output=True, timeout=3,
                         env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    assert run.returncode == 2 and json.loads(run.stdout)["reason"] == "missing_collector"
    assert not (tmp_path/".agent-pet").exists()


@pytest.mark.parametrize("server_offset", (-300, 254))
def test_receipt_clock_keeps_short_remote_tools_stable_and_then_thinking(server_offset):
    from datetime import timedelta
    now = datetime.fromisoformat(STAMP)
    machine = PhaseMachine()
    for delta, event in ((0, "turn_start"), (.1, "tool_start"), (.2, "tool_end"),
                         (.65, "tool_start"), (.8, "tool_end")):
        remote = remote_event(event, tool_name="Read", tool_call_id="read-1")
        remote = replace(remote, ts=(now+timedelta(seconds=server_offset+delta)).isoformat())
        arrival = (now+timedelta(seconds=delta)).isoformat()
        machine.apply(imported_event("example-server", remote.to_dict(), received_at=arrival))
        if event == "tool_end":
            assert machine.view(now.timestamp()+delta+.1).pose == "tool"
    assert machine.view(now.timestamp()+.9).pose == "tool"
    assert machine.view(now.timestamp()+2).pose == "working"
    assert machine.view(now.timestamp()+181).pose == "stale"


def test_dead_receiver_health_marks_disconnect_without_ssh_and_fresh_pulse_restores(tmp_path):
    from naiwa.remote import refresh_connection_health
    now = datetime.fromisoformat(STAMP).timestamp()
    machine = PhaseMachine()
    machine.apply(imported_event("example-server", remote_event().to_dict()))
    health = tmp_path/f"remote-{host_id('example-server')}-health.json"
    health.write_text(json.dumps({"state": "connected", "ts": now-16}), encoding="utf-8")
    refresh_connection_health(tmp_path, machine, now)
    assert machine.view(now).agents[0].activity == "disconnected"
    assert machine.view(now).pose == "stale"
    health.write_text(json.dumps({"state": "connected", "ts": now}), encoding="utf-8")
    refresh_connection_health(tmp_path, machine, now)
    assert machine.view(now).agents[0].activity == ""
    assert machine.view(now).pose == "working"


def test_pet_starts_the_named_host_stream(tmp_path, monkeypatch):
    (tmp_path/"remotes.json").write_text(json.dumps({"hosts": [{"host": "example-server", "workspace": "SAMPLE_PROJECT"}]}), encoding="utf-8")
    started = []
    monkeypatch.setattr("naiwa.remote.subprocess.Popen", lambda args, creationflags=0: started.append((args, creationflags)) or object())
    from naiwa.remote import start_configured
    start_configured(tmp_path)
    assert len(started) == 1
    args, flags = started[0]
    assert args[1:5] == ["-m", "naiwa.remote", "--host", "example-server"]
    assert args[5:7] == ["--data-dir", str(tmp_path)]
    assert "password" not in " ".join(args).lower()
    assert flags == subprocess.CREATE_NO_WINDOW


def test_password_session_opens_a_console_and_is_not_stored(tmp_path, monkeypatch):
    (tmp_path/"remotes.json").write_text(json.dumps(
        {"hosts": [{"host": "example-server", "workspace": "SAMPLE_PROJECT", "interactive": True}]}), encoding="utf-8")
    started = []
    monkeypatch.setattr("naiwa.remote.subprocess.Popen", lambda args, creationflags=0: started.append((args, creationflags)) or object())
    from naiwa.remote import start_configured
    start_configured(tmp_path)
    args, flags = started[0]
    assert args[-1] == "--interactive"
    assert not any(part.lower() in {"password", "passwd"} or "password=" in part.lower() for part in args)
    assert flags == subprocess.CREATE_NEW_CONSOLE

    seen = {}
    def fake_run(args, input, stdout, stderr, timeout, env):
        seen["args"] = args
        seen["stderr"] = stderr
        seen["timeout"] = timeout
        assert b"password" not in input.lower()
        class Result:
            returncode = 0
            stdout = b'{"kind":"ready","workspace":"SAMPLE_PROJECT"}\n'
        return Result()
    monkeypatch.setattr("naiwa.remote.subprocess.run", fake_run)
    monkeypatch.setattr("naiwa.remote.bundle_bytes", lambda: b"bundle")
    install_host("example-server", "SAMPLE_PROJECT", root=tmp_path, interactive=True)
    assert "BatchMode=yes" not in seen["args"] and seen["stderr"] is None and seen["timeout"] == 180
    saved = json.loads((tmp_path/"remotes.json").read_text(encoding="utf-8"))
    assert saved["hosts"] == [{"host": "example-server", "workspace": "SAMPLE_PROJECT", "interactive": True}]


def test_install_remembers_alias_without_a_server_path(tmp_path, monkeypatch):
    def fake_run(args, input, capture_output, timeout, env):
        assert "BatchMode=yes" in args
        request = json.loads(input)
        assert request["workspace"] == "SAMPLE_PROJECT" and request["root"] == ""
        assert "password" not in request
        class Result:
            returncode = 0
            stdout = b'{"kind":"ready","workspace":"SAMPLE_PROJECT"}\n'
            stderr = b""
        return Result()
    monkeypatch.setattr("naiwa.remote.subprocess.run", fake_run)
    monkeypatch.setattr("naiwa.remote.bundle_bytes", lambda: b"bundle")
    install_host("example-server", "SAMPLE_PROJECT", root=tmp_path)
    saved = json.loads((tmp_path/"remotes.json").read_text(encoding="utf-8"))
    assert saved == {"hosts": [{"host": "example-server", "workspace": "SAMPLE_PROJECT"}]}


def test_remote_collector_fails_open_on_invalid_input(tmp_path):
    assert observe(tmp_path, "beforeSubmitPrompt", io.BytesIO(b"bad")) == {"continue": True}
    assert observe(tmp_path, "preToolUse", io.BytesIO(b"bad")) == {"permission": "allow"}


def test_portable_bundle_runs_without_qt_or_a_remote_pip_install(tmp_path, monkeypatch):
    bundle = tmp_path/"collector.pyz"
    bundle.write_bytes(bundle_bytes())
    with zipfile.ZipFile(bundle) as archive:
        assert "naiwa/remote_agent.py" in archive.namelist()
        assert "naiwa/phase.py" in archive.namelist()
        assert "naiwa/window.py" not in archive.namelist()
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path/".agent-pet"/"naiwa-remote"
    target.mkdir(parents=True)
    (target/"allow.json").write_text(json.dumps({"root": SAMPLE_PROJECT}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(bundle), "hook", "--event", "preToolUse"],
                            input=json.dumps({"conversation_id": "bundle", "generation_id": "g", "tool_name": "Read",
                                              "workspace_roots": ["vscode-remote://ssh-remote+example-server"+SAMPLE_PROJECT]}),
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"permission": "allow"}
    connection = journal(target)
    row = connection.execute("SELECT data FROM events").fetchone()
    connection.close()
    assert row is not None and "/home/sample_project" not in row[0] and row[0].count("SAMPLE_PROJECT") == 1


def test_host_namespaces_separate_local_and_two_remote_sessions():
    machine = PhaseMachine()
    events = [remote_event(), imported_event("example-server", remote_event().to_dict()),
              imported_event("other-server", remote_event().to_dict())]
    for event in events:
        machine.apply(event)
    agents = machine.view(datetime.fromisoformat(STAMP).timestamp()).agents
    assert len(agents) == 3 and len({agent.key for agent in agents}) == 3
    assert len({agent.workspace_id for agent in agents}) == 3
    assert {agent.workspace_name for agent in agents} == {"workspace-b", "workspace-b · example-server", "workspace-b · other-server"}
    assert all(agent.source == "cursor" for agent in agents)
    longest = replace(remote_event(), session_id="a"*160)
    assert imported_event("example-server", longest.to_dict()).session_id.startswith("ssh-"+host_id("example-server"))


def test_link_loss_preserves_work_and_wait_without_finishing_or_hiding_local_agent(tmp_path):
    remote = imported_event("example-server", remote_event().to_dict())
    append_event(tmp_path, remote)
    append_event(tmp_path, replace(remote, event="input_wait", tool_call_id="q"))
    append_event(tmp_path, remote_event())
    link_event(tmp_path, "example-server", True)
    machine = EventReader(tmp_path).poll(PhaseMachine())
    now = datetime.fromisoformat(STAMP).timestamp()
    view = {agent.key: agent for agent in machine.view(now).agents}
    assert view["cursor:"+remote.session_id].pose == "stale"
    assert view["cursor:"+remote.session_id].activity == "disconnected"
    assert view["cursor:shared-session"].pose == "working"
    restored = PhaseMachine.from_snapshot(machine.snapshot())
    assert restored.turns["cursor:"+remote.session_id].disconnected is True
    from naiwa.notices import action
    assert action(view["cursor:"+remote.session_id]) == "SSH 连接中断，状态未知"
    link_event(tmp_path, "example-server", False)
    machine = EventReader(tmp_path).poll(PhaseMachine())
    assert machine.turns["cursor:"+remote.session_id].ts == STAMP
    assert machine.turns["cursor:"+remote.session_id].phase == "needs_you"
    rows = machine.details(now)
    remote_row = next(row for row in rows if row["session_id"] == remote.session_id)
    assert remote_row["activity"] == ""


def test_receiver_checkpoints_skip_repeated_sequences_and_marks_disconnect(tmp_path, monkeypatch):
    epoch = "a"*32
    messages = [{"kind": "ready", "protocol": 1, "epoch": epoch, "self_test": True}]
    messages += [{"kind": "event", "epoch": epoch, "seq": 1, "event": remote_event().to_dict()}]*2
    messages += [{"kind": "event", "epoch": epoch, "seq": 2,
                  "event": remote_event("tool_start", tool_call_id="r", tool_name="Read").to_dict()}]
    class Child:
        stdin = io.BytesIO()
        stdout = io.BytesIO(b"".join((json.dumps(message)+"\n").encode() for message in messages))
        def wait(self, **kwargs):
            return 0
        def poll(self):
            return 0
    monkeypatch.setattr("naiwa.remote.subprocess.Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr("naiwa.remote.console_visible", lambda value: None)
    monkeypatch.setattr("builtins.input", lambda: (_ for _ in ()).throw(EOFError))
    receive("example-server", tmp_path, interactive=True)
    events = read_events(tmp_path/"events.jsonl")
    assert [event.event for event in events] == ["turn_start", "tool_start", "connection_lost", "connection_lost"]
    cursor = json.loads((tmp_path/("remote-"+host_id("example-server")+"-cursor.json")).read_text())
    assert cursor == {"epoch": epoch, "seq": 2}
    health = json.loads((tmp_path/("remote-"+host_id("example-server")+"-health.json")).read_text())
    assert health["state"] == "stopped" and health["self_test"] is True


def test_receiver_keeps_login_through_invalid_record_and_local_bus_contention(tmp_path, monkeypatch):
    from naiwa import remote
    import shlex
    epoch = "b"*32
    messages = [{"kind": "ready", "protocol": 1, "read_only": True},
                {"kind": "event", "epoch": epoch, "seq": 1, "event": {"prompt": "SECRET"}},
                {"kind": "event", "epoch": epoch, "seq": 2, "event": remote_event().to_dict()}]
    requests, launches = [], []
    class Request(io.BytesIO):
        def close(self):
            requests.append(json.loads(self.getvalue()))
            super().close()
    class Child:
        def __init__(self):
            self.stdin = Request()
            self.stdout = io.BytesIO(b"".join((json.dumps(message)+"\n").encode() for message in messages))
        def wait(self, **kwargs):
            return 0
        def poll(self):
            return 0
    def launch(args, **kwargs):
        launches.append(args)
        return Child()
    real_append = remote.append_event
    attempts = []
    def busy_once(root, event):
        attempts.append(event.event)
        if len(attempts) == 1:
            raise TimeoutError("bus busy")
        return real_append(root, event)
    monkeypatch.setattr(remote.subprocess, "Popen", launch)
    monkeypatch.setattr(remote, "append_event", busy_once)
    monkeypatch.setattr(remote, "console_visible", lambda value: None)
    monkeypatch.setattr("builtins.input", lambda: (_ for _ in ()).throw(EOFError))
    receive("example-server", tmp_path, interactive=True)
    assert len(launches) == 1 and shlex.split(launches[0][-1])[-1] == remote.READ_ONLY_STREAM
    assert requests == [{"seq": 0, "epoch": ""}]
    assert attempts[:2] == ["turn_start", "turn_start"]
    health = json.loads((tmp_path/f"remote-{host_id('example-server')}-health.json").read_text())
    assert health["read_only"] is True and health["skipped_remote_seq"] == 1
    assert health["last_remote_seq"] == 2 and health["last_error"] == "SSH connection closed"
    assert "SECRET" not in "".join(p.read_text() for p in tmp_path.glob("*.json*"))


def test_receiver_batches_history_and_finishes_without_replaying_duplicate_packet(tmp_path, monkeypatch):
    from naiwa import remote
    epoch = "c"*32
    packet = {"kind": "batch", "epoch": epoch, "records": [
        {"seq": 1, "event": remote_event().to_dict()},
        {"seq": 2, "event": remote_event("tool_start", tool_name="Read", tool_call_id="r").to_dict()},
        {"seq": 3, "event": remote_event("turn_done", end_reason="completed").to_dict()}]}
    messages = [{"kind": "ready", "protocol": 1, "read_only": True}, packet, packet]
    class Child:
        stdin = io.BytesIO()
        stdout = io.BytesIO(b"".join((json.dumps(message)+"\n").encode() for message in messages))
        def wait(self, **kwargs):
            return 0
        def poll(self):
            return 0
    commits = []
    append = remote.append_events
    def batch(root, events):
        commits.append(len(events))
        return append(root, events)
    monkeypatch.setattr(remote.subprocess, "Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr(remote, "append_events", batch)
    monkeypatch.setattr(remote, "console_visible", lambda value: None)
    monkeypatch.setattr("builtins.input", lambda: (_ for _ in ()).throw(EOFError))
    receive("example-server", tmp_path, interactive=True)
    assert commits == [3]
    events = read_events(tmp_path/"events.jsonl")
    assert [event.event for event in events] == ["turn_start", "tool_start", "turn_done"]
    machine = PhaseMachine()
    machine.replay(events)
    assert next(iter(machine.turns.values())).phase == "done"
    cursor = json.loads((tmp_path/f"remote-{host_id('example-server')}-cursor.json").read_text())
    assert cursor == {"epoch": epoch, "seq": 3}


@pytest.mark.parametrize("host", ("-oProxyCommand=bad", "host;bad", "host\ncommand", "user@host"))
def test_receiver_rejects_shell_or_ssh_option_injection(host):
    with pytest.raises(ValueError):
        host_id(host)
