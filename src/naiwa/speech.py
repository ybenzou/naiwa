"""Cassette-inspired pixel instruments for two independent agent banks."""
from dataclasses import dataclass
from functools import lru_cache
import math
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPolygon
from naiwa.geometry import integer_scale

BOARD_WIDTH = 468
SOURCE_COLOURS = {"cursor": "#58d6c1", "codex": "#f3bd59"}
STATUS_COLOURS = {"working": "#66cfc4", "tool": "#e7b254", "needs_you": "#ff839e",
                  "done": "#98d394", "stopped": "#aab7bd", "error": "#ff8c72", "stale": "#e7b254"}

@dataclass(frozen=True)
class SpeechBlock:
    title: str
    source: str = ""
    status: str = ""
    meta: str = ""
    extra: tuple[str, ...] = ()
    pose: str = ""
    agent_key: str = ""
    wait_kind: str = ""
    activity: str = ""
    selected: bool = False

@dataclass(frozen=True)
class SpeechColumn:
    source: str
    blocks: tuple[SpeechBlock, ...] = ()
    total: int = 0
    working: int = 0
    waiting: int = 0
    finished: int = 0
    page: int = 0
    pages: int = 1
    empty: str = "暂无会话"

@dataclass(frozen=True)
class SpeechBoard:
    columns: tuple[SpeechColumn, ...]
    notice: str = ""
    compact: bool = False
    focused: bool = False

def wrap(text, metrics, width):
    result = []
    for paragraph in text.split("\n"):
        current = ""
        for char in paragraph:
            if current and metrics.horizontalAdvance(current+char) > width:
                result.append(current); current = ""
            current += char
        result.append(current)
    return result

def state_label(block):
    if block.activity == "disconnected":
        return "SSH 已断开"
    if block.pose == "needs_you":
        return "等你回答" if block.wait_kind == "input" else "等你批准"
    if block.activity == "compacting" and block.pose == "working":
        return "整理上下文"
    return {"working": "正在思考", "tool": "执行工具", "done": "已完成", "stopped": "本轮结束",
            "error": "出错", "stale": "状态未更新"}.get(block.pose, "状态提示")

