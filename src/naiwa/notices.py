"""Per-session transient notices so another working session cannot hide completion."""
from collections import deque

SOURCE_LABELS = {"cursor": "Cursor", "codex": "Codex", "demo": "演示"}


def tool_variant(name):
    """Group tool names by visible action, rather than restarting for every call."""
    short = name.rsplit(".", 1)[-1].lower().replace("_", "").replace("-", "")
    if short in {"read", "readfile", "grep", "glob", "search", "webrun", "websearch"}:
        return 0  # Reading / turning a page.
    if short in {"applypatch", "strreplace", "edit", "write", "writefile"}:
        return 2  # Writing with a pencil.
    if short in {"viewimage", "imagegenimagegen"}:
        return 3  # Inspecting a geometric object.
    return 1  # Typing / issuing a command, also the generic tool fallback.


def tool_action(name):
    short = name.rsplit(".", 1)[-1].lower().replace("_", "").replace("-", "")
    labels = {"bash": "运行命令", "shell": "运行命令", "execcommand": "运行命令",
              "exec": "运行工具", "read": "读取文件", "readfile": "读取文件",
              "grep": "搜索代码", "glob": "查找文件", "applypatch": "修改文件",
              "strreplace": "修改文件", "edit": "修改文件", "write": "写入文件",
              "viewimage": "查看图片", "webrun": "查询网页", "websearch": "查询网页",
              "imagegenimagegen": "生成图片", "requestuserinput": "等你回答", "requestuserinputasync": "等你回答",
              "askquestion": "等你回答"}
    return labels.get(short, f"运行工具 {name[:24]}" if name else "运行工具")


def action(agent):
    if agent.activity == "disconnected":
        return "SSH 连接中断，状态未知"
    if agent.pose == "needs_you":
        return "等你回答" if agent.wait_kind == "input" else "等你批准"
    if agent.pose == "working" and agent.activity == "compacting":
        return "正在整理上下文"
    return {"working": "正在思考", "tool": tool_action(agent.tool_name),
            "done": "完成", "stopped": "本轮结束", "error": "出错了", "stale": "信号较久未更新"}.get(agent.pose, "")


class StatusNotices:
    def __init__(self):
        self.seen = {}
        self.queue = deque(maxlen=32)
        self.current = ""
        self.until = 0.0
        self.current_key = ""
        self.current_state = None

    def observe(self, agents, now):
        next_seen = {a.key: (a.turn_id, a.pose, a.wait_kind, a.display_label, a.activity) for a in agents}
        if self.current_state:
            live = next_seen.get(self.current_key)
            if (self.current_state[1] in {"working", "tool", "needs_you"} and live != self.current_state
                    or live and live[0] != self.current_state[0]):
                self.current = ""
                self.until = now
        for agent in agents:
            state = next_seen[agent.key]
            previous = self.seen.get(agent.key)
            if state == previous:
                continue
            # Tool-name changes remain visible on the hand and in details, without
            # a bubble for every tool. Preserve independent terminal/wait notices.
            if previous and agent.pose in {"working", "tool"} and previous[0] == agent.turn_id and previous[1] in {"working", "tool"} and previous[3:] == state[3:]:
                continue
            message = f"{SOURCE_LABELS[agent.source]} {agent.display_label} · {action(agent)}"
            self.queue = deque((n for n in self.queue if n[0] != agent.key), maxlen=32)
            notice = (agent.key, message, 4.0 if agent.pose == "needs_you" else 2.5, state)
            if agent.pose == "needs_you":
                if self.current and now < self.until and self.current_key != agent.key:
                    self.queue.appendleft((self.current_key, self.current, max(.5, self.until-now), self.current_state))
                self.current_key, self.current, duration, self.current_state = notice
                self.until = now+duration
            elif agent.pose in {"done", "stopped", "error", "stale"}:
                if self.current_state and self.current_state[1] in {"working", "tool"}:
                    self.current = ""
                    self.until = now
                self.queue.appendleft(notice)
            else:
                self.queue.append(notice)
        self.seen = next_seen
        self.queue = deque((notice for notice in self.queue if notice[3][1] in {"done", "stopped", "error", "stale"}
                            or next_seen.get(notice[0]) == notice[3]), maxlen=32)
        if now >= self.until and self.queue:
            self.current_key, self.current, duration, self.current_state = self.queue.popleft()
            self.until = now+duration
        return self.current if now < self.until else ""
