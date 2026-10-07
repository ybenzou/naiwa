import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtWidgets import QApplication

from naiwa.demo import demo_machine
from naiwa.sprites import SHADOW, WIDTH, HEIGHT
from naiwa.window import NaiwaWindow, WS_EX_TRANSPARENT, _image, exstyle, window_flags


def test_file_notification_burst_keeps_state_reads_bounded_and_allows_paint(tmp_path, monkeypatch):
    import time
    from datetime import datetime, timezone
    from PySide6.QtTest import QTest
    from naiwa.bus import append_event
    from naiwa.schema import BusEvent
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path)
    window.show()
    app.processEvents()
    reads = []
    poll = window.reader.poll
    def tracked(machine):
        reads.append(time.monotonic())
        return poll(machine)
    monkeypatch.setattr(window.reader, "poll", tracked)
    append_event(tmp_path, BusEvent("cursor", "remote", "round", "turn_start",
        ts=datetime.now(timezone.utc).isoformat(), workspace_name="remote-work"))
    for _ in range(1000):
        window._watcher.directoryChanged.emit(str(tmp_path))
        window._watcher.fileChanged.emit(str(tmp_path/"events.jsonl"))
    # A notification must not synchronously read, replay or render the bus.
    assert reads == []
    QTest.qWait(350)
    app.processEvents()
    assert 1 <= len(reads) <= 5
    assert window.machine.turns["cursor:remote"].phase == "working"
    assert window._paint_seq == window.machine.last_seq
    assert time.time()-window._paint_ts < .3
    window.close()


def test_codex_state_and_render_follow_live_events_during_drag(tmp_path):
    from datetime import datetime, timezone
    from PySide6.QtCore import QPoint
    from naiwa.bus import append_event
    from naiwa.schema import BusEvent
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path)
    window._anim.stop()
    window._poll.stop()
    window._press = QPoint(100, 100)
    window._origin = window.pos()
    window.show()
    for name, pose in (("turn_start", "working"), ("tool_start", "tool"),
                       ("approval_wait", "needs_you"), ("turn_done", "done")):
        append_event(tmp_path, BusEvent("codex", "vscode", "t", name,
            ts=datetime.now(timezone.utc).isoformat(), tool_name="Bash",
            tool_call_id="command", workspace_name="workspace-a", origin="vscode"))
        foot = window.foot_position()
        window._health_since = 0
        window._reload()
        app.processEvents()
        assert window._view().pose == pose
        assert window._motion.pose == pose
        assert (window.foot_position()-foot).manhattanLength() <= 2
        assert window._press is not None
        assert window._render_seq == window.machine.last_seq
        assert window._paint_seq == window.machine.last_seq
        assert any("workspace-a" in line for line in window._detail_lines())
    import json
    health = json.loads((tmp_path/"reader-health.json").read_text(encoding="utf-8"))
    assert health["pose"] == health["animation_pose"] == "done"
    assert health["render_seq"] == window.machine.last_seq and health["dragging"]
    assert health["active_sources"]["codex"] == 0
    window.close()


def test_lost_mouse_capture_cancels_drag_and_keeps_new_agent_live(tmp_path):
    from datetime import datetime, timezone
    from PySide6.QtCore import QEvent, QPoint
    from PySide6.QtGui import QMouseEvent
    from naiwa.schema import BusEvent
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    window._press, window._moved = QPoint(100, 100), True
    app.sendEvent(window, QEvent(QEvent.Type.UngrabMouse))
    assert window._press is None and not window._moved
    window._press = QPoint(100, 100)
    window.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(10, 10), QPointF(110, 110),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    assert window._press is None
    window.machine.apply(BusEvent("codex", "vscode", "t", "turn_start",
        ts=datetime.now(timezone.utc).isoformat(), workspace_name="workspace-a"))
    window._sync()
    assert window._motion.pose == "working"
    assert (tmp_path/"window.json").exists()
    window.close()


def test_same_workspace_codex_windows_have_two_arms_and_distinct_compaction_status(tmp_path):
    from datetime import datetime, timezone
    from naiwa.schema import BusEvent
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    stamp = datetime.now(timezone.utc).isoformat()
    for session in ("first-window", "second-window"):
        window.machine.apply(BusEvent("codex", session, "t", "turn_start", ts=stamp, workspace_name="workspace-b"))
    window.machine.apply(BusEvent("codex", "first-window", "t", "compact_start", ts=stamp))
    window.machine.apply(BusEvent("codex", "second-window", "t", "tool_start", ts=stamp, tool_name="Bash"))
    window.detail_open = True
    window._sync()
    assert len(window._composition.labels) == 2
    assert {row[2].split("+")[0] for row in window._composition.labels} == {"X1", "X2"}
    blocks = window._detail_blocks()
    assert len(blocks) == 2 and all(block.title == "workspace-b" for block in blocks)
    assert {block.status for block in blocks} == {"正在整理上下文", "运行命令"}
    window.close()


