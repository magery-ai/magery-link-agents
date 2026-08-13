import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import buffer  # noqa: E402
import run as run_module  # noqa: E402


def _write_config(tmp_path, **overrides):
    data = {
        "access_key": "plain-key",
        "buffer_path": str(tmp_path / "buffer.jsonl"),
        "rooms": [{"room_id": "room-1", "label": "test"}],
    }
    data.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(data))
    return str(path)


def test_compact_flag_compacts_the_buffer_and_does_not_start_threads(tmp_path):
    config_path = _write_config(tmp_path)
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, {"id": 1, "status": "done"}, lock)
    buffer.append(buffer_path, {"id": 2, "status": "pending"}, lock)

    with patch("run.threading.Thread") as mock_thread:
        code = run_module.main(["--config", config_path, "--compact"])

    assert code == 0
    mock_thread.assert_not_called()
    ids = {r["id"] for r in buffer.load_all(buffer_path, lock)}
    assert ids == {2}


def test_reset_failed_flag_resets_one_record_and_does_not_start_threads(tmp_path):
    config_path = _write_config(tmp_path)
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    buffer.append(
        buffer_path,
        {"id": 5, "status": "failed", "attempts": 3, "last_error": "boom", "next_attempt_at": None},
        lock,
    )

    with patch("run.threading.Thread") as mock_thread:
        code = run_module.main(["--config", config_path, "--reset-failed", "5"])

    assert code == 0
    mock_thread.assert_not_called()
    record = buffer.load_all(buffer_path, lock)[0]
    assert record["status"] == "pending"
    assert record["attempts"] == 0
    assert record["last_error"] is None


def test_default_invocation_spawns_one_ingest_thread_per_room_plus_one_worker_thread(tmp_path):
    config_path = _write_config(tmp_path, rooms=[
        {"room_id": "room-1", "label": "a", "agent": "agent-a"},
        {"room_id": "room-2", "label": "b", "agent": "agent-b"},
    ])

    created_threads = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args
            created_threads.append(self)

        def start(self):
            pass

        def join(self):
            pass

    with patch("run.threading.Thread", FakeThread), patch("run.signal.signal"):
        code = run_module.main(["--config", config_path])

    assert code == 0
    assert len(created_threads) == 3
    targets = [t.target for t in created_threads]
    assert targets.count(run_module.run_ingest) == 2
    assert targets.count(run_module.run_worker) == 1

    config = run_module.load_config(config_path)
    ingest_threads = [t for t in created_threads if t.target is run_module.run_ingest]
    worker_threads = [t for t in created_threads if t.target is run_module.run_worker]

    # The exact positional-arg order matters: a real wiring bug (e.g. swapped params between
    # run_ingest's positional args) would still pass a count-only assertion.
    by_room_id = {t.args[2]: t for t in ingest_threads}
    assert set(by_room_id) == {"room-1", "room-2"}

    lock = ingest_threads[0].args[6]
    wake = ingest_threads[0].args[7]
    shutdown = ingest_threads[0].args[8]
    exit_codes = ingest_threads[0].args[9]

    expected_by_room = {"room-1": ("a", "agent-a"), "room-2": ("b", "agent-b")}
    for room_id, (label, agent) in expected_by_room.items():
        t = by_room_id[room_id]
        assert t.args == (
            config.base_url, config.access_key, room_id, label, agent,
            config.buffer_path, lock, wake, shutdown, exit_codes,
        )

    worker_thread = worker_threads[0]
    assert worker_thread.args == (config, config.buffer_path, lock, wake, shutdown)

    # The shared lock and wake objects passed to every thread must be the literal same object
    # (identity, not just equality) — that's the invariant the shared buffer/queue design
    # depends on.
    for t in ingest_threads:
        assert t.args[6] is lock
        assert t.args[7] is wake
    assert worker_thread.args[2] is lock
    assert worker_thread.args[3] is wake
