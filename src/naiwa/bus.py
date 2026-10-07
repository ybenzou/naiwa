"""Bounded, process-safe local JSONL journal and incremental reader."""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

from naiwa.schema import BusEvent

MAX_BYTES = 256 * 1024


class BusLock:
    def __init__(self, path: Path, timeout: float = 0.3) -> None:
        self.path, self.timeout, self._fh = path, timeout, None

    def __enter__(self) -> "BusLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self._fh.write(b"\0")
                    self._fh.flush()
                def acquire():
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                def acquire():
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    acquire()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Event bus is busy") from None
                    time.sleep(0.005)
        except BaseException:
            self._fh.close()
            self._fh = None
            raise
        return self

    def __exit__(self, *_) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_dir(root: Path | None = None) -> Path:
    path = Path(root) if root is not None else Path(os.environ.get("NAIWA_HOME") or Path.home() / ".agent-pet")
    path.mkdir(parents=True, exist_ok=True)
    return path


def events_path(root: Path | None = None) -> Path:
    return runtime_dir(root) / "events.jsonl"


def snapshot_path(root: Path | None = None) -> Path:
    return runtime_dir(root) / "snapshot.json"


def lock_path(root: Path | None = None) -> Path:
    return runtime_dir(root) / "bus.lock"


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        deadline = time.monotonic() + 1.0
        while True:
            try:
                temporary.replace(path)
                break
            except PermissionError:
                # Windows cannot replace a file while a reader has a handle
                # without FILE_SHARE_DELETE. The GUI polls SSH health while the
                # receiver writes it; retry the rename, keeping atomicity.
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
    finally:
        temporary.unlink(missing_ok=True)


def _decode(lines: bytes) -> list[BusEvent]:
    events = []
    for line in lines.splitlines():
        try:
            events.append(BusEvent.from_dict(json.loads(line)))
        except (ValueError, TypeError, UnicodeDecodeError, OverflowError):
            continue
    return events


def read_events(path: Path) -> list[BusEvent]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    return _decode(data[:data.rfind(b"\n") + 1])


def read_snapshot(root: Path | None = None) -> dict | None:
    try:
        raw = json.loads(snapshot_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _repair_tail(path: Path) -> None:
    if not path.exists() or not path.stat().st_size:
        return
    with path.open("r+b") as fh:
        fh.seek(-1, os.SEEK_END)
        if fh.read(1) == b"\n":
            return
        # A crashed writer's partial record must not absorb the next valid record.
        fh.seek(0)
        data = fh.read()
        fh.truncate(data.rfind(b"\n") + 1)


def _last_seq(path: Path, root: Path | None) -> int:
    if path.exists() and path.stat().st_size:
        with path.open("rb") as fh:
            fh.seek(max(0, path.stat().st_size - 8192))
            tail = _decode(fh.read())
        if tail:
            return max(e.seq for e in tail)
    raw = read_snapshot(root) or {}
    value = raw.get("last_seq", 0)
    return value if type(value) is int and value >= 0 else 0


def _compact(root: Path | None) -> None:
    path = events_path(root)
    machine = EventReader(root)._restore()
    machine.replay(read_events(path))
    # Snapshot first; seq filtering makes a crash between these writes replay-safe.
    atomic_json(snapshot_path(root), machine.snapshot())
    path.replace(path.with_name("events.prev.jsonl"))
    path.touch()


def append_event(root: Path | None, event: BusEvent) -> BusEvent:
    return append_events(root, [event])[0]


def append_events(root: Path | None, events: list[BusEvent]) -> list[BusEvent]:
    """Commit a bounded transport batch with one lock, write and compaction."""
    if not events:
        return []
    if len(events) > 256:
        raise ValueError("Event batch exceeds 256 records")
    validated = [BusEvent.from_dict(event.to_dict()) for event in events]
    path = events_path(root)
    with BusLock(lock_path(root)):
        _repair_tail(path)
        previous = _last_seq(path, root)
        validated = [replace(event, seq=previous+i+1) for i, event in enumerate(validated)]
        lines = "".join(json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))+"\n" for event in validated)
        with path.open("ab") as fh:
            fh.write(lines.encode("utf-8"))
            fh.flush()
        if path.stat().st_size >= MAX_BYTES:
            _compact(root)
    return validated