def test_old_cursor_completions_do_not_push_live_codex_windows_off_the_first_page(tmp_path):
    from datetime import datetime, timezone
    from naiwa.schema import BusEvent
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    stamp = datetime.now(timezone.utc).isoformat()
    for number in range(4):
        for event in ("turn_start", "turn_done"):
            window.machine.apply(BusEvent("cursor", f"old-{number}", "t", event, ts=stamp, workspace_name="finished"))
    for number, workspace in enumerate(("workspace-a", "workspace-b")):
        window.machine.apply(BusEvent("codex", f"live-{number}", "t", "turn_start", ts=stamp, workspace_name=workspace))
    column = next(c for c in window._detail_board().columns if c.source == "codex")
    assert {block.title for block in column.blocks} == {"workspace-a", "workspace-b"}
    window.close()


def test_window_does_not_take_focus_or_set_transparent_exstyle(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, placeholder=True)
    assert window.windowFlags() & window_flags()
    window.show()
    app.processEvents()
    style = exstyle(int(window.winId()))
    assert style & WS_EX_TRANSPARENT == 0
    assert window.focusPolicy().name == "NoFocus" or int(window.focusPolicy()) == 0
    window.close()


def test_bubble_and_zoom_preserve_the_foot_anchor(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    window.show()
    app.processEvents()
    window.move(200, 300)
    original = window.foot_position()
    window.machine = demo_machine("needs_you")
    window._sync()
    app.processEvents()
    assert (window.foot_position()-original).manhattanLength() <= 1
    window._bubble_until = 0
    window._sync()
    assert (window.foot_position()-original).manhattanLength() <= 1
    window.set_zoom(2)
    assert (window.foot_position()-original).manhattanLength() <= 2
    window.close()


def test_shadow_is_preserved_in_decoration_and_passes_body_hit_test(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    window.show()
    app.processEvents()
    shadow_pixels = [(x, y) for y, row in enumerate(window._canvas) for x, p in enumerate(row) if p == SHADOW]
    assert shadow_pixels
    x, y = shadow_pixels[0]
    factor = window._scale/window._ratio
    point = window._body_origin + QPointF((x+0.5)*factor, (y+0.5)*factor)
    assert not window._hits(point)
    assert window.decoration.windowFlags() & Qt.WindowType.WindowTransparentForInput
    image = window.decoration.images[0]
    assert image.pixelColor(x*window._scale, y*window._scale).alpha() == SHADOW[3]
    assert window._body_image.pixelColor(x*window._scale, y*window._scale).alpha() == 0
    window.close()


def test_details_stay_open_until_toggled_and_parallel_count_has_a_click_target(tmp_path):
    import time
    from naiwa.schema import BusEvent
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    window.machine = demo_machine("working")
    first = next(iter(window.machine.turns.values()))
    window.machine.apply(BusEvent("codex", "another", "another", "turn_start", ts=first.ts))
    window._sync()
    assert not window._badge.isEmpty()
    assert window._hits(window._badge.center())
    window.toggle_details()
    assert len(window._detail_lines()) == 4
    window._detail_until = time.monotonic()-1
    window._sync()
    assert window.detail_open
    window.toggle_details()
    assert not window.detail_open
    window.close()


def test_physical_pixels_remain_integer_on_fractional_dpi():
    from naiwa.sprites import pose_frames
    image = _image(pose_frames("idle")[0], 4, ratio=1.25)
    assert (image.width(), image.height()) == (WIDTH*4, HEIGHT*4)
    assert image.devicePixelRatio() == 1.25
    for y in range(HEIGHT):
        for x in range(WIDTH):
            assert image.pixelColor(x*4, y*4) == image.pixelColor(x*4+3, y*4+3)


@pytest.mark.parametrize("saved", ["[]", "null", '"invalid"', "17"])
def test_non_object_saved_position_does_not_prevent_startup(tmp_path, saved):
    app = QApplication.instance() or QApplication([])
    (tmp_path / "window.json").write_text(saved, encoding="utf-8")
    window = NaiwaWindow(root=tmp_path, demo=True)
    window.show()
    app.processEvents()
    assert app.primaryScreen().availableGeometry().contains(window.geometry())
    window.close()


def test_automatic_approval_bubble_stays_visible_at_screen_corner(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    window.show()
    app.processEvents()
    area = app.primaryScreen().availableGeometry()
    window.move(area.topLeft())
    window.machine = demo_machine("needs_you")
    window._sync()
    assert window._bubble is not None
    assert area.contains(window.geometry())
    assert window.decoration.geometry() == window.geometry()
    window.close()


def test_legacy_position_keeps_foot_and_migrates_size_for_dense_art(tmp_path):
    import json
    app = QApplication.instance() or QApplication([])
    (tmp_path/"window.json").write_text(json.dumps({"foot_x": 300, "foot_y": 400, "zoom": 3}))
    window = NaiwaWindow(root=tmp_path, demo=True)
    assert window._zoom == 1
    assert (window.foot_position()-QPointF(300, 400)).manhattanLength() <= 1
    window.close()
    saved = json.loads((tmp_path/"window.json").read_text())
    assert saved["sprite_version"] == 3


def test_fans_source_badges_and_hand_click_show_the_right_session(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    window.machine = demo_machine("multi")
    window._sync()
    assert set(window._badges) == {"cursor"}
    point, key = next(iter(window._composition.owners.items()))
    factor = window._scale/window._ratio
    local = window._body_origin+QPointF((point[0]+.5)*factor, (point[1]+.5)*factor)
    assert window._hits(local)
    window._click_details(local)
    assert window._detail_agent == key
    board = window._detail_board()
    assert any(block.agent_key == key and block.selected for column in board.columns for block in column.blocks)
    assert all(column.blocks for column in board.columns)
    window.close()


def test_codex_only_has_workspace_hand_target_without_lower_right_counter(tmp_path):
    import time
    from datetime import datetime, timezone
    from naiwa.schema import BusEvent
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    stamp = datetime.fromtimestamp(time.time(), timezone.utc).isoformat()
    window.machine.apply(BusEvent("codex", "one", "t", "turn_start", ts=stamp, workspace_name="我的工作区"))
    window._sync()
    assert not window._badges and window._badge.isEmpty()
    key, name, suffix, box, _ = window._composition.labels[0]
    assert name == "我的工作区" and suffix == "X1"
    factor = window._scale/window._ratio
    window._click_details(window._body_origin+QPointF((box[0]+box[2]/2)*factor, (box[1]+box[3]/2)*factor))
    assert window._detail_agent == key and window.detail_open
    block = window._detail_blocks()[0]
    assert block.title == name and block.source == "Codex X1" and block.status == "正在思考"
    window.close()


def test_speech_width_stays_stable_and_long_chinese_names_wrap_fully(tmp_path):
    from naiwa.speech import SpeechBlock, wrap
    from PySide6.QtGui import QFont, QFontMetrics
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    name = "这是一个非常长的中文工作区名称用于检查完整展示"
    short = window._make_bubble([SpeechBlock("FE", "Codex X1", "正在思考", "本轮 1秒")])
    long = window._make_bubble([SpeechBlock(name, "Codex X1", "正在思考", "本轮 100分01秒")])
    assert short.width() == long.width() and long.height() > short.height()
    font = QFont("SimSun"); font.setPixelSize(13)
    lines = wrap(name, QFontMetrics(font), 100)
    assert "".join(lines) == name and len(lines) > 1
    window.close()


def test_static_rest_reuses_enlarged_images_and_large_zoom_cache_is_bounded(tmp_path, monkeypatch):
    from naiwa.motion import MotionPlayer
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    window.set_zoom(3)
    window._cache.clear()
    window._motion = MotionPlayer()
    monkeypatch.setattr("naiwa.window.time.monotonic", lambda: 0)
    window._sync()
    first = window._body_image
    monkeypatch.setattr("naiwa.window.time.monotonic", lambda: .04)
    window._sync()
    assert window._body_image is first
    window.machine = demo_machine("multi")
    for tick in range(40):
        monkeypatch.setattr("naiwa.window.time.monotonic", lambda tick=tick: 1+tick/25)
        window._sync()
    assert sum(image.sizeInBytes() for image in window._cache.values()) <= 32*1024*1024
    window.close()


def test_wide_fan_and_side_lying_switch_preserves_contact_position(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("naiwa.window.time.monotonic", lambda: clock[0])
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    window.move(180, 240)
    foot = window.foot_position()
    window.machine = demo_machine("mixed")
    window._sync()
    for tick in range(10):
        clock[0] += .04
        window._sync()
    assert len(window._canvas[0]) == 320
    assert (window.foot_position()-foot).manhattanLength() <= 1
    window.machine = demo_machine("idle")
    window._sync()
    assert len(window._canvas[0]) == 320  # The arms finish retiring first.
    for tick in range(10):
        clock[0] += .04
        window._sync()
    assert len(window._canvas[0]) == 240
    assert not window._badges
    assert "正在思考" not in window._notice_text
    assert (window.foot_position()-foot).manhattanLength() <= 1
    window.close()


def test_every_session_can_be_reached_through_detail_pages(tmp_path):
    import time
    from datetime import datetime, timezone
    from naiwa.schema import BusEvent
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    stamp = datetime.fromtimestamp(time.time(), timezone.utc).isoformat()
    for i in range(11):
        window.machine.apply(BusEvent("cursor", f"s{i}", "t", "turn_start", ts=stamp))
    window._sync()
    window.toggle_details()
    lines = set()
    pages = next(c.pages for c in window._detail_board().columns if c.source == "cursor")
    for page in range(pages):
        window._source_pages["cursor"] = page
        lines.update(line for line in window._detail_lines() if "Cursor" in line)
    assert len(lines) == 11
    window.close()


def test_unicode_workspace_tag_tooltip_and_click_keep_full_name_and_number(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    window.machine = demo_machine("mixed")
    window._sync()
    agent = next(a for a in window._view().agents if a.workspace_name == "设计素材工作区")
    _, name, suffix, box, colour = next(label for label in window._composition.labels if label[0] == agent.key)
    assert name == agent.workspace_name
    assert "X3" in suffix
    assert name in window._tooltip_text(agent.key)
    assert "等你回答" in window._tooltip_text(agent.key)
    factor = window._scale/window._ratio
    point = window._body_origin+QPointF((box[0]+box[2]/2)*factor, (box[1]+box[3]/2)*factor)
    assert window._agent_at(point) == agent.key
    assert window._hits(point)
    window._click_details(point)
    assert any(name in line and "X3" in line for line in window._detail_lines())
    # Unicode is rendered to the separate UI image, on the same nearest-neighbour
    # physical grid. Background-only compositing must not erase the name text.
    assert any(window._name_image.pixelColor(x*window._scale, y*window._scale).alpha() > 0
               for y in range(box[1], box[1]+box[3]) for x in range(box[0], box[0]+box[2]))
    window.close()


def test_name_tags_never_overlap_the_body_or_other_tags_on_the_same_side(tmp_path):
    from naiwa.phase import AgentView
    from naiwa.multi_sprite import compose, composition_key, WING
    agents = tuple(AgentView(source, f"s{i}", "t", i, "tool", "Shell", "", 0, 0, "很长的工作区名称")
                   for source in ("cursor", "codex") for i in range(1, 9))
    for tick in range(4):
        frame = compose("working", 0, composition_key(agents), tick)
        for source in ("cursor", "codex"):
            boxes = [QRect(*box) for key, _, _, box, _ in frame.labels if key.startswith(source+":")]
            for i, rect in enumerate(boxes):
                assert rect.right() < WING if source == "cursor" else rect.left() >= WING+WIDTH
                assert all(not rect.intersects(other) for other in boxes[i+1:])


def test_tooltip_is_hidden_when_its_session_state_changes(tmp_path):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QToolTip
    from dataclasses import replace
    app = QApplication.instance() or QApplication([])
    window = NaiwaWindow(root=tmp_path, demo=True)
    window.machine = demo_machine("multi")
    window._sync()
    key, _, _, box, _ = window._composition.labels[0]
    factor = window._scale/window._ratio
    point = (window._body_origin+QPointF((box[0]+box[2]/2)*factor, (box[1]+box[3]/2)*factor)).toPoint()
    app.sendEvent(window, QHelpEvent(QEvent.Type.ToolTip, point, window.mapToGlobal(point)))
    assert window._tooltip_key == key
    turn = window.machine.turns[key]
    turn.phase = "done"
    window._sync()
    assert window._tooltip_key is None
    assert not QToolTip.isVisible()
    window.close()
