"""Source-isolated turns and conservative lifecycle inference."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from naiwa.adapt import PENDING_CURSOR
from naiwa.schema import BusEvent, EVENTS, identifier, timestamp, token

STALE_AFTER_SECONDS = 180.0
DONE_SECONDS = 2.8
TOOL_HOLD_SECONDS = 0.9
ERROR_SECONDS = 15.0
RETAIN_SECONDS = 24 * 3600
MAX_TURNS = 256
DETAIL_RETAIN_SECONDS = 15 * 60
ACTIVE = {"working", "tool", "needs_you"}
TERMINAL = {"done", "stopped", "error", "idle"}
START_EVIDENCE = {"tool_start", "input_wait", "approval_wait", "heartbeat", "subagent_start", "compact_start", "compact_end"}
END_EVENTS = {"turn_done", "turn_interrupt", "turn_error", "turn_unconfirmed", "session_end"}


def parse_ts(value: str) -> float | None:
    stamp = timestamp(value)
    return datetime.fromisoformat(stamp).timestamp() if stamp else None


@dataclass
class Turn:
    source: str
    session_id: str
    turn_id: str
    phase: str = "working"
    tools: set[str] = field(default_factory=set)
    subagents: set[str] = field(default_factory=set)
    tool_names: dict[str, str] = field(default_factory=dict)
    waiting: set[str] = field(default_factory=set)
    tool_name: str = ""
    end_reason: str = ""
    ts: str = ""
    started_ts: str = ""
    last_seq: int = 0
    inferred: bool = False
    input_waiting: set[str] = field(default_factory=set)
    number: int = 0
    workspace_name: str = ""
    workspace_id: str = ""
    recent_actions: list[dict] = field(default_factory=list)
    action_count: int = 0
    previous_turn_ids: list[str] = field(default_factory=list)
    compacting: bool = False
    disconnected: bool = False
    tool_hold_at: float = 0.0
    tool_hold_name: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source, "session_id": self.session_id, "turn_id": self.turn_id,
            "phase": self.phase, "tools": sorted(self.tools), "subagents": sorted(self.subagents),
            "tool_names": self.tool_names, "waiting": sorted(self.waiting), "tool_name": self.tool_name,
            "end_reason": self.end_reason, "ts": self.ts, "started_ts": self.started_ts,
            "last_seq": self.last_seq, "inferred": self.inferred,
            "input_waiting": sorted(self.input_waiting), "number": self.number,
            "workspace_name": self.workspace_name, "workspace_id": self.workspace_id,
            "recent_actions": self.recent_actions, "action_count": self.action_count,
            "previous_turn_ids": self.previous_turn_ids,
            "compacting": self.compacting, "disconnected": self.disconnected,
            "tool_hold_ts": datetime.fromtimestamp(self.tool_hold_at, timezone.utc).isoformat() if self.tool_hold_at else "",
            "tool_hold_name": self.tool_hold_name,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Turn":
        event = BusEvent.from_dict({**raw, "event": "turn_start", "schema_version": 2, "seq": 0})
        phase = raw.get("phase", "working")
        if phase not in ACTIVE | TERMINAL:
            raise ValueError("Invalid saved phase")
        def ids(key):
            values = raw.get(key, [])
            return {identifier(v) for v in values if identifier(v)} if isinstance(values, list) else set()
        names = raw.get("tool_names", {})
        if not isinstance(names, dict):
            names = {}
        seq = raw.get("last_seq", 0)
        turn = cls(
            event.source, event.session_id, event.turn_id, phase=phase,
            tools=ids("tools"), subagents=ids("subagents"), waiting=ids("waiting"),
            tool_names={identifier(k): token(v) for k, v in names.items() if identifier(k)},
            tool_name=event.tool_name, end_reason=event.end_reason, ts=event.ts,
            started_ts=timestamp(raw.get("started_ts")) or event.ts,
            last_seq=seq if type(seq) is int and seq >= 0 else 0, inferred=raw.get("inferred") is True,
            input_waiting=ids("input_waiting"),
            number=raw.get("number", 0) if type(raw.get("number")) is int and raw["number"] > 0 else 0,
            workspace_name=event.workspace_name, workspace_id=event.workspace_id,
            recent_actions=[{"tool_name": token(x.get("tool_name")), "ts": timestamp(x.get("ts")),
                             "call_id": identifier(x.get("call_id")),
                             "result": x.get("result") if x.get("result") in {"running", "done", "error"} else "done"}
                            for x in raw.get("recent_actions", [])[-8:] if isinstance(x, dict)],
            action_count=raw.get("action_count", 0) if type(raw.get("action_count")) is int else 0,
            previous_turn_ids=[identifier(x) for x in raw.get("previous_turn_ids", [])[-16:] if identifier(x)],
            compacting=raw.get("compacting") is True, disconnected=raw.get("disconnected") is True,
            tool_hold_at=parse_ts(raw.get("tool_hold_ts", "")) or 0.0,
            tool_hold_name=token(raw.get("tool_hold_name")),
        )
        # Old collectors still produce snapshots without hold fields. Recover
        # the last completed tool so rotating the bus cannot flash thinking.
        if (not turn.tool_hold_at and turn.phase == "working" and not turn.tools
                and not turn.compacting and turn.recent_actions):
            last = turn.recent_actions[-1]
            if last["result"] in {"done", "error"}:
                turn.tool_hold_at = parse_ts(last["ts"]) or 0.0
                turn.tool_hold_name = last["tool_name"]
        return turn


@dataclass(frozen=True)
class AgentView:
    source: str
    session_id: str
    turn_id: str
    number: int
    pose: str
    tool_name: str
    wait_kind: str
    elapsed_seconds: int
    subagents: int
    workspace_name: str = ""
    workspace_id: str = ""
    last_tool: str = ""
    updated_seconds: int = 0
    action_count: int = 0
    activity: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.session_id}"

    @property
    def label(self) -> str:
        return f"{ {'cursor': 'C', 'codex': 'X', 'demo': 'D'}[self.source]}{self.number}"

    @property
    def display_label(self) -> str:
        return f"{self.workspace_name} · {self.label}" if self.workspace_name else self.label


@dataclass(frozen=True)
class PetView:
    pose: str
    bubble: str
    source: str
    tool_name: str
    active_sessions: int
    subagents: tuple[str, ...]
    elapsed_seconds: int
    silent_seconds: int = 0
    session_id: str = ""
    turn_id: str = ""
    agents: tuple[AgentView, ...] = ()


class PhaseMachine:
    def __init__(self, *, stale_after: float = STALE_AFTER_SECONDS) -> None:
        self.turns: dict[str, Turn] = {}
        self.last_seq = 0
        self.stale_after = stale_after

    @staticmethod
    def key(source: str, session_id: str) -> str:
        return f"{source}:{session_id}"

    def _number(self, source: str) -> int:
        return 1 + max((t.number for t in self.turns.values() if t.source == source), default=0)

    def apply(self, event: BusEvent) -> None:
        if not event.session_id or event.event not in EVENTS:
            return
        if event.seq and event.seq <= self.last_seq:
            return
        self.last_seq = max(self.last_seq, event.seq)
        incoming = parse_ts(event.ts)
        event = self._bind_pending(event)
        if not event.session_id:
            return
        current = self.turns.get(self.key(event.source, event.session_id))
        if current is not None and event.source == "cursor" and event.event == "heartbeat":
            # Actual Cursor traces interleave thought/response generation IDs
            # with the generation used by submit/tools/stop in the same chat.
            # They provide liveness, never positive evidence of a new round.
            # In particular an after-response notification must not resurrect
            # a finished round or retire the ID its real stop will use.
            event = replace(event, turn_id=current.turn_id)
        previous = parse_ts(current.ts) if current else None
        if not event.seq and previous is not None and incoming is not None and incoming < previous:
            return
        different_turn = bool(current and event.turn_id and event.turn_id != current.turn_id)
        if different_turn and (event.turn_id in current.previous_turn_ids
                               or previous is not None and incoming is not None and incoming < previous):
            # Delayed starts are as dangerous as delayed stops. A new journal
            # sequence describes arrival order, not the age of a generation.
            return
        # Cursor changes generation on a new user message. If submit is missing,
        # the old turn may still be active: don't wait three minutes to adopt
        # positive evidence of a new generation. Codex keeps its stricter rule.
        newer = previous is None or incoming is not None and incoming >= previous
        start_evidence = event.event in START_EVIDENCE and (
            event.source == "cursor" or current is not None and (
                current.phase in TERMINAL or self._pose(current, incoming or 0) == "stale"))
        # A stop for a previously unseen Cursor generation is authoritative only
        # when the old turn has no outstanding work. Known retired generations
        # are rejected above; tools, approvals and child agents prevent fallback.
        end_evidence = (event.source == "cursor" and event.event in END_EVENTS
                        and current is not None and current.phase in ACTIVE
                        and not (current.tools or current.waiting or current.input_waiting
                                 or current.subagents or current.compacting))
        recover = different_turn and newer and (start_evidence or end_evidence)
        if event.event == "turn_start" or recover:
            if current and event.turn_id and current.turn_id == event.turn_id:
                self._learn_workspace(current, event)
                if current.inferred:
                    current.inferred = False
                    current.started_ts = event.ts or current.started_ts
                return
            self.turns[self.key(event.source, event.session_id)] = Turn(
                event.source, event.session_id, event.turn_id, ts=event.ts, started_ts=event.ts, last_seq=event.seq,
                number=current.number if current else self._number(event.source),
                workspace_name=event.workspace_name or (current.workspace_name if current else ""),
                workspace_id=event.workspace_id if event.workspace_name else (current.workspace_id if current else ""),
                previous_turn_ids=((current.previous_turn_ids+[current.turn_id])[-16:] if current else []),
                inferred=bool(recover))
            self._prune(incoming)
            if not recover:
                return
            current = self.turns[self.key(event.source, event.session_id)]
        if current is None:
            if event.event not in START_EVIDENCE:
                return
            current = Turn(event.source, event.session_id, event.turn_id,
                           ts=event.ts, started_ts=event.ts, inferred=True, number=self._number(event.source),
                           workspace_name=event.workspace_name, workspace_id=event.workspace_id)
            self.turns[self.key(event.source, event.session_id)] = current
        if event.turn_id and current.turn_id and event.turn_id != current.turn_id:
            return
        if event.event in {"connection_lost", "connection_restored"}:
            if event.event == "connection_lost" and current.phase in ACTIVE:
                current.disconnected = True
            elif event.event == "connection_restored":
                current.disconnected = False
            return
        self._learn_workspace(current, event)
        if not current.turn_id and event.turn_id:
            current.turn_id = event.turn_id
        if event.event == "done_ack":
            if current.phase == "done" and event.turn_id == current.turn_id:
                current.phase = "idle"
            return
        if current.phase in TERMINAL:
            return
        current.ts = event.ts or current.ts
        current.last_seq = event.seq or current.last_seq
        call = event.tool_call_id or ("anonymous:" + event.tool_name if event.tool_name else "anonymous")
        if event.event in {"tool_start", "input_wait"}:
            current.compacting = False
            if call not in current.tools:
                current.action_count += 1
                current.recent_actions.append({"tool_name": event.tool_name, "ts": event.ts, "result": "running", "call_id": call})
                current.recent_actions = current.recent_actions[-8:]
            current.tools.add(call)
            current.tool_names[call] = event.tool_name
            current.tool_name = event.tool_name or current.tool_name
            current.tool_hold_at = 0.0
            if event.event == "input_wait":
                current.input_waiting.add(call)
        elif event.event in {"tool_end", "tool_fail", "input_resume"}:
            ending_input = call in current.input_waiting or event.event == "input_resume"
            history_name = event.tool_name or current.tool_names.get(call, "")
            if history_name:
                recent = next((x for x in reversed(current.recent_actions)
                               if (event.tool_call_id and x.get("call_id") == call
                                   or not event.tool_call_id and x["tool_name"] == history_name)), None)
                if recent is None:
                    recent = {"tool_name": history_name, "ts": event.ts, "result": "done", "call_id": call}
                    current.recent_actions.append(recent)
                    current.recent_actions = current.recent_actions[-8:]
                recent.update(ts=event.ts, result="error" if event.event == "tool_fail" else "done")
            if event.tool_call_id:
                ended = {event.tool_call_id}
            else:
                matches = {k for k in current.tools if current.tool_names.get(k) == event.tool_name}
                ended = matches if len(matches) == 1 else set()
            ended_names = {current.tool_names.get(k, "") for k in ended}
            for ended_call in ended:
                current.tools.discard(ended_call)
                current.tool_names.pop(ended_call, None)
                current.waiting.discard(ended_call)
                current.input_waiting.discard(ended_call)
            current.waiting.discard("anonymous")
            current.waiting.discard("anonymous:" + event.tool_name)
            for ended_name in ended_names:
                current.waiting.discard("anonymous:" + ended_name)
            if event.event == "input_resume" and call == "anonymous":
                current.tools.difference_update(current.input_waiting)
                for key in current.input_waiting:
                    current.tool_names.pop(key, None)
                current.input_waiting.clear()
            released = current.tool_name or event.tool_name
            current.tool_name = next(reversed(current.tool_names.values()), "")
            if (not current.tools and not current.waiting and not current.input_waiting
                    and incoming is not None and released and not ending_input):
                current.tool_hold_at = incoming
                current.tool_hold_name = released
            elif ending_input:
                current.tool_hold_at = 0.0
                current.tool_hold_name = ""
        elif event.event == "approval_wait":
            current.waiting.add(call)
            current.tool_name = event.tool_name or current.tool_name
        elif event.event in {"compact_start", "compact_end"}:
            current.compacting = event.event == "compact_start"
        elif event.event in {"subagent_start", "subagent_stop"}:
            child = event.subagent_id or event.tool_call_id
            if child:
                if event.event == "subagent_start":
                    current.subagents.add(child)
                else:
                    current.subagents.discard(child)
            elif event.event == "subagent_stop" and len(current.subagents) == 1:
                current.subagents.clear()
        elif event.event in {"turn_done", "turn_interrupt", "turn_error", "turn_unconfirmed", "session_end"}:
            current.phase = {"turn_done": "done", "turn_unconfirmed": "stopped", "turn_error": "error"}.get(event.event, "idle")
            current.end_reason = event.end_reason
            current.tools.clear()
            current.tool_names.clear()
            current.subagents.clear()
            current.waiting.clear()
            current.input_waiting.clear()
            current.compacting = False
            return
        current.phase = ("needs_you" if current.waiting or current.input_waiting else
                         "working" if current.compacting else "tool" if current.tools else "working")
        self._prune(incoming)

    @staticmethod
    def _learn_workspace(turn: Turn, event: BusEvent) -> None:
        # A late metadata-bearing hook can label a legacy/open turn, but a tool
        # changing directory or a child agent must not rename its parent mid-turn.
        if not turn.workspace_name and event.workspace_name and event.event not in {"subagent_start", "subagent_stop"}:
            turn.workspace_name, turn.workspace_id = event.workspace_name, event.workspace_id

    def _prune(self, now: float | None) -> None:
        if now is not None:
            for key, turn in list(self.turns.items()):
                stamp = parse_ts(turn.ts)
                if stamp is not None and now - stamp > RETAIN_SECONDS:
                    del self.turns[key]
        if len(self.turns) > MAX_TURNS:
            ordered = sorted(self.turns, key=lambda k: (self.turns[k].phase in ACTIVE, parse_ts(self.turns[k].ts) or 0))
            for key in ordered[:len(self.turns) - MAX_TURNS]:
                del self.turns[key]

    def acknowledge_done(self) -> None:
        for turn in self.turns.values():
            if turn.phase == "done":
                turn.phase = "idle"

    def _age(self, turn: Turn, now: float) -> float:
        stamp = parse_ts(turn.ts)
        return max(0.0, now - stamp) if stamp is not None else 0.0

    def _bind_pending(self, event: BusEvent) -> BusEvent:
        if event.source != "cursor":
            return event
        pending_key = self.key("cursor", PENDING_CURSOR)
        if event.session_id == PENDING_CURSOR:
            active = [turn for turn in self.turns.values()
                      if turn.source == "cursor" and turn.session_id != PENDING_CURSOR
                      and turn.phase in ACTIVE and not turn.disconnected]
            if event.event == "heartbeat":
                if len(active) == 1:
                    return replace(event, session_id=active[0].session_id, turn_id=event.turn_id or active[0].turn_id)
                if len(active) > 1:
                    return replace(event, session_id="")
                pending = self.turns.get(pending_key)
                if pending is None or pending.phase not in ACTIVE:
                    return replace(event, session_id="")
                return event
            if event.event == "turn_start":
                pending = self.turns.get(pending_key)
                if pending and pending.phase in ACTIVE and pending.turn_id:
                    return replace(event, turn_id=pending.turn_id)
            return event
        pending = self.turns.get(pending_key)
        target = self.key(event.source, event.session_id)
        if pending and pending.phase in ACTIVE and target not in self.turns:
            pending.session_id = event.session_id
            pending.turn_id = ""
            self.turns[target] = self.turns.pop(pending_key)
        return event

    def _pose(self, turn: Turn, now: float) -> str:
        if turn.disconnected and turn.phase in ACTIVE:
            return "stale"
        if turn.phase == "needs_you":
            return "needs_you"
        if turn.phase in ACTIVE and self._age(turn, now) >= self.stale_after:
            return "stale"
        if turn.phase in {"done", "stopped"} and self._age(turn, now) >= DONE_SECONDS:
            return "idle"
        if turn.phase == "error" and self._age(turn, now) >= ERROR_SECONDS:
            return "idle"
        # A burst of short reads would otherwise flash back to thinking between calls.
        # Include the exact end instant; excluding it produced a one-frame flash
        # on every completion. Tolerate sub-millisecond ISO timestamp rounding.
        if turn.phase == "working" and not turn.compacting and turn.tool_hold_at:
            age = now-turn.tool_hold_at
            if -.001 <= age <= TOOL_HOLD_SECONDS:
                return "tool"
        return turn.phase

    def view(self, now: float) -> PetView:
        self._prune(now)
        active = [t for t in self.turns.values() if t.phase in ACTIVE]
        candidates = [t for t in self.turns.values() if self._pose(t, now) != "idle"]
        agents = tuple(self._agent(t, now) for t in sorted(candidates, key=lambda t: (t.source, t.number)))
        if not candidates:
            return PetView("idle", "", "", "", len(active), (), 0)
        priority = {"needs_you": 0, "tool": 1, "working": 2, "done": 3, "stopped": 3, "error": 4, "stale": 5}
        chosen = min(candidates, key=lambda t: (priority[self._pose(t, now)], -(parse_ts(t.ts) or 0)))
        pose = self._pose(chosen, now)
        bubble = {"needs_you": "在等你批准", "done": "回合结束", "stopped": "本轮结束", "error": "任务出错", "stale": "暂时没有新消息"}.get(pose, "")
        if pose == "needs_you" and chosen.input_waiting:
            bubble = "在等你回答"
        started = parse_ts(chosen.started_ts or chosen.ts)
        elapsed = max(0, int(now - started)) if started is not None else 0
        children = tuple(f"{t.source}:{child}" for t in active for child in sorted(t.subagents))
        tool_name = chosen.tool_name or (chosen.tool_hold_name if pose == "tool" else "")
        return PetView(pose, bubble, chosen.source, tool_name, len(active), children,
                       elapsed, int(self._age(chosen, now)), chosen.session_id, chosen.turn_id, agents)

    def _agent(self, turn: Turn, now: float) -> AgentView:
        started = parse_ts(turn.started_ts or turn.ts)
        pose = self._pose(turn, now)
        tool_name = turn.tool_name or (turn.tool_hold_name if pose == "tool" else "")
        return AgentView(turn.source, turn.session_id, turn.turn_id, turn.number, pose,
                         tool_name, "input" if turn.input_waiting else "approval" if turn.waiting else "",
                         max(0, int(now-started)) if started is not None else 0, len(turn.subagents),
                         turn.workspace_name, turn.workspace_id,
                         turn.recent_actions[-1]["tool_name"] if turn.recent_actions else "",
                         int(self._age(turn, now)), turn.action_count,
                         "disconnected" if turn.disconnected else "compacting" if turn.compacting else "")

    def details(self, now: float) -> list[dict]:
        rows = []
        for turn in sorted(self.turns.values(), key=lambda t: ({"cursor": 0, "codex": 1, "demo": 2}[t.source], t.number)):
            pose = self._pose(turn, now)
            if pose == "idle":
                if turn.phase not in {"done", "stopped", "error"} or self._age(turn, now) > DETAIL_RETAIN_SECONDS:
                    continue
                pose = turn.phase
            started = parse_ts(turn.started_ts or turn.ts)
            finished = parse_ts(turn.ts) if turn.phase in TERMINAL else None
            rows.append({"source": turn.source, "pose": pose,
                         "tool_name": turn.tool_name or (turn.tool_hold_name if pose == "tool" else ""),
                         "elapsed": max(0, int((finished if finished is not None else now) - started)) if started is not None else 0,
                         "subagents": len(turn.subagents), "label": self._agent(turn, now).label,
                         "session_id": turn.session_id, "wait_kind": self._agent(turn, now).wait_kind})
            rows[-1].update(workspace_name=turn.workspace_name, workspace_id=turn.workspace_id)
            rows[-1].update(updated_seconds=int(self._age(turn, now)), action_count=turn.action_count,
                            activity="disconnected" if turn.disconnected else "compacting" if turn.compacting else "",
                            recent_actions=list(turn.recent_actions),
                            last_tool=turn.recent_actions[-1]["tool_name"] if turn.recent_actions else "")
        return rows

    def snapshot(self) -> dict:
        return {"version": 2, "last_seq": self.last_seq, "turns": [t.to_dict() for t in self.turns.values()]}

    @classmethod
    def from_snapshot(cls, payload: dict | None) -> "PhaseMachine":
        machine = cls()
        if not isinstance(payload, dict) or not isinstance(payload.get("turns"), list):
            return machine
        for raw in payload["turns"][-MAX_TURNS:]:
            try:
                turn = Turn.from_dict(raw)
            except (ValueError, TypeError, AttributeError):
                continue
            machine.turns[machine.key(turn.source, turn.session_id)] = turn
            if not turn.number:
                turn.number = machine._number(turn.source)
        seq = payload.get("last_seq", 0)
        machine.last_seq = seq if type(seq) is int and seq >= 0 else 0
        return machine

    def replay(self, events: list[BusEvent]) -> None:
        for event in events:
            self.apply(event)
