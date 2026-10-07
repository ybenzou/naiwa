"""An anchored, non-activating pixel character with pinnable live speech."""
from __future__ import annotations

import ctypes
import json
import os
import time
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal, QFileSystemWatcher
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QImage, QPainter, QRegion
from PySide6.QtWidgets import QApplication, QToolTip, QWidget

from naiwa.bus import EventReader, atomic_json, project_root, runtime_dir
from naiwa.geometry import integer_scale, visible_position
from naiwa.phase import PhaseMachine
from naiwa.multi_sprite import compose_motion, composition_key, transformed_arm, GLYPHS
from naiwa.motion import MotionPlayer
from naiwa.arm_motion import ArmPlayer
from naiwa.notices import StatusNotices, action, tool_action, tool_variant
from naiwa.speech import SpeechBlock, SpeechBoard, SpeechColumn, board_height, render_bubble
from naiwa.sprites import ANCHOR, HEIGHT, WIDTH, FOOT_Y, PALETTE, SHADOW, TRANSPARENT, hit, pose_frames, render_placeholder

WS_EX_TRANSPARENT = 0x20
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20
WM_NCHITTEST, WM_MOUSEACTIVATE = 0x84, 0x21
HTTRANSPARENT, HTCLIENT, MA_NOACTIVATE = -1, 1, 3
PET_FLAGS = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint |
             Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)


def window_flags():
    return PET_FLAGS