class _Deck:
    def __init__(self, width, height, ratio):
        self.scale = integer_scale(ratio); self.unit = ratio/self.scale
        self.px = lambda n: max(1, round(n*self.unit))
        self.image = QImage(self.px(width), self.px(height), QImage.Format.Format_ARGB32)
        self.image.fill(Qt.GlobalColor.transparent)
        self.painter = QPainter(self.image)
        self.painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, False)
        self.fonts, self.metrics = {}, {}
        for role, size in (("heading", 14), ("title", 13), ("status", 13), ("meta", 10), ("tiny", 10)):
            font = QFont("Consolas" if role in {"heading", "tiny"} else "Microsoft YaHei")
            font.setPixelSize(self.px(size)); font.setBold(role in {"heading", "title", "status"})
            font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
            self.fonts[role], self.metrics[role] = font, QFontMetrics(font)
        self.ratio = ratio

    def rect(self, x, y, w, h, fill, outline=None):
        self.painter.setPen(QColor(outline) if outline else Qt.PenStyle.NoPen)
        self.painter.setBrush(QColor(fill))
        self.painter.drawRect(QRect(round(x*self.unit), round(y*self.unit), self.px(w), self.px(h)))

    def text(self, x, y, w, role, text, colour="#f6edd4", align=Qt.AlignmentFlag.AlignLeft):
        self.painter.setFont(self.fonts[role]); self.painter.setPen(QColor(colour))
        self.painter.drawText(QRect(round(x*self.unit), round(y*self.unit), self.px(w), self.metrics[role].height()+self.px(2)),
                              align | Qt.AlignmentFlag.AlignVCenter, text)

    def lines(self, text, role, width):
        return wrap(text, self.metrics[role], self.px(width))

    def line_height(self, role):
        return math.ceil(self.metrics[role].height()/self.unit)+2

    def control(self, controls, action, value, x, y, w, h):
        factor = self.scale/self.ratio
        controls.append((action, value, (round(x*self.unit)*factor, round(y*self.unit)*factor,
                                         self.px(w)*factor, self.px(h)*factor)))

    def icon(self, x, y, pose, colour, activity=""):
        # Shape and text identify the state as well as colour.
        self.rect(x, y, 26, 23, "#142025", "#526167")
        if pose == "needs_you":
            self.rect(x+11, y+4, 4, 10, colour); self.rect(x+11, y+17, 4, 3, colour)
        elif pose == "done":
            for dx, dy in ((5, 12), (8, 15), (11, 12), (14, 9), (17, 6)):
                self.rect(x+dx, y+dy, 4, 4, colour)
        elif pose == "error":
            for at in range(4, 19, 3):
                self.rect(x+at, y+at-1, 3, 3, colour); self.rect(x+at, y+21-at, 3, 3, colour)
        elif pose in {"stopped", "stale"}:
            self.rect(x+7, y+6, 12, 12, colour)
            if pose == "stale":
                self.rect(x+10, y+8, 3, 7, "#142025"); self.rect(x+13, y+12, 4, 3, "#142025")
        elif activity == "compacting":
            for at in range(3):
                self.rect(x+5+at*3, y+5+at*3, 3, 3, colour)
                self.rect(x+18-at*3, y+5+at*3, 3, 3, colour)
                self.rect(x+5+at*3, y+17-at*3, 3, 3, colour)
                self.rect(x+18-at*3, y+17-at*3, 3, 3, colour)
        elif pose == "tool":
            self.rect(x+7, y+6, 12, 12, colour)
            for dx, dy, w, h in ((11, 3, 4, 4), (11, 17, 4, 4), (4, 10, 4, 4), (18, 10, 4, 4)):
                self.rect(x+dx, y+dy, w, h, colour)
            self.rect(x+11, y+10, 4, 4, "#142025")
        else:
            for dx in (4, 15):
                self.rect(x+dx, y+7, 8, 9, colour); self.rect(x+dx+2, y+9, 4, 5, "#142025")
            self.rect(x+11, y+11, 5, 2, colour)

    def finish(self):
        self.painter.end()
        image = self.image.scaled(self.image.width()*self.scale, self.image.height()*self.scale,
                                  Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
        image.setDevicePixelRatio(self.ratio)
        return image

def _card_plan(deck, block, width, compact):
    names = deck.lines(block.title, "title", width-58)
    if compact:
        names = [deck.metrics["title"].elidedText(block.title, Qt.TextElideMode.ElideRight, deck.px(width-58))]
    height = 12+len(names)*deck.line_height("title")+28
    action = (deck.lines(block.status, "meta", width-20)
              if not compact and block.pose == "tool" else [])
    extra = [] if compact else [part for text in (block.meta, *block.extra)
                               if text for part in deck.lines(text, "meta", width-20)]
    height += (len(action)+len(extra))*deck.line_height("meta")+7
    return names, action, extra, height

@lru_cache(maxsize=64)
def _board_layout(board, ratio):
    width, pad, gap = BOARD_WIDTH, 12, 12
    column_width = (width-2*pad-gap)//2
    measure = _Deck(width, 8, ratio)
    plans = [[_card_plan(measure, block, column_width-12, board.compact) for block in column.blocks]
             for column in board.columns]
    row_heights = [sum(plan[3]+8 for plan in column)-8 if column else 75 for column in plans]
    header = 30 if board.compact else 63
    bay = max(75, *row_heights); outer_top = 24
    notice_height = (measure.line_height("meta")+8) if board.notice else 0
    height = outer_top+header+bay+32+notice_height+10
    measure.painter.end()
    return plans, header, bay, notice_height, height

def board_height(board, ratio):
    height = _board_layout(board, ratio)[-1]
    scale = integer_scale(ratio)
    return max(1, round(height*ratio/scale))*scale/ratio

def render_board(board, ratio, controls):
    width, pad, gap, outer_top = BOARD_WIDTH, 12, 12, 24
    column_width = (width-2*pad-gap)//2
    plans, header, bay, notice_height, height = _board_layout(board, ratio)
    deck = _Deck(width, height, ratio)
    deck.rect(0, 0, width-1, height-9, "#d4c7a3", "#18262b")
    deck.rect(3, 3, width-7, height-15, "#eee1bb", "#8b816d")
    deck.text(14, 4, 245, "tiny", "AGENT STATUS / 奶蛙", "#30444a")
    deck.text(width-83, 4, 65, "meta", "全览" if board.focused else "收起", "#30444a", Qt.AlignmentFlag.AlignRight)
    deck.control(controls, "all" if board.focused else "close", "", width-83, 1, 70, 20)
    for at in (5, width-8):
        deck.rect(at, 6, 3, 7, "#726a5c"); deck.rect(at, 8, 3, 2, "#e9dbb2")
    for index, column in enumerate(board.columns):
        x = pad+index*(column_width+gap); colour = SOURCE_COLOURS[column.source]
        deck.rect(x, outer_top, column_width, header+bay+23, "#17262c", "#516064")
        deck.rect(x+1, outer_top+1, column_width-2, 3, colour)
        deck.rect(x+9, outer_top+12, 6, 7, colour)
        deck.text(x+22, outer_top+7, 115, "heading", "CURSOR" if column.source == "cursor" else "CODEX", colour)
        deck.text(x+142, outer_top+10, column_width-151, "meta", f"{column.total} 会话", "#c6cbbd", Qt.AlignmentFlag.AlignRight)
        if not board.compact:
            for slot, (label, count, lamp) in enumerate((("工作", column.working, STATUS_COLOURS["working"]),
                                                       ("等你", column.waiting, STATUS_COLOURS["needs_you"]),
                                                       ("结束", column.finished, STATUS_COLOURS["done"]))):
                bx, by = x+7+slot*68, outer_top+34
                deck.rect(bx, by, 64, 21, "#263238", "#526167")
                deck.rect(bx+4, by+7, 4, 6, lamp if count else "#526167")
                deck.text(bx+12, by+3, 49, "meta", f"{label} {count}", lamp if count else "#a3b0ae")
        y = outer_top+header
        for block, plan in zip(column.blocks, plans[index]):
            names, action, extra, card_height = plan; bx, bw = x+6, column_width-12
            state_colour = STATUS_COLOURS.get(block.pose, colour)
            deck.rect(bx, y, bw, card_height, "#243239", colour if block.selected else "#65716d")
            deck.rect(bx+1, y+1, 4, card_height-2, state_colour); deck.rect(bx+6, y+1, bw-8, 2, "#38494d")
            deck.control(controls, "agent", block.agent_key, bx, y, bw, card_height)
            tag = block.source.rsplit(" ", 1)[-1]
            deck.rect(bx+bw-42, y+8, 32, 18, "#16262b", "#65716d")
            deck.text(bx+bw-41, y+9, 30, "tiny", tag, colour, Qt.AlignmentFlag.AlignCenter)
            text_y = y+7
            for text in names:
                deck.text(bx+10, text_y, bw-58, "title", text); text_y += deck.line_height("title")
            text_y += 4
            deck.icon(bx+10, text_y, block.pose, state_colour, block.activity)
            deck.rect(bx+42, text_y, bw-52, 23, state_colour)
            deck.text(bx+48, text_y+2, bw-64, "status", state_label(block), "#18262b")
            text_y += 28
            for text in action+extra:
                deck.text(bx+10, text_y, bw-20, "meta", text, "#c3cebf"); text_y += deck.line_height("meta")
            y += card_height+8
        if not column.blocks:
            deck.rect(x+6, y, column_width-12, bay, "#202d32", "#46575a")
            deck.icon(x+column_width//2-13, y+13, "stopped", "#637876")
            deck.text(x+12, y+45, column_width-24, "meta", column.empty, "#bcc6ba", Qt.AlignmentFlag.AlignCenter)
        fy = outer_top+header+bay+4
        if column.pages > 1:
            for dx, symbol, action in ((7, "<", "prev"), (column_width-35, ">", "next")):
                deck.rect(x+dx, fy, 28, 18, "#354349", "#778179")
                deck.text(x+dx, fy, 28, "tiny", symbol, colour, Qt.AlignmentFlag.AlignCenter)
                deck.control(controls, action, column.source, x+dx, fy, 28, 18)
            deck.text(x+40, fy, column_width-80, "tiny", f"{column.page+1:02d} / {column.pages:02d}", "#d6dbc9", Qt.AlignmentFlag.AlignCenter)
        else:
            deck.text(x+8, fy, column_width-16, "meta", "点击卡带查看最近动作" if column.blocks else "等待新事件", "#a7b4ac", Qt.AlignmentFlag.AlignCenter)
    if board.notice:
        deck.text(pad, height-notice_height-13, width-2*pad, "meta", board.notice, "#5f302b")
    painter = deck.painter; painter.setPen(QColor("#18262b")); painter.setBrush(QColor("#eee1bb"))
    painter.drawPolygon(QPolygon([QPoint(round((width//2-8)*deck.unit), round((height-9)*deck.unit)),
                                   QPoint(round((width//2+8)*deck.unit), round((height-9)*deck.unit)),
                                   QPoint(round(width//2*deck.unit), round((height-1)*deck.unit))]))
    return deck.finish()

def render_bubble(items, ratio, *, controls=None):
    controls = controls if controls is not None else []
    if isinstance(items, SpeechBoard):
        return render_board(items, ratio, controls)
    blocks = [item if isinstance(item, SpeechBlock) else SpeechBlock(item) for item in items]
    measure = _Deck(280, 8, ratio)
    lines = [(line, block.pose) for block in blocks for text in (block.title, block.status, block.meta, *block.extra)
             if text for line in measure.lines(text, "title", 250)]
    height = 24+len(lines)*measure.line_height("title")
    measure.painter.end(); deck = _Deck(280, height, ratio)
    deck.rect(0, 0, 279, height-1, "#eee1bb", "#18262b"); deck.rect(4, 4, 271, height-9, "#243239", "#65716d")
    y = 10
    for line, pose in lines:
        deck.text(14, y, 250, "title", line, STATUS_COLOURS.get(pose, "#f6edd4")); y += deck.line_height("title")
    return deck.finish()
