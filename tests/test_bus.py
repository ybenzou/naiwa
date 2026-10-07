import json
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import pytest

from naiwa.bus import (BusLock, EventReader, append_event, events_path, read_events,
                       read_snapshot, rotate_if_needed, runtime_dir)
from naiwa.phase import PhaseMachine
from naiwa.schema import BusEvent


@pytest.mark.skipif(os.name != "nt", reason="Windows reader handles block atomic replacement")
def test_atomic_health_update_waits_for_windows_reader_without_partial_json(tmp_path):
    import threading
    import time
    from naiwa.bus import atomic_json
    path = tmp_path/"health.json"
    atomic_json(path, {"state": "connecting"})
    opened = threading.Event()
    seen = []
    def reader():
        with path.open("rb") as stream:
            opened.set()
            time.sleep(.08)
            seen.append(json.load(stream))
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert opened.wait(2)
        atomic_json(path, {"state": "connected"})
    finally:
        thread.join(timeout=2)
    assert seen == [{"state": "connecting"}]
    assert json.loads(path.read_text()) == {"state": "connected"}
    assert not list(tmp_path.glob("*.tmp"))


def sample(name="turn_start", **kw):
    return BusEvent(**{"source": "cursor", "session_id": "a", "turn_id": "one", "event": name,
                       "ts": datetime.now(timezone.utc).isoformat(), **kw})


def worker_write(args):
    path, worker = args
    for index in range(8):
        append_event(path, sample(session_id=f"worker-{worker}-{index}"))
    return 8


def test_batch_preserves_order_and_rotates_with_complete_final_state(tmp_path, monkeypatch):
    from naiwa import bus
    monkeypatch.setattr(bus, "MAX_BYTES", 1)
    batch = bus.append_events(tmp_path, [sample(), sample("tool_start", tool_name="Read", tool_call_id="r"),
                                         sample("turn_done", end_reason="completed")])
    assert [event.seq for event in batch] == [1, 2, 3]
    machine = EventReader(tmp_path).poll(PhaseMachine())
    assert machine.last_seq == 3 and machine.turns["cursor:a"].phase == "done"
    following = append_event(tmp_path, sample("turn_start", turn_id="next"))
    assert following.seq == 4
    assert EventReader(tmp_path).poll(PhaseMachine()).turns["cursor:a"].turn_id == "next"


def test_parallel_process_writers_preserve_complete_lines_and_unique_sequences(tmp_path):
    with ProcessPoolExecutor(max_workers=3) as pool:
        assert sum(pool.map(worker_write, [(tmp_path, n) for n in range(3)])) == 24
    events = read_events(events_path(tmp_path))
    assert len(events) == 24
    assert [e.seq for e in events] == list(range(1, 25))
    assert len({e.session_id for e in events}) == 24


def test_partial_writer_tail_is_repaired_before_append(tmp_path):
    append_event(tmp_path, sample())
    with events_path(tmp_path).open("ab") as fh:
        fh.write(b'{"event":')
    append_event(tmp_path, sample("tool_start", tool_call_id="t"))
    assert [e.event for e in read_events(events_path(tmp_path))] == ["turn_start", "tool_start"]


def test_incremental_reader_waits_for_newline_and_does_not_reapply(tmp_path):
    reader, machine = EventReader(tmp_path), PhaseMachine()
    append_event(tmp_path, sample())
    machine = reader.poll(machine)
    offset = reader.offset
    assert reader.poll(machine) is machine
    assert reader.offset == offset
    data = json.dumps(sample("tool_start", tool_call_id="t").to_dict()).encode()
    with events_path(tmp_path).open("ab") as fh:
        fh.write(data)
    machine = reader.poll(machine)
    assert not machine.turns["cursor:a"].tools
    with events_path(tmp_path).open("ab") as fh:
        fh.write(b"\n")
    machine = reader.poll(machine)
    assert machine.turns["cursor:a"].tools == {"t"}


def test_rotation_rebuilds_from_authoritative_events_not_a_lagging_snapshot(tmp_path):
    append_event(tmp_path, sample())
    reader = EventReader(tmp_path)
    machine = reader.poll(PhaseMachine())
    lagging = machine.snapshot()
    append_event(tmp_path, sample("tool_start", tool_call_id="t"))
    assert rotate_if_needed(tmp_path, lagging, max_bytes=1)
    append_event(tmp_path, sample("tool_end", tool_call_id="t"))
    machine = reader.poll(machine)
    assert machine.turns["cursor:a"].phase == "working"
    assert machine.last_seq == 3
    restored = PhaseMachine.from_snapshot(read_snapshot(tmp_path))
    restored.replay(read_events(events_path(tmp_path)))
    assert restored.snapshot() == machine.snapshot()


