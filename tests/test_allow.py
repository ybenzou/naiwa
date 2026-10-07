import json

from naiwa.allow import allow_response, is_observe_only
from naiwa.hook import handle


def test_cursor_permission_and_codex_stop_are_observe_only():
    samples = {
        ("cursor", "preToolUse"): {"permission": "allow"},
        ("cursor", "subagentStart"): {"permission": "allow"},
        ("cursor", "beforeSubmitPrompt"): {"continue": True},
        ("cursor", "stop"): {},
        ("codex", "PreToolUse"): {},
        ("codex", "PermissionRequest"): {},
        ("codex", "Stop"): {"continue": True},
        ("codex", "SubagentStop"): {"continue": True},
    }
    for (source, name), expected in samples.items():
        response = allow_response(source, name)
        assert response == expected
        assert is_observe_only(response)
        assert "ask" not in json.dumps(response)
        assert "followup_message" not in response
        assert "decision" not in response


def test_hook_still_allows_when_the_bus_cannot_be_written(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    response = handle(
        "cursor",
        {"hook_event_name": "preToolUse", "conversation_id": "conv-1", "tool_name": "Shell", "tool_input": {"command": "rm -rf /"}},
        root=blocker,
    )
    assert response == {"permission": "allow"}
