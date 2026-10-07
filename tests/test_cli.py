import json
import pytest

from naiwa.__main__ import main
from naiwa.demo import PHASES, demo_machine
from naiwa.probe import report


def test_version_does_not_launch_desktop(capsys):
    from naiwa import __version__
    with pytest.raises(SystemExit) as result:
        main(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == f"naiwa {__version__}"


def test_cursor_only_install_ignores_broken_codex_and_preserves_other_hooks(tmp_path, monkeypatch, capsys):
    import naiwa.__main__ as cli
    monkeypatch.setenv("NAIWA_HOME", str(tmp_path / "data"))
    cursor, codex = tmp_path / "cursor.json", tmp_path / "codex.json"
    cursor.write_text('{"version":1,"hooks":{"stop":[{"command":"echo keep"}]}}')
    codex.write_text("{broken")
    monkeypatch.setattr(cli, "default_cursor_path", lambda: cursor)
    monkeypatch.setattr(cli, "default_codex_path", lambda: codex)
    original = cursor.read_bytes()
    assert main(["install", "--source", "cursor", "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert set(preview) == {"cursor"}
    assert cursor.read_bytes() == original
    assert main(["install", "--source", "cursor"]) == 0
    output = capsys.readouterr().out
    assert "Cursor:" in output and "Codex" not in output
    assert codex.read_text() == "{broken"
    assert main(["uninstall", "--source", "cursor"]) == 0
    assert json.loads(cursor.read_text())["hooks"] == {"stop": [{"command": "echo keep"}]}
    assert codex.read_text() == "{broken"


def test_sheet_command_exports_all_frames(tmp_path):
    assert main(["sheet", "--output", str(tmp_path)]) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["character"].startswith("奶娃")
    assert (manifest["width"], manifest["height"]) == (160, 224)
    assert len(list(tmp_path.glob("*.png"))) == 185
    assert manifest["physical_frames"] == 208
    assert (tmp_path/"whole-book-15.png").exists()
    assert (tmp_path/"whole-signals-7.png").exists()
    assert not (tmp_path/"rigid-body.png").exists()
    assert manifest["motion_version"] == 4
    assert (tmp_path/"physical-clips.json").exists()
    assert (tmp_path/"bridge-chin-5.png").exists()
    assert manifest["rest"]["width"] == 240
    assert (tmp_path/"arm-waiting.png").exists()
    assert manifest["animation_fps"] == 25
    assert (tmp_path/"motion-rise-5.png").exists()
    assert (tmp_path/"motion-write-5.png").exists()


def test_demo_states_never_write_production_events(tmp_path, monkeypatch):
    monkeypatch.setenv("NAIWA_HOME", str(tmp_path))
    for phase in PHASES:
        assert demo_machine(phase, now=1000).view(1000).pose == phase
    assert not (tmp_path / "events.jsonl").exists()


def test_probe_requires_fresh_ide_origin_records(tmp_path, monkeypatch):
    import naiwa.probe as probe
    monkeypatch.setattr(probe, "hook_health", lambda: {})
    rows = [
        {"source": "codex", "origin": "other", "observed": True, "hook_event_name": "Stop", "ts": "2026-10-05T06:00:00+00:00"},
        {"source": "codex", "origin": "vscode", "observed": True, "hook_event_name": "PreToolUse", "ts": "2026-10-05T06:01:00+00:00"},
        {"source": "cursor", "observed": True, "hook_event_name": "stop", "ts": "2026-10-05T06:00:00+00:00"},
    ]
    (tmp_path / "probe-keys.jsonl").write_text("\n".join(json.dumps(r) for r in rows)+"\n")
    observed = report(tmp_path, since="2026-10-05T06:00:30+00:00")["observed_events"]
    assert observed == {"cursor": [], "codex": ["PreToolUse"]}