def test_live_reader_keeps_canonical_completion_when_older_writer_rotates_bad_snapshot(tmp_path):
    from naiwa.bus import atomic_json, snapshot_path
    append_event(tmp_path, sample(turn_id="canonical"))
    reader = EventReader(tmp_path)
    machine = reader.poll(PhaseMachine())
    append_event(tmp_path, sample("heartbeat", turn_id="thought-generation"))
    append_event(tmp_path, sample("turn_done", turn_id="canonical", end_reason="completed"))
    assert rotate_if_needed(tmp_path, max_bytes=1)
    old = read_snapshot(tmp_path)
    old["turns"][0].update(turn_id="thought-generation", phase="working",
                           previous_turn_ids=["canonical"], end_reason="")
    atomic_json(snapshot_path(tmp_path), old)
    machine = reader.poll(machine)
    assert machine.last_seq == 3 and machine.turns["cursor:a"].turn_id == "canonical"
    assert machine.turns["cursor:a"].phase == "done"


def test_crash_after_snapshot_before_rotation_is_idempotent(tmp_path):
    from naiwa.bus import write_snapshot
    machine = PhaseMachine()
    first = append_event(tmp_path, sample())
    machine.apply(first)
    ended = append_event(tmp_path, sample("turn_done"))
    machine.apply(ended)
    write_snapshot(tmp_path, machine.snapshot())
    restored = EventReader(tmp_path).poll(PhaseMachine())
    assert restored.snapshot() == machine.snapshot()


def test_malformed_records_and_future_schema_do_not_crash_reader(tmp_path):
    rows = [[], {"event": "x"}, {**sample().to_dict(), "schema_version": "oops"},
            {**sample().to_dict(), "ts": "private prompt", "schema_version": 99}]
    events_path(tmp_path).write_bytes(b"\xff\n" + b"\n".join(json.dumps(r).encode() for r in rows)+b"\n")
    append_event(tmp_path, sample())
    assert len(read_events(events_path(tmp_path))) == 1


def test_default_bus_is_per_user_and_override_is_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("NAIWA_HOME", str(tmp_path / "personal"))
    assert runtime_dir() == tmp_path / "personal"
    assert events_path() == tmp_path / "personal" / "events.jsonl"


def test_lock_timeout_is_bounded_and_failed_handle_is_closed(tmp_path):
    with BusLock(tmp_path / "bus.lock"):
        contender = BusLock(tmp_path / "bus.lock", timeout=0.02)
        with pytest.raises(TimeoutError):
            contender.__enter__()
        assert contender._fh is None


def test_rewritten_file_larger_than_previous_offset_is_detected(tmp_path):
    append_event(tmp_path, sample())
    reader = EventReader(tmp_path)
    machine = reader.poll(PhaseMachine())
    replacement = sample(session_id="replacement").to_dict()
    replacement["seq"] = 1
    events_path(tmp_path).write_text(json.dumps(replacement)+"\n" + "\n"*200)
    machine = reader.poll(machine)
    assert set(machine.turns) == {"cursor:replacement"}


def test_authenticated_old_remote_clock_survives_rotation_without_reconnecting(tmp_path, monkeypatch):
    from datetime import timedelta
    now = datetime.now(timezone.utc).timestamp()
    monkeypatch.setattr("naiwa.bus.time.time", lambda: now)
    session = "ssh-"+"a"*16+":conversation"
    origin = "ssh-"+"a"*16
    def remote(name, delta):
        return sample(name, session_id=session, origin=origin, tool_call_id="read", tool_name="Read",
                      ts=datetime.fromtimestamp(now+254+delta, timezone.utc).isoformat())
    append_event(tmp_path, remote("turn_start", -8))
    append_event(tmp_path, remote("tool_start", -.2))
    append_event(tmp_path, remote("tool_end", 0))
    reader = EventReader(tmp_path)
    machine = reader.poll(PhaseMachine())
    assert machine.view(now+.1).pose == "tool"
    assert machine.view(now+2).pose == "working"
    assert machine.view(now+181).pose == "stale"
    assert machine.view(now).agents[0].elapsed_seconds == 8
    # Simulate a still-running pre-upgrade collector's incomplete snapshot.
    assert rotate_if_needed(tmp_path, max_bytes=1)
    snapshot = read_snapshot(tmp_path)
    for turn in snapshot["turns"]:
        turn.pop("tool_hold_ts", None)
        turn.pop("tool_hold_name", None)
    from naiwa.bus import atomic_json, snapshot_path
    atomic_json(snapshot_path(tmp_path), snapshot)
    machine = reader.poll(machine)
    assert machine.view(now+.1).pose == "tool"
    assert machine.view(now+2).pose == "working"
    # Local events keep their original timestamps and source semantics.
    append_event(tmp_path, sample(session_id="local", ts=datetime.fromtimestamp(now-5, timezone.utc).isoformat()))
    machine = reader.poll(machine)
    assert machine.turns['cursor:local'].ts == datetime.fromtimestamp(now-5, timezone.utc).isoformat()