def _native_functions():
    user32 = ctypes.windll.user32
    get = user32.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user32.GetWindowLongW
    put = user32.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user32.SetWindowLongW
    get.restype, get.argtypes = ctypes.c_ssize_t, [ctypes.c_void_p, ctypes.c_int]
    put.restype, put.argtypes = ctypes.c_ssize_t, [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    return get, put


def exstyle(hwnd: int) -> int:
    if os.name != "nt" or not hwnd:
        return 0
    return int(_native_functions()[0](hwnd, GWL_EXSTYLE))


def clear_click_through_style(hwnd: int) -> int:
    if os.name != "nt" or not hwnd:
        return 0
    getter, setter = _native_functions()
    style = (int(getter(hwnd, GWL_EXSTYLE)) & ~WS_EX_TRANSPARENT) | WS_EX_NOACTIVATE
    setter(hwnd, GWL_EXSTYLE, style)
    return style


RGBA_BYTES = {p: bytes(p) for p in PALETTE}
INDEXED_PALETTE = sorted(PALETTE)
PALETTE_INDEX = {p: i for i, p in enumerate(INDEXED_PALETTE)}
COLOUR_TABLE = [QColor(*p).rgba() for p in INDEXED_PALETTE]


def _image(canvas, scale: int, ratio: float = 1.0, *, layer: str = "all") -> QImage:
    colours = PALETTE_INDEX.copy()
    if layer == "body":
        colours[SHADOW] = PALETTE_INDEX[TRANSPARENT]
    elif layer == "shadow":
        colours = {p: PALETTE_INDEX[SHADOW] if p == SHADOW else PALETTE_INDEX[TRANSPARENT] for p in PALETTE}
    if hasattr(canvas, "indices"):
        translation = bytes([colours[p] for p in INDEXED_PALETTE]+[0]*(256-len(INDEXED_PALETTE)))
        pixels = canvas.indices.translate(translation)
    else:
        pixels = bytes(colours[p] for row in canvas for p in row)
    height, width = len(canvas), len(canvas[0])
    image = QImage(pixels, width, height, width, QImage.Format.Format_Indexed8)
    image.setColorTable(COLOUR_TABLE)
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    image = image.scaled(width*scale, height*scale, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.FastTransformation)
    image.setDevicePixelRatio(ratio)
    return image


def _region(canvas, scale: float, top: float = 0, left: float = 0) -> QRegion:
    region = QRegion()
    for y, row in enumerate(canvas):
        start = None
        for x, pixel in enumerate(row + [TRANSPARENT]):
            opaque = pixel[3] == 255
            if opaque and start is None:
                start = x
            if start is not None and not opaque:
                x0, x1 = round(left+start*scale), round(left+x*scale)
                y0, y1 = round(top+y*scale), round(top+(y+1)*scale)
                region += QRegion(x0, y0, max(1, x1-x0), max(1, y1-y0))
                start = None
    return region


class DecorationWindow(QWidget):
    """A separate input-transparent layer preserves the shadow without blocking IDE clicks."""
    def __init__(self):
        super().__init__()
        self.setWindowFlags(PET_FLAGS | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.images: list[QImage] = []
        self.origin = QPointF()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        for image in self.images:
            painter.drawImage(self.origin, image)


class NaiwaWindow(QWidget):
    scaleChanged = Signal(int)

    def __init__(self, root: Path | None = None, placeholder: bool = False, *, zoom: int = 1, demo: bool = False):
        super().__init__()
        self.root = runtime_dir(root)
        self._allow_legacy_position = self.root.resolve() == runtime_dir().resolve()
        self.placeholder, self.demo = placeholder, demo
        self.reader = EventReader(self.root)
        self.machine = PhaseMachine()
        self.detail_open = not demo
        self._detail_until = self._bubble_until = 0.0
        self._phase_since = time.monotonic()
        self._motion = MotionPlayer()
        self._arms = ArmPlayer()
        self._body_agent = None
        self._health_since = 0.0
        self._render_seq = 0
        self._render_ts = 0.0
        self._paint_seq = 0
        self._paint_ts = 0.0
        self._state_key = None
        self._notices = StatusNotices()
        self._notice_text = ""
        self._anchor = ANCHOR
        self._composition = None
        self._composition_key = None
        self._badges = {}
        self._detail_page = 0
        self._source_pages = {}
        self._detail_source = None
        self._detail_agent = None
        self._tooltip_key = None
        self._tooltip_state = None
        self._press = None
        self._origin = QPoint()
        self._moved = False
        self._zoom = zoom
        self._scale = 1
        self._ratio = 1.0
        self._canvas = render_placeholder()
        self._bubble = None
        self._bubble_height = 0.0
        self._detail_slot = 0.0
        self._detail_slot_key = None
        self._bubble_origin = QPointF()
        self._body_origin = QPointF()
        self._name_image = None
        self._badge = QRect()
        self._cache: OrderedDict[tuple, QImage] = OrderedDict()
        self._body_layers = OrderedDict()
        self._decor_cache = {}
        self._name_cache = {}
        self._bubble_cache = OrderedDict()
        self._bubble_controls = ()
        self._restored = False
        self._io_error = False
        self._closed = False
        self.decoration = DecorationWindow()
        self.setWindowTitle("奶蛙")
        self.setWindowFlags(window_flags())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("QToolTip { background-color: #fff8db; color: #241f26; "
                          "border: 1px solid #241f26; border-radius: 0px; padding: 4px; }")
        self._anim = QTimer(self)
        self._anim.setInterval(40)
        self._anim.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim.timeout.connect(self._advance)
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._reload)
        self._watcher = QFileSystemWatcher(self)
        if not demo:
            # A fresh install has no events file yet; rotation can also replace
            # the watched file. Watch the directory so neither needs a poll.
            self._watcher.addPath(str(self.root))
            self._watcher.directoryChanged.connect(self._schedule_reload)
            bus = self.root/"events.jsonl"
            if bus.exists():
                self._watcher.addPath(str(bus))
            self._watcher.fileChanged.connect(self._schedule_reload)
        app = QGuiApplication.instance()
        if app is not None:
            app.screenAdded.connect(self._screen_added)
            app.screenRemoved.connect(self._screen_removed)
            for screen in app.screens():
                self._watch_screen(screen)
        self._reload()
        self._restore_position()
        self._anim.start()
        if not demo:
            self._poll.start()

    def _watch_screen(self, screen):
        screen.availableGeometryChanged.connect(self._screen_geometry)
        screen.logicalDotsPerInchChanged.connect(self._screen_geometry)

    def _screen_added(self, screen):
        self._watch_screen(screen)
        self._screen_geometry()

    def _screen_removed(self, _screen):
        self._screen_geometry()

    def _screen_geometry(self, *_):
        if not self._closed:
            self._sync()
            self.park()

    def _view(self):
        return self.machine.view(time.time())

    def _advance(self):
        self._sync()

    def _schedule_reload(self, *_):
        # A remote backlog updates the journal, checkpoint and health files for
        # every record. Rendering directly inside directoryChanged starves Qt's
        # paint events (including notifications from our own health writes).
        # Coalesce notifications into the existing 100 ms polling timer; keep
        # the animation/paint loop free, with bounded state-read latency.
        if not self.demo and not self._closed and not self._poll.isActive():
            self._poll.start()

    def _reload(self):
        if not self.demo:
            try:
                self.machine = self.reader.poll(self.machine)
                from naiwa.remote import refresh_connection_health
                refresh_connection_health(self.root, self.machine)
                self._io_error = False
            except TimeoutError:
                pass
            except (OSError, ValueError):
                self._io_error = True
            bus = str(self.root/"events.jsonl")
            if bus not in self._watcher.files() and Path(bus).exists():
                self._watcher.addPath(bus)
        self._sync()
        if not self.demo:
            # Read-only health evidence from the actual running pet, once/second.
            # Ignore lock contention without displaying a one-frame error bubble.
            now = time.monotonic()
            if now-self._health_since >= 1:
                self._health_since = now
                try:
                    view = self._view()
                    atomic_json(self.root/"reader-health.json", {"pid": os.getpid(), "last_seq": self.machine.last_seq,
                        "ts": time.time(), "read_error": self._io_error,
                        "pose": view.pose, "animation_pose": self._motion.pose,
                        "render_seq": self._render_seq, "render_ts": self._render_ts,
                        "paint_seq": self._paint_seq, "paint_ts": self._paint_ts,
                        "dragging": self._press is not None,
                        "active_sources": {source: sum(a.source == source and a.pose in {"working", "tool", "needs_you"}
                                                       for a in view.agents) for source in ("cursor", "codex")},
                        "agents": [{"source": a.source, "workspace_name": a.workspace_name,
                                    "label": a.label, "pose": a.pose, "activity": a.activity,
                                    "updated_seconds": a.updated_seconds} for a in view.agents],
                        "sources": {source: sum(t.source == source for t in self.machine.turns.values())
                                    for source in ("cursor", "codex")}})
                except OSError:
                    pass

    def showEvent(self, event):
        super().showEvent(event)
        clear_click_through_style(int(self.winId()))
        self.decoration.show()
        self.park()

    def _frame_image(self, pose, index, layer="body"):
        key = (pose, index, self._scale, self._ratio, layer)
        if key not in self._cache:
            canvas = render_placeholder() if pose == "placeholder" else pose_frames(pose)[index]
            self._cache[key] = _image(canvas, self._scale, self._ratio, layer=layer)
        return self._cache[key]

    def foot_position(self) -> QPointF:
        factor = self._scale / self._ratio
        return QPointF(self.pos()) + self._body_origin + QPointF(self._anchor[0]*factor, self._anchor[1]*factor)

    def _sync(self):
        now = time.monotonic()
        view = self._view()
        if self._tooltip_key:
            agent = self._tooltip_agent(self._tooltip_key)
            state = (agent.turn_id, agent.pose, agent.wait_kind, agent.workspace_name, agent.tool_name, agent.activity) if agent else None
            if state != self._tooltip_state:
                self._hide_tooltip()
        state_key = view.pose
        if state_key != self._state_key:
            self._state_key = state_key
            self._phase_since = now
        notice = self._notices.observe(view.agents, now)
        if notice != self._notice_text:
            self._notice_text = notice
            self._bubble_until = self._notices.until if notice else 0
        previous_foot = self.foot_position() if self._restored else None
        screen = self.screen() or QGuiApplication.primaryScreen()
        if previous_foot is not None:
            screen = next((s for s in QGuiApplication.screens() if s.availableGeometry().contains(previous_foot.toPoint())), screen)
        self._ratio = screen.devicePixelRatio() if screen else 1.0
        self._scale = integer_scale(self._ratio, self._zoom)
        factor = self._scale/self._ratio
        pose = {"idle": "rest", "stopped": "idle", "error": "droop"}.get(view.pose, view.pose)
        index = 0
        tick = int(now*25) % 80
        if self.placeholder:
            pose, index = "placeholder", 0
            self._canvas = render_placeholder()
            self._anchor = ANCHOR
            self._composition = None
            self._name_image = None
            self._body_image = self._frame_image(pose, index)
            shadow_image = self._frame_image(pose, index, "shadow")
        else:
            descriptor = composition_key(view.agents)
            descriptor, arm_states = self._arms.sample(descriptor, now)
            if pose == "rest" and descriptor and self._motion.pose not in {None, "rest"}:
                pose = "working"  # Finish retracting before lying down.
            if not descriptor:
                tick = 0  # No moving agent arms: reuse identical resting pixels.
            tools = [a for a in view.agents if a.pose == "tool"]
            running_tools = [a for a in tools if self.machine.turns.get(a.key) and self.machine.turns[a.key].tools]
            tools = running_tools or tools
            selected = next((a for a in tools if a.key == self._body_agent), None)
            if selected is None and tools:
                selected = min(tools, key=lambda a: (a.source, a.number))
                self._body_agent = selected.key
            elif not tools:
                self._body_agent = None
            body, motion_key = self._motion.sample(pose, now, tool_variant(selected.tool_name if selected else view.tool_name))
            from naiwa.motion import standing_blink
            settled = (self._motion._physical is None and self._motion._rise_tick is None and
                       (self._motion.transition is None or now-self._motion.since >= .64))
            body, blink = standing_blink(body, now) if settled else (body, 0)
            current_key = (pose, motion_key, descriptor, tick, arm_states, blink)
            if current_key != self._composition_key:
                self._composition = compose_motion(pose, body, descriptor, tick, arm_states)
                self._composition_key = current_key
            self._canvas = self._composition.canvas
            self._anchor = self._composition.anchor
            cache_key = ("multi", current_key, self._scale, self._ratio)
            if cache_key not in self._cache:
                # A large zoom used to retain hundreds of MB and free every
                # image together. Bound enlarged images and evict incrementally.
                image_bytes = len(self._canvas[0])*HEIGHT*4*self._scale*self._scale
                limit = max(2, min(64, (32*1024*1024)//image_bytes))
                while len(self._cache) >= limit:
                    self._cache.popitem(last=False)
                self._cache[cache_key] = self._composite_image()
            self._cache.move_to_end(cache_key)
            self._body_image = self._cache[cache_key]
            shadow_key = ("shadow", len(self._canvas[0]), self._scale, self._ratio,
                          tuple(tuple(p == SHADOW for p in row) for row in self._canvas[FOOT_Y+4:FOOT_Y+9]))
            accent_key = ("accent", pose, len(self._composition.accents[0]), self._scale, self._ratio)
            if len(self._decor_cache) > 32:
                self._decor_cache.clear()
            if shadow_key not in self._decor_cache:
                self._decor_cache[shadow_key] = _image(self._canvas, self._scale, self._ratio, layer="shadow")
            if accent_key not in self._decor_cache:
                self._decor_cache[accent_key] = _image(self._composition.accents, self._scale, self._ratio)
            shadow_image, accents_image = self._decor_cache[shadow_key], self._decor_cache[accent_key]
            name_key = (self._composition.labels, self._scale, self._ratio)
            if len(self._name_cache) > 32:
                self._name_cache.clear()
            if name_key not in self._name_cache:
                self._name_cache[name_key] = self._make_name_labels(self._composition)
            self._name_image = self._name_cache[name_key]
        lines = self._detail_board() if self.detail_open else [notice] if notice and now < self._bubble_until else []
        self._bubble = self._make_bubble(lines) if lines else None
        if not self._bubble:
            self._bubble_controls = ()
        bubble_w = self._bubble.deviceIndependentSize().width() if self._bubble else 0
        actual_height = self._bubble.deviceIndependentSize().height() if self._bubble else 0
        self._bubble_height = actual_height
        if self.detail_open and self._bubble:
            available = screen.availableGeometry().height()-HEIGHT*factor-24 if screen else actual_height+48
            slot_key = (self._ratio, self._zoom, available)
            if slot_key != self._detail_slot_key:
                self._detail_slot = 0
                self._detail_slot_key = slot_key
            if actual_height > self._detail_slot:
                self._detail_slot = min(available, actual_height+48)
            # Reallocating a translucent native backing surface for every action
            # line can blank the entire pet. Keep a small reserve and anchor the
            # console's bottom; routine status changes only repaint its pixels.
            self._bubble_height = max(actual_height, self._detail_slot)
        else:
            self._detail_slot = 0
        canvas_width = len(self._canvas[0])
        width = max(canvas_width*factor, bubble_w)
        height = self._bubble_height + HEIGHT*factor
        self._body_origin = QPointF((width-canvas_width*factor)/2, self._bubble_height)
        self._bubble_origin = QPointF((width-bubble_w)/2, self._bubble_height-actual_height)
        if previous_foot is not None and screen:
            # The wider console may slide sideways near a screen edge. Keep the
            # pet's contact point fixed instead of dragging it with the console.
            area = screen.availableGeometry()
            desired = previous_foot.x()-self._body_origin.x()-self._anchor[0]*factor
            left = min(max(area.left(), desired), area.left()+max(0, area.width()-width))
            body_x = previous_foot.x()-left-self._anchor[0]*factor
            self._body_origin.setX(min(max(0, body_x), width-canvas_width*factor))
        previous_size = self.size()
        self.setFixedSize(round(width), round(height))
        if previous_foot is not None:
            local_foot = self._body_origin + QPointF(self._anchor[0]*factor, self._anchor[1]*factor)
            old_position = self.pos()
            self.move((previous_foot-local_foot).toPoint())
            if self._press is not None:
                # A live state/bubble can change our size during dragging. Keep
                # the next pointer delta relative to the adjusted window origin.
                self._origin += self.pos()-old_position
        self._badges = {}
        # Each Codex arm already carries its workspace / X-number. Avoid a
        # duplicate lower-right X1 aggregate badge and its invisible hit target.
        for source in ("cursor", "demo"):
            agents = [a for a in view.agents if a.source == source]
            if not agents:
                continue
            working = sum(a.pose in {"working", "tool"} for a in agents)
            waiting = sum(a.pose == "needs_you" for a in agents)
            label = {"cursor": "C", "codex": "X", "demo": "D"}[source]+str(working)
            if waiting:
                label += f" !{waiting}"
            extra = self._composition.overflow.get(source, 0) if self._composition else 0
            if extra:
                label += f" +{extra}"
            badge_width = max(30, len(label)*7+6)
            x = 4 if source != "codex" else canvas_width-badge_width-4
            rect = QRect(round(self._body_origin.x()+x*factor), round(self._body_origin.y()+203*factor),
                         round(badge_width*factor), round(15*factor))
            self._badges[source] = (rect, label)
        self._badge = next(iter(self._badges.values()))[0] if self._badges else QRect()
        self._apply_mask()
        self.decoration.setGeometry(self.geometry())
        self.decoration.origin = self._body_origin
        self.decoration.images = [shadow_image]
        if not self.placeholder:
            self.decoration.images.append(accents_image)
        self.decoration.update()
        self.update()
        self._render_seq = self.machine.last_seq
        self._render_ts = time.time()
        if self._restored and self.size() != previous_size:
            old_position = self.pos()
            self.park()
            if self._press is not None:
                self._origin += self.pos()-old_position

    def _detail_board(self):
        rows = self.machine.details(time.time())
        priority = {"needs_you": 0, "working": 1, "tool": 1, "stale": 2,
                    "error": 3, "done": 4, "stopped": 4}
        rows.sort(key=lambda row: priority.get(row["pose"], 5))
        screen = self.screen() or QGuiApplication.primaryScreen()
        available = screen.availableGeometry().height()-HEIGHT*self._scale/self._ratio-24 if screen else 600
        def make_board(capacity, compact=False):
            columns = []
            for source in ("cursor", "codex"):
                group = [row for row in rows if row["source"] == source or source == "cursor" and row["source"] == "demo"]
                size = 1 if compact else capacity
                pages = max(1, (len(group)+size-1)//size)
                page = self._source_pages.get(source, self._detail_page) % pages
                if self._detail_agent:
                    selected = next((i for i, row in enumerate(group) if f"{row['source']}:{row['session_id']}" == self._detail_agent), None)
                    if selected is not None:
                        page = selected//size
                blocks = tuple(self._row_block(row) for row in group[page*size:(page+1)*size])
                seen = any(t.source == source or source == "cursor" and t.source == "demo" for t in self.machine.turns.values())
                columns.append(SpeechColumn(source, blocks, len(group),
                    sum(row["pose"] in {"working", "tool"} for row in group),
                    sum(row["pose"] == "needs_you" for row in group),
                    sum(row["pose"] in {"done", "stopped"} for row in group), page, pages,
                    "暂无工作中的会话" if seen else "尚未收到本地事件"))
            return SpeechBoard(tuple(columns), "暂时读不到状态 · 保留上次记录" if self._io_error else "",
                               compact, bool(self._detail_agent or self._detail_source))
        # Measure real wrapped text and selected history, rather than guessing
        # how many rows will fit from the number of characters in a name.
        for capacity in (3, 2, 1):
            board = make_board(capacity)
            if board_height(board, self._ratio) <= available:
                return board
        return make_board(1, compact=True)

    def _row_block(self, row):
        labels = {"working": "正在思考", "tool": "运行工具", "needs_you": "等你批准",
                  "done": "完成", "stopped": "本轮结束", "error": "出错", "stale": "信号较久未更新"}
        source = {"cursor": "Cursor", "codex": "Codex", "demo": "演示"}[row["source"]]
        current_action = tool_action(row["tool_name"]) if row["pose"] == "tool" else "等你回答" if row["wait_kind"] == "input" else labels.get(row["pose"], "")
        if row["pose"] == "working" and row["activity"] == "compacting":
            current_action = "正在整理上下文"
        elif row["activity"] == "disconnected":
            current_action = "SSH 连接中断，状态未知"
        seconds = row["elapsed"]
        age = f"{seconds}秒" if seconds < 60 else f"{seconds//60}分{seconds%60:02d}秒"
        children = f" · 子代理 {row['subagents']}" if row["subagents"] else ""
        key = f"{row['source']}:{row['session_id']}"
        selected = key == self._detail_agent
        extra = []
        if selected:
            for item in reversed(row["recent_actions"][-4:]):
                outcome = {"done": "完成", "error": "失败", "running": "开始"}[item["result"]]
                extra.append(f"{tool_action(item['tool_name'])} · {outcome}")
        elif row["last_tool"] and row["pose"] in {"working", "stale"}:
            extra.append(f"刚刚：{tool_action(row['last_tool'])}")
        fresh = f"{row['updated_seconds']}秒未更新" if row["pose"] == "stale" else f"{row['updated_seconds']}秒前更新"
        return SpeechBlock(row["workspace_name"] or "工作区未知", f"{source} {row['label']}", current_action,
                           f"本轮 {age} · {fresh}{children}", tuple(extra), row["pose"], key,
                           row["wait_kind"], row["activity"], selected)

    def _detail_blocks(self):
        return [block for column in self._detail_board().columns for block in column.blocks]

    def _detail_lines(self):
        # Plain text access for diagnostics; rendering uses structured blocks.
        lines = []
        for block in self._detail_blocks():
            lines.append(block.title+(f" · {block.source}" if block.source else ""))
            if block.status:
                lines.append(f"{block.status} · {block.meta}")
            lines.extend(block.extra)
        if not lines:
            lines = [f"{column.source}: {column.empty}" for column in self._detail_board().columns]
        return lines

    def _composite_image(self):
        composition = self._composition
        if composition.body is None:
            return _image(self._canvas, self._scale, self._ratio, layer="body")
        frame = composition.body
        identity = id(frame)
        if identity not in self._body_layers:
            while len(self._body_layers) >= 128:
                self._body_layers.popitem(last=False)
            self._body_layers[identity] = (frame, _image(frame, 1, layer="body"))
        self._body_layers.move_to_end(identity)
        body_image = self._body_layers[identity][1]
        width = len(self._canvas[0])
        image = QImage(width, HEIGHT, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        for gesture, angle, left, extent, x, y in composition.arm_draws:
            arm, (ox, oy) = transformed_arm(gesture, angle, left, extent)
            if arm is not None:
                painter.drawImage(x+ox, y+oy, arm)
        for _, name, suffix, box, colour in composition.labels:
            painter.fillRect(QRect(*box), QColor(30, 26, 25))
            if not name:
                for at, char in enumerate(suffix):
                    for dy, bits in enumerate(GLYPHS.get(char, GLYPHS["?"])):
                        for dx in range(3):
                            if bits & (4 >> dx):
                                painter.fillRect(box[0]+3+at*4+dx, box[1]+3+dy, 1, 1, QColor(*colour))
        painter.drawImage(composition.body_offset, 0, body_image)
        painter.end()
        image = image.scaled(width*self._scale, HEIGHT*self._scale,
                             Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
        image.setDevicePixelRatio(self._ratio)
        return image

    def _make_name_labels(self, composition):
        if not composition.labels:
            return None
        base = QImage(len(composition.canvas[0]), HEIGHT, QImage.Format.Format_ARGB32)
        base.fill(Qt.GlobalColor.transparent)
        font = QFont("SimSun")
        font.setPixelSize(10)
        font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        painter = QPainter(base)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, False)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        for _, name, suffix, box, colour in composition.labels:
            if not name:
                continue
            rect = QRect(*box).adjusted(2, 0, -2, 0)
            tail = " "+suffix
            available = max(0, rect.width()-metrics.horizontalAdvance(tail))
            text = metrics.elidedText(name, Qt.TextElideMode.ElideRight, available)+tail
            painter.setPen(QColor(*colour))
            painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        painter.end()
        image = base.scaled(base.width()*self._scale, base.height()*self._scale,
                            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
        image.setDevicePixelRatio(self._ratio)
        return image

    def _make_bubble(self, lines):
        key = (lines if isinstance(lines, SpeechBoard) else tuple(lines), self._ratio)
        if key in self._bubble_cache:
            self._bubble_cache.move_to_end(key)
            image, self._bubble_controls = self._bubble_cache[key]
            return image
        controls = []
        image = render_bubble(lines, self._ratio, controls=controls)
        while self._bubble_cache and (len(self._bubble_cache) >= 16 or
              sum(value[0].sizeInBytes() for value in self._bubble_cache.values())+image.sizeInBytes() > 16*1024*1024):
            self._bubble_cache.popitem(last=False)
        self._bubble_controls = tuple(controls)
        self._bubble_cache[key] = (image, self._bubble_controls)
        return image

    def _apply_mask(self):
        if os.name == "nt" and QGuiApplication.platformName() == "windows":
            # Layered-window alpha handles cross-process holes. A QRegion mask would
            # clip the soft contact shadow; the separate decoration layer passes all input.
            return
        region = _region(self._canvas, self._scale/self._ratio, self._body_origin.y(), self._body_origin.x())
        if self._bubble:
            region += QRegion(round(self._bubble_origin.x()), round(self._bubble_origin.y()),
                              round(self._bubble.deviceIndependentSize().width()), round(self._bubble.deviceIndependentSize().height()-3))
        for rect, _ in self._badges.values():
            region += QRegion(rect)
        self.setMask(region)

    def _hits(self, position):
        if any(rect.contains(position.toPoint() if isinstance(position, QPointF) else position) for rect, _ in self._badges.values()):
            return True
        if self._bubble and position.y() < self._bubble_height-3:
            x = round((position.x()-self._bubble_origin.x())*self._ratio)
            y = round((position.y()-self._bubble_origin.y())*self._ratio)
            return self._bubble.pixelColor(x, y).alpha() > 0 if 0 <= x < self._bubble.width() and 0 <= y < self._bubble.height() else False
        factor = self._scale/self._ratio
        x, y = position.x()-self._body_origin.x(), position.y()-self._body_origin.y()
        return x >= 0 and y >= 0 and hit(self._canvas, int(x/factor), int(y/factor))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawImage(self._body_origin, self._body_image)
        if self._name_image is not None:
            painter.drawImage(self._body_origin, self._name_image)
        if self._bubble:
            painter.drawImage(self._bubble_origin, self._bubble)
        for source, (rect, label) in self._badges.items():
            painter.setPen(QColor(36, 31, 38))
            painter.setBrush(QColor(243, 61, 148) if "!" in label else QColor(36, 198, 218))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            font = QFont("Consolas")
            font.setPixelSize(max(9, round(12*self._scale/self._ratio)))
            font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        self._paint_seq = self._render_seq
        self._paint_ts = time.time()

    def toggle_details(self):
        self._hide_tooltip()
        self._detail_page = 0
        self._source_pages.clear()
        self._detail_source = self._detail_agent = None
        self.detail_open = not self.detail_open
        self._detail_until = float("inf")
        self._bubble_until = 0
        self._detail_slot = 0
        self._sync()
        self.park()
        self._save_position()

    def _click_details(self, position):
        self._hide_tooltip()
        for source, (rect, _) in self._badges.items():
            if rect.contains(position.toPoint()):
                self._detail_source, self._detail_agent = source, None
                self._detail_page = 0
                self.detail_open = True
                break
        else:
            if self.detail_open and position.y() < self._bubble_height:
                local = position-self._bubble_origin
                control = next(((action, value) for action, value, rect in self._bubble_controls
                                if QRectF(*rect).contains(local)), None)
                if not control:
                    return
                action, value = control
                if action == "close":
                    self.toggle_details()
                    return
                if action == "all":
                    self._detail_source = self._detail_agent = None
                    self._source_pages.clear(); self._detail_page = 0
                elif action in {"prev", "next"}:
                    column = next(c for c in self._detail_board().columns if c.source == value)
                    self._source_pages[value] = (column.page+(1 if action == "next" else -1)) % column.pages
                    if self._detail_agent and self._detail_agent.startswith(value+":"):
                        self._detail_agent = None
                elif action == "agent" and value:
                    self._detail_agent = None if self._detail_agent == value else value
                    self._detail_source = None
            else:
                key = self._agent_at(position)
                if key:
                    self._detail_source, self._detail_agent = None, key
                    self._detail_page = 0
                    self.detail_open = True
                else:
                    self.toggle_details()
                    return
        self._detail_until = float("inf")
        self._bubble_until = 0
        self._detail_slot = 0
        self._sync()
        self.park()
        self._save_position()

    def _agent_at(self, position):
        if self._bubble and position.y() < self._bubble_height:
            local = position-self._bubble_origin
            return next((value for action, value, rect in self._bubble_controls
                         if action == "agent" and QRectF(*rect).contains(local)), None)
        if not self._composition:
            return None
        factor = self._scale/self._ratio
        local = QPointF(position)-self._body_origin
        return self._composition.owners.get((int(local.x()/factor), int(local.y()/factor)))

    def _tooltip_agent(self, key):
        turn = self.machine.turns.get(key)
        if turn is None:
            return None
        agent = self.machine._agent(turn, time.time())
        if agent.pose == "idle" and turn.phase in {"done", "stopped", "error"}:
            agent = replace(agent, pose=turn.phase)
        return agent

    def _tooltip_text(self, key):
        agent = self._tooltip_agent(key)
        if agent is None:
            return ""
        source = {"cursor": "Cursor", "codex": "VS Code Codex", "demo": "演示"}[agent.source]
        name = agent.workspace_name or "工作区未知"
        return f"{name}\n{source} {agent.label} · {action(agent)}\n已用 {agent.elapsed_seconds}s · 子代理 {agent.subagents}"

    def _hide_tooltip(self):
        QToolTip.hideText()
        # Qt defers native tooltip hiding; hide our application's tooltip widget
        # now so a completed/removed agent cannot leave a stale work label behind.
        for widget in QApplication.allWidgets():
            if widget.isWindow() and widget.windowType() == Qt.WindowType.ToolTip:
                widget.hide()
        self._tooltip_key = self._tooltip_state = None

    def event(self, event):
        if event.type() in {QEvent.Type.UngrabMouse, QEvent.Type.Hide} and getattr(self, "_press", None) is not None:
            self._cancel_drag()
        if event.type() == QEvent.Type.ToolTip and hasattr(self, "_composition"):
            key = self._agent_at(event.pos())
            text = self._tooltip_text(key) if key else ""
            if text and self._press is None:
                self._tooltip_key = key
                agent = self._tooltip_agent(key)
                self._tooltip_state = (agent.turn_id, agent.pose, agent.wait_kind, agent.workspace_name, agent.tool_name, agent.activity)
                font = QFont("SimSun")
                font.setPixelSize(12)
                font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
                QToolTip.setFont(font)
                # HTML escaping prevents workspace characters from being treated
                # as tooltip markup; line breaks are retained as plain text.
                import html
                QToolTip.showText(event.globalPos(), "<qt>"+html.escape(text).replace("\n", "<br>")+"</qt>", self)
            else:
                self._hide_tooltip()
            return True
        return super().event(event)

    def set_zoom(self, zoom: int):
        if zoom not in {1, 2, 3}:
            raise ValueError("Zoom must be 1, 2, or 3")
        self._zoom = zoom
        self._sync()
        self.park()
        self._save_position()
        self.scaleChanged.emit(zoom)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._hits(event.position()):
            self._hide_tooltip()
            self._press = event.globalPosition().toPoint()
            self._origin = self.pos()
            self._moved = False
            event.accept()

    def mouseMoveEvent(self, event):
        if self._press is None:
            return
        if not event.buttons() & Qt.MouseButton.LeftButton:
            self._cancel_drag()
            return
        delta = event.globalPosition().toPoint()-self._press
        if delta.manhattanLength() >= QApplication.startDragDistance():
            self._moved = True
        if self._moved:
            self.move(self._origin+delta)
            self.decoration.move(self.pos())

    def _cancel_drag(self):
        self._press = None
        self._moved = False
        self._sync()
        self.park()
        self._save_position()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        clicked = not self._moved and self._hits(event.position())
        self._press = None
        if clicked:
            self._click_details(event.position())
        self.park()
        self._save_position()

    def nativeEvent(self, event_type, message):
        if os.name != "nt" or bytes(event_type) not in {b"windows_generic_MSG", b"windows_dispatcher_MSG"}:
            return False, 0
        import ctypes.wintypes as wintypes
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_MOUSEACTIVATE:
            return True, MA_NOACTIVATE
        if msg.message == WM_NCHITTEST:
            if self._press is not None:
                # Keep the release routed to the pressed pet across alpha holes.
                return True, HTCLIENT
            point = wintypes.POINT(ctypes.c_short(msg.lParam & 0xffff).value,
                                   ctypes.c_short((msg.lParam >> 16) & 0xffff).value)
            ctypes.windll.user32.ScreenToClient(ctypes.c_void_p(int(self.winId())), ctypes.byref(point))
            local = QPointF(point.x/self._ratio, point.y/self._ratio)
            return True, HTCLIENT if self._hits(local) else HTTRANSPARENT
        return False, 0

    def park(self):
        screens = []
        for screen in QGuiApplication.screens():
            area = screen.availableGeometry()
            screens.append((area.x(), area.y(), area.width(), area.height()))
        x, y = visible_position(self.x(), self.y(), self.width(), self.height(), screens)
        self.move(x, y)
        self.decoration.setGeometry(self.geometry())

    def _position_path(self):
        return self.root / "window.json"

    def _restore_position(self):
        raw = {}
        path = self._position_path()
        legacy = project_root() / "runtime" / "window.json"
        try:
            selected = path if path.exists() or not self._allow_legacy_position else legacy
            raw = json.loads(selected.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {}
            self._zoom = int(raw.get("zoom", self._zoom))
            if "zoom" in raw and raw.get("sprite_version") != 3:
                # Old 64x72 sprites were enlarged 2-4x; preserve their approximate
                # desktop height when migrating to the much denser native grid.
                self._zoom = max(1, min(3, round(self._zoom*72/HEIGHT)))
            if self._zoom not in {1, 2, 3}:
                self._zoom = 1
            self.detail_open = raw.get("detail_open", self.detail_open) is True
            self._sync()
            if "foot_x" in raw:
                local = self._body_origin + QPointF(self._anchor[0]*self._scale/self._ratio, self._anchor[1]*self._scale/self._ratio)
                self.move((QPointF(float(raw["foot_x"]), float(raw["foot_y"]))-local).toPoint())
            elif "x" in raw:
                self.move(int(raw["x"]), int(raw["y"]))
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            pass
        if not raw:
            screen = QGuiApplication.primaryScreen()
            if screen:
                area = screen.availableGeometry()
                self.move(area.right()-self.width()-32, area.bottom()-self.height()-36)
        self._restored = True
        # A saved foot can be nearer an edge than half the wide console. Reflow
        # the console around that foot before clamping the native window, or a
        # restart would pull the pet inward by the console's extra width.
        self._sync()
        self.park()

    def _save_position(self):
        foot = self.foot_position()
        try:
            atomic_json(self._position_path(), {"foot_x": foot.x(), "foot_y": foot.y(),
                                              "zoom": self._zoom, "sprite_version": 3,
                                              "detail_open": self.detail_open})
        except OSError:
            pass

    def reset_position(self):
        screen = QGuiApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.move(area.right()-self.width()-32, area.bottom()-self.height()-36)
            self.park()
            self._save_position()

    def closeEvent(self, event):
        self._hide_tooltip()
        self._closed = True
        self._anim.stop()
        self._poll.stop()
        self._save_position()
        self.decoration.close()
        super().closeEvent(event)
