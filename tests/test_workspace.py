import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from naiwa.adapt import adapt
from naiwa.bus import append_event, read_events, read_snapshot, rotate_if_needed
from naiwa.phase import PhaseMachine
from naiwa.schema import BusEvent, SCHEMA_VERSION
from naiwa.workspace import display_name, workspace_identity

NOW = 1791180000.0
STAMP = datetime.fromtimestamp(NOW, timezone.utc).isoformat()


@pytest.mark.parametrize("path,name", [
    (r"D:\private-parent\workspace-a", "workspace-a"),
    ("/home/private-user/项目 名称/", "项目 名称"),
    ("file:///D:/private-parent/workspace-a", "workspace-a"),
    ("vscode-remote://ssh-remote+server/home/private-user/api", "api"),
    (r"\\private-server\private-share\frontend", "frontend"),
])
def test_cross_platform_leaf_names_do_not_retain_parent_paths(path, name):
    workspace_name, identity = workspace_identity("codex", {"cwd": path})
    assert workspace_name == name
    assert identity.startswith("ws-")
    assert "private" not in workspace_name+identity


def test_cursor_workspace_roots_take_precedence_over_tool_cwd():
    event = adapt("cursor", {"hook_event_name": "preToolUse", "conversation_id": "a", "generation_id": "t",
                             "workspace_roots": [r"D:\private\workspace-a"], "cwd": r"D:\private\workspace-a\src"})
    assert event.workspace_name == "workspace-a"
    assert "D:" not in json.dumps(event.to_dict())


def test_multiroot_identity_is_stable_when_roots_reordered():
    one = workspace_identity("cursor", {"workspace_roots": ["/private/app", "/private/api"]})
    two = workspace_identity("cursor", {"workspace_roots": ["/private/api", "/private/app"]})
    assert one[0] == "app + api"
    assert one[1] == two[1]


def test_same_name_different_paths_and_remote_hosts_have_distinct_identity():
    roots = ("/one/api", "/two/api", "vscode-remote://ssh-remote+a/one/api", "vscode-remote://ssh-remote+b/one/api")
    identities = [workspace_identity("codex", {"cwd": root}) for root in roots]
    assert {name for name, _ in identities} == {"api"}
    assert len({key for _, key in identities}) == 4


def test_windows_file_uri_and_case_have_matching_identity():
    assert workspace_identity("codex", {"cwd": r"D:\private\workspace-a"})[1] == workspace_identity(
        "codex", {"cwd": "file:///d:/PRIVATE/WORKSPACE-A"})[1]


@pytest.mark.parametrize("value", [None, 3, [], "", "/", "C:/", "relative/folder", "https://example.com/repo", "/bad\npath"])
def test_invalid_or_missing_roots_stay_unknown(value):
    assert workspace_identity("codex", {"cwd": value}) == ("", "")


def test_display_metadata_is_plain_bounded_unicode_and_never_a_full_path():
    assert display_name("项目\u202e\n 名称") == "项目 名称"
    assert len(display_name("项"*200)) == 80
    assert display_name(r"D:\private\project") == ""
    raw = BusEvent("cursor", "a", "t", "turn_start").to_dict()
    decoded = BusEvent.from_dict({**raw, "workspace_name": "/private/full/path", "workspace_id": "/private/path"})
    assert not decoded.workspace_name
    assert not decoded.workspace_id


def test_event_versions_one_two_and_three_are_replayable():
    raw = BusEvent("cursor", "a", "t", "turn_start").to_dict()
    for version in (1, 2, SCHEMA_VERSION):
        assert BusEvent.from_dict({**raw, "schema_version": version}).event == "turn_start"


def start(cwd, session="a", turn="t"):
    return adapt("codex", {"hook_event_name": "UserPromptSubmit", "session_id": session, "turn_id": turn, "cwd": cwd}, ts=STAMP)


def test_workspace_is_stable_during_tool_changes_but_can_change_on_new_turn():
    machine = PhaseMachine()
    event = start("/private/workspace-a")
    machine.apply(event)
    machine.apply(adapt("codex", {"hook_event_name": "PreToolUse", "session_id": "a", "turn_id": "t",
                                 "cwd": "/private/workspace-a/src", "tool_name": "Shell"}, ts=STAMP))
    assert machine.view(NOW).agents[0].workspace_name == "workspace-a"
    machine.apply(start("/private/naiwa", turn="next"))
    agent = machine.view(NOW).agents[0]
    assert agent.display_label == "naiwa · X1"


def test_legacy_running_session_learns_name_without_late_or_child_renaming():
    machine = PhaseMachine()
    machine.apply(BusEvent("codex", "a", "t", "turn_start", ts=STAMP))
    machine.apply(replace(start("/private/wrong", turn="old"), event="heartbeat"))
    machine.apply(replace(start("/private/child"), event="subagent_start", subagent_id="child"))
    assert not machine.view(NOW).agents[0].workspace_name
    machine.apply(replace(start("/private/workspace-a"), event="heartbeat"))
    assert machine.view(NOW).agents[0].workspace_name == "workspace-a"


def test_same_workspace_parallel_sessions_remain_separate_and_survive_rotation(tmp_path):
    append_event(tmp_path, start("/private/workspace-a", session="a"))
    append_event(tmp_path, start("/private/workspace-a", session="b"))
    rotate_if_needed(tmp_path, max_bytes=1)
    machine = PhaseMachine.from_snapshot(read_snapshot(tmp_path))
    agents = machine.view(NOW).agents
    assert {a.display_label for a in agents} == {"workspace-a · X1", "workspace-a · X2"}
    assert len({a.workspace_id for a in agents}) == 1
    assert "private" not in (tmp_path/"snapshot.json").read_text()
