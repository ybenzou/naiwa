"""Two-source visibility, live state signals and constrained desktop layout."""
from datetime import datetime, timezone
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRect, QRectF
from PySide6.QtWidgets import QApplication
import pytest

from naiwa.schema import BusEvent
from naiwa.speech import board_height, state_label
from naiwa.window import NaiwaWindow


@pytest.fixture
def window(tmp_path):
    app = QApplication.instance() or QApplication([])
    pet = NaiwaWindow(tmp_path, demo=True)
    pet._anim.stop(); pet._poll.stop()
    pet.detail_open = True
    yield pet
    pet.close()


def emit(pet, source, session, event="turn_start", **fields):
    pet.machine.apply(BusEvent(source, session, "t", event,
        ts=datetime.now(timezone.utc).isoformat(), workspace_name=session, **fields))


def click(pet, action, value):
    box = next(rect for kind, key, rect in pet._bubble_controls if kind == action and key == value)
    pet._click_details(pet._bubble_origin+QRectF(*box).center())


def test_two_source_banks_remain_visible_even_when_only_codex_has_events(window):
    empty = window._detail_board()
    assert [c.source for c in empty.columns] == ["cursor", "codex"]
    assert all(c.total == 0 and not c.blocks for c in empty.columns)
    emit(window, "codex", "workspace-a")
    window._sync()
    cursor, codex = window._detail_board().columns
    assert cursor.total == 0 and not cursor.blocks
    assert codex.total == codex.working == 1 and codex.blocks[0].title == "workspace-a"
    assert window._bubble is not None


def test_column_page_buttons_and_card_selection_preserve_the_other_source(window):
    for source in ("cursor", "codex"):
        for i in range(8):
            emit(window, source, f"{source}-{i}")
    window._sync()
    before = window._detail_board()
    click(window, "next", "cursor")
    after = window._detail_board()
    assert after.columns[0].page == 1 and after.columns[0].blocks != before.columns[0].blocks
    assert after.columns[1] == before.columns[1]
    key = after.columns[0].blocks[0].agent_key
    click(window, "agent", key)
    selected = window._detail_board()
    assert any(b.agent_key == key and b.selected for b in selected.columns[0].blocks)
    assert selected.columns[1] == after.columns[1]
    click(window, "prev", "cursor")
    assert window._detail_agent is None and window._detail_board().columns[0].page == 0


def test_live_wait_tool_completion_and_compaction_have_distinct_readable_signals(window):
    emit(window, "cursor", "frontend", "tool_start", tool_name="Bash", tool_call_id="run")
    emit(window, "codex", "workspace-b")
    for event, expected in (("compact_start", "整理上下文"), ("input_wait", "等你回答"),
                            ("input_resume", "正在思考"), ("turn_done", "已完成")):
        emit(window, "codex", "workspace-b", event)
        window._sync()
        cursor, codex = window._detail_board().columns
        block = codex.blocks[0]
        assert state_label(block) == expected
        assert codex.waiting == int(event == "input_wait")
        assert codex.finished == int(event == "turn_done")
        assert cursor.working == 1 and cursor.blocks[0].status == "运行命令"
    assert "workspace-b" in window._tooltip_text("codex:workspace-b")


def test_wider_two_column_console_keeps_the_pet_foot_at_both_screen_edges(window):
    app = QApplication.instance()
    emit(window, "cursor", "frontend")
    emit(window, "codex", "workspace-a")
    window.detail_open = False; window._bubble_until = 0
    window._sync()
    area = app.primaryScreen().availableGeometry()
    for left in (area.left(), area.right()-window.width()+1):
        window.move(left, area.bottom()-window.height()-5)
        foot = window.foot_position()
        window.toggle_details()
        assert area.contains(window.geometry())
        assert (window.foot_position()-foot).manhattanLength() <= 2
        window.toggle_details()


def test_small_screen_measures_wrapped_names_and_selected_history_before_paging(window, monkeypatch):
    class Screen:
        def availableGeometry(self):
            return QRect(0, 0, 800, 610)
    monkeypatch.setattr(window, "screen", lambda: Screen())
    for source in ("cursor", "codex"):
        for i in range(6):
            name = f"{source}-{i}-这是完整保留的长工作区名称"
            emit(window, source, name)
            for tool in ("Read", "Bash", "ApplyPatch", "Read"):
                emit(window, source, name, "tool_start", tool_name=tool, tool_call_id=tool)
                emit(window, source, name, "tool_end", tool_name=tool, tool_call_id=tool)
    window._detail_agent = next(iter(window.machine.turns))
    board = window._detail_board()
    available = 610-224*window._scale/window._ratio-24
    assert board_height(board, window._ratio) <= available
    assert all(c.pages > 1 for c in board.columns)
    assert any(b.selected for b in board.columns[0].blocks)
    assert board.columns[1].blocks


def test_selected_agent_updates_do_not_resize_or_move_the_translucent_pet(window):
    emit(window, "codex", "workspace-a", "tool_start", tool_name="Read", tool_call_id="read")
    window._detail_agent = "codex:workspace-a"
    window._sync()
    original = window.geometry()
    body_origin = QPointF(window._body_origin)
    emit(window, "codex", "workspace-a", "tool_end", tool_name="Read", tool_call_id="read")
    window._sync()
    assert window.geometry() == original and window._body_origin == body_origin
    assert state_label(window._detail_board().columns[1].blocks[0]) == "执行工具"
    # The console can shrink within its reserved surface. Hit testing must use
    # the translated console origin rather than the invisible padding above it.
    assert window._bubble_origin.y() > 0
    box = next(rect for kind, key, rect in window._bubble_controls if kind == "agent" and key == "codex:workspace-a")
    card = window._bubble_origin+QRectF(*box).center()
    assert window._hits(card) and window._agent_at(card) == "codex:workspace-a"
    assert not window._hits(QPointF(window._bubble_origin.x()+10, 1))


def test_disconnected_remote_card_says_ssh_disconnected_in_the_status_heading(window):
    from naiwa.remote import imported_event
    from naiwa.schema import BusEvent
    from datetime import datetime, timezone
    event = imported_event("example-server", BusEvent("cursor", "remote", "t", "turn_start",
        ts=datetime.now(timezone.utc).isoformat(), workspace_name="server-work").to_dict())
    window.machine.apply(event)
    window.machine.turns["cursor:"+event.session_id].disconnected = True
    window._sync()
    card = next(block for block in window._detail_board().columns[0].blocks if "example-server" in block.title)
    assert state_label(card) == "SSH 已断开"
    assert card.status == "SSH 连接中断，状态未知"


def test_restart_restores_a_foot_near_the_left_edge_with_the_wide_console_open(tmp_path):
    import json
    app = QApplication.instance() or QApplication([])
    area = app.primaryScreen().availableGeometry()
    foot = QPointF(area.left()+170, area.bottom()-30)
    (tmp_path/"window.json").write_text(json.dumps({"foot_x":foot.x(), "foot_y":foot.y(),
        "zoom":1, "sprite_version":3, "detail_open":True}), encoding="utf-8")
    pet = NaiwaWindow(tmp_path, demo=True)
    try:
        assert area.contains(pet.geometry())
        assert (pet.foot_position()-foot).manhattanLength() <= 1
    finally:
        pet.close()