def write_snapshot(root: Path | None, payload: dict) -> None:
    with BusLock(lock_path(root)):
        atomic_json(snapshot_path(root), payload)


def rotate_if_needed(root: Path | None, snapshot: dict | None = None, max_bytes: int = MAX_BYTES) -> bool:
    # Rebuild under the lock: a caller-provided snapshot can lag a concurrent writer.
    with BusLock(lock_path(root)):
        path = events_path(root)
        if not path.exists() or path.stat().st_size < max_bytes:
            return False
        _compact(root)
        return True


def append_diagnostic(root: Path | None, payload: dict) -> None:
    path = runtime_dir(root) / "probe-keys.jsonl"
    with BusLock(lock_path(root)):
        _repair_tail(path)
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            path.replace(path.with_name("probe-keys.prev.jsonl"))
        with path.open("ab") as fh:
            fh.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


class EventReader:
    """Only consume new complete records; reload on rotation/truncation."""
    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        self.offset = 0
        self.identity = None
        self.loaded = False
        self.mark = b""
        self.remote_clock_offsets = {}

    def _restore(self):
        from naiwa.phase import PhaseMachine, ACTIVE
        base = runtime_dir(self.root)
        snapshot = read_snapshot(self.root)
        legacy = snapshot.get("phase_rules", 0) < 3 if isinstance(snapshot, dict) else True
        machine = PhaseMachine.from_snapshot(snapshot)
        archived = read_events(base/"events.prev.jsonl")
        current = read_events(events_path(self.root))
        journal = archived+current
        # The pet's validated state is separate from a writer's compaction
        # snapshot. A still-authenticated pre-upgrade writer can have older
        # generation rules; restarting the UI must not restore those rules.
        try:
            cached = json.loads((base/"reader-state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
        if isinstance(cached, dict) and cached.get("reader_schema") == 1:
            saved = PhaseMachine.from_snapshot(cached)
            latest = max([machine.last_seq]+[event.seq for event in journal])
            gap = sorted({event.seq for event in journal if saved.last_seq < event.seq <= latest})
            if (saved.last_seq <= latest and
                    (saved.last_seq == latest or gap and gap[0] == saved.last_seq+1
                     and gap[-1] == latest and len(gap) == latest-saved.last_seq)):
                machine = saved
                legacy = False
                missing = [event for event in archived if event.seq > machine.last_seq]
                machine.replay(self._remote_clock(machine, missing, restored=True))

        # Hook diagnostics contain only already-sanitized event identifiers and
        # outcomes. They retain real local Stop evidence even when a high-volume
        # SSH journal has rotated it out. Prefer actual bus rows when present.
        evidence = {}
        for name in ("probe-keys.prev.jsonl", "probe-keys.jsonl"):
            try:
                lines = (base/name).read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    row = json.loads(line)
                    if row.get("source") != "cursor" or row.get("observed") is not True:
                        continue
                    event = BusEvent.from_dict({key: row[key] for key in (
                        "source", "session_id", "turn_id", "event", "end_reason", "seq", "ts", "workspace_name") if key in row})
                    if event.seq:
                        evidence[event.seq] = event
                except (ValueError, TypeError, AttributeError):
                    continue
        for event in journal:
            if event.source == "cursor":
                evidence[event.seq] = event
        relevant = sorted((event for event in evidence.values() if event.seq <= machine.last_seq), key=lambda e: e.seq)
        replay = PhaseMachine()
        replay.replay(relevant)
        canonical, thoughts = {}, {}
        for event in relevant:
            key = machine.key(event.source, event.session_id)
            target = thoughts if event.event == "heartbeat" else canonical
            target.setdefault(key, set()).add(event.turn_id)
        for key, turn in list(machine.turns.items()):
            corrected = replay.turns.get(key)
            if (turn.source == "cursor" and turn.phase in ACTIVE and corrected is not None
                    and (turn.turn_id in thoughts.get(key, ()) or
                         legacy and corrected.turn_id in turn.previous_turn_ids
                         and not turn.tools and not turn.action_count and corrected.action_count > 0)
                    and turn.turn_id not in canonical.get(key, ())
                    and corrected.turn_id != turn.turn_id
                    and corrected.last_seq >= turn.last_seq):
                corrected.number = turn.number
                corrected.workspace_name = corrected.workspace_name or turn.workspace_name
                corrected.workspace_id = corrected.workspace_id or turn.workspace_id
                machine.turns[key] = corrected
        return machine

    def _remote_clock(self, machine, events, restored=False):
        """Migrate future-dated events from an already authenticated old reader.

        New receivers timestamp on arrival. This compatibility path permits
        updating the local pet while keeping the current SSH process alive.
        It never modifies the journal, SSH connection, or the remote server.
        """
        from naiwa.phase import parse_ts
        now = time.time()
        newest = {}
        for turn in machine.turns.values() if restored else ():
            if turn.session_id.startswith("ssh-") and ":" in turn.session_id:
                stamp = parse_ts(turn.ts)
                if stamp is not None and stamp > now+2:
                    host = turn.session_id.split(":", 1)[0]
                    newest[host] = max(newest.get(host, stamp), stamp)
        for event in events:
            stamp = parse_ts(event.ts)
            if (event.seq > machine.last_seq and event.session_id.startswith("ssh-")
                    and ":" in event.session_id and stamp is not None and stamp > now+2):
                host = event.session_id.split(":", 1)[0]
                newest[host] = max(newest.get(host, stamp), stamp)
        for host, stamp in newest.items():
            self.remote_clock_offsets.setdefault(host, now-stamp)

        def corrected(stamp, delta):
            return datetime.fromtimestamp(min(now, stamp+delta), timezone.utc).isoformat()

        if restored:
            for turn in machine.turns.values():
                host = turn.session_id.split(":", 1)[0]
                stamp = parse_ts(turn.ts)
                if host not in newest or stamp is None or stamp <= now+2:
                    continue
                delta = self.remote_clock_offsets[host]
                turn.ts = corrected(stamp, delta)
                started = parse_ts(turn.started_ts)
                if started is not None:
                    turn.started_ts = corrected(started, delta)
                if turn.tool_hold_at:
                    turn.tool_hold_at = min(now, turn.tool_hold_at+delta)
                for action in turn.recent_actions:
                    stamp = parse_ts(action["ts"])
                    if stamp is not None:
                        action["ts"] = corrected(stamp, delta)
        result = []
        for event in events:
            host = event.session_id.split(":", 1)[0]
            stamp = parse_ts(event.ts)
            if host in newest and stamp is not None and stamp > now+2:
                event = replace(event, ts=corrected(stamp, self.remote_clock_offsets[host]))
            result.append(event)
        return result

    def poll(self, machine):
        path = events_path(self.root)
        with BusLock(lock_path(self.root), timeout=0.02):
            stat = path.stat() if path.exists() else None
            identity = (stat.st_dev, stat.st_ino) if stat else None
            reset = not self.loaded or identity != self.identity or (stat and stat.st_size < self.offset)
            if not reset and stat and self.offset and self.mark:
                with path.open("rb") as fh:
                    fh.seek(self.offset-len(self.mark))
                    reset = fh.read(len(self.mark)) != self.mark
            if reset:
                from naiwa.phase import PhaseMachine
                kept_live = False
                # While running, continue from our correctly correlated state
                # across a rotation whenever the archived journal covers the
                # gap. An already logged-in older writer may still compact with
                # older lifecycle rules; its snapshot must not replace truth we
                # can reconstruct from actual events without reconnecting SSH.
                if self.loaded and identity != self.identity and machine.last_seq:
                    archived = read_events(path.with_name("events.prev.jsonl"))
                    if archived and archived[0].seq <= machine.last_seq+1 <= archived[-1].seq+1:
                        missing = [event for event in archived if event.seq > machine.last_seq]
                        if not missing or len(missing) == missing[-1].seq-machine.last_seq:
                            machine.replay(self._remote_clock(machine, missing))
                            kept_live = True
                if not kept_live:
                    machine = self._restore()
                self.offset = 0
                self.identity = identity
                self.loaded = True
                self.mark = b""
            if stat and stat.st_size > self.offset:
                with path.open("rb") as fh:
                    fh.seek(self.offset)
                    data = fh.read()
                complete = data.rfind(b"\n") + 1
                events = self._remote_clock(machine, _decode(data[:complete]), restored=reset)
                machine.replay(events)
                self.offset += complete
                if self.offset:
                    with path.open("rb") as fh:
                        fh.seek(max(0, self.offset-64))
                        self.mark = fh.read(min(self.offset, 64))
            elif reset:
                self._remote_clock(machine, (), restored=True)
        return machine
