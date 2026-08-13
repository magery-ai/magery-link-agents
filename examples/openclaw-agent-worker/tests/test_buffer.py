import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import buffer  # noqa: E402


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

def test_chunk_text_returns_empty_list_for_empty_text():
    assert buffer.chunk_text("", 10) == []


def test_chunk_text_returns_single_chunk_when_under_limit():
    assert buffer.chunk_text("hello", 10) == ["hello"]


def test_chunk_text_splits_exactly_at_chunk_size():
    assert buffer.chunk_text("abcdefghij", 4) == ["abcd", "efgh", "ij"]


def test_chunk_text_handles_text_exactly_matching_chunk_size():
    assert buffer.chunk_text("abcd", 4) == ["abcd"]


@pytest.mark.parametrize("attempts,expected", [(1, 30), (2, 60), (3, 120), (4, 240), (5, 480)])
def test_compute_backoff_seconds_doubles_per_attempt(attempts, expected):
    assert buffer.compute_backoff_seconds(attempts) == expected


def test_compute_backoff_seconds_caps_at_ten_minutes():
    assert buffer.compute_backoff_seconds(10) == 600


def test_compute_next_attempt_at_is_in_the_future_by_the_backoff_delay():
    before = datetime.now(timezone.utc)
    result = buffer.compute_next_attempt_at(1)
    when = datetime.fromisoformat(result)
    delta = (when - before).total_seconds()
    assert 29 <= delta <= 31


def test_is_ready_true_when_next_attempt_at_is_none():
    assert buffer.is_ready({"next_attempt_at": None}) is True


def test_is_ready_false_when_next_attempt_at_is_in_the_future():
    future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(timespec="microseconds")
    assert buffer.is_ready({"next_attempt_at": future}) is False


def test_is_ready_true_when_next_attempt_at_is_in_the_past():
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="microseconds")
    assert buffer.is_ready({"next_attempt_at": past}) is True


def test_seconds_until_is_zero_for_a_past_timestamp():
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="microseconds")
    assert buffer.seconds_until(past) == 0.0


def test_seconds_until_returns_remaining_delay_for_a_future_timestamp():
    future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(timespec="microseconds")
    remaining = buffer.seconds_until(future)
    assert 28 <= remaining <= 30


# ---------------------------------------------------------------------------
# File I/O round trips
# ---------------------------------------------------------------------------

@pytest.fixture
def buffer_path(tmp_path):
    return str(tmp_path / "buffer.jsonl")


def test_append_then_load_all_round_trips_a_record(buffer_path):
    lock = buffer.FileLock(buffer_path)
    record = {"id": 1, "room_id": "abc", "status": "pending"}
    buffer.append(buffer_path, record, lock)
    assert buffer.load_all(buffer_path, lock) == [record]


def test_load_all_returns_empty_list_for_a_missing_file(buffer_path):
    lock = buffer.FileLock(buffer_path)
    assert buffer.load_all(buffer_path, lock) == []


def test_append_preserves_earlier_records(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, {"id": 1, "status": "pending"}, lock)
    buffer.append(buffer_path, {"id": 2, "status": "pending"}, lock)
    records = buffer.load_all(buffer_path, lock)
    assert [r["id"] for r in records] == [1, 2]


def test_update_record_mutates_the_matching_record_and_persists_it(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, {"id": 1, "status": "pending"}, lock)
    buffer.append(buffer_path, {"id": 2, "status": "pending"}, lock)

    def mark_done(record):
        record["status"] = "done"
        return record

    updated = buffer.update_record(buffer_path, lock, 1, mark_done)
    assert updated["status"] == "done"

    records = {r["id"]: r for r in buffer.load_all(buffer_path, lock)}
    assert records[1]["status"] == "done"
    assert records[2]["status"] == "pending"


def test_update_record_raises_key_error_for_unknown_id(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, {"id": 1, "status": "pending"}, lock)
    with pytest.raises(KeyError):
        buffer.update_record(buffer_path, lock, 999, lambda r: r)


def test_update_record_does_not_lose_a_concurrent_append(buffer_path):
    """Guards the fix for the ingest/worker race: a naive load_all-then-separately-locked-rewrite
    implementation would silently drop this concurrent append, because its rewrite would use a
    stale in-memory snapshot taken before the append happened. This test fails against that naive
    version and passes only because update_record re-reads the file fresh inside one continuous
    lock hold — which also means the concurrent append() below cannot even start until the update
    finishes, proving append() and update_record() share the same lock.

    The append call runs on its own thread (not the thread driving the test) because
    update_record holds the lock for its whole duration: if the main thread both waited to call
    append() AND was the one responsible for releasing slow_mutate's gate afterwards, the two
    would deadlock each other — append() would block forever waiting for a lock that can only be
    released once slow_mutate returns, which can't happen until the very append() call that's
    blocked releases the gate."""
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, {"id": 1, "status": "pending"}, lock)

    started = threading.Event()

    def slow_mutate(record):
        started.set()
        time.sleep(0.2)
        record["status"] = "processing"
        return record

    def do_update():
        buffer.update_record(buffer_path, lock, 1, slow_mutate)

    def do_append():
        started.wait(timeout=5)
        buffer.append(buffer_path, {"id": 2, "status": "pending"}, lock)

    updater = threading.Thread(target=do_update)
    appender = threading.Thread(target=do_append)
    updater.start()
    appender.start()
    updater.join(timeout=5)
    appender.join(timeout=5)
    assert not updater.is_alive()
    assert not appender.is_alive()

    records = {r["id"]: r for r in buffer.load_all(buffer_path, lock)}
    assert set(records) == {1, 2}
    assert records[1]["status"] == "processing"
    assert records[2]["status"] == "pending"


def test_load_all_skips_a_corrupted_line_without_raising(buffer_path):
    # A truncated/malformed line (e.g. left behind by a process killed mid-append) must not
    # break every reader of the buffer file.
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, {"id": 1, "status": "pending"}, lock)
    with open(buffer_path, "a") as f:
        f.write('{"id": 2, "status": "pending", truncated\n')
    buffer.append(buffer_path, {"id": 3, "status": "pending"}, lock)

    records = buffer.load_all(buffer_path, lock)
    assert [r["id"] for r in records] == [1, 3]


def test_reset_stuck_processing_resets_only_processing_records(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, {"id": 1, "status": "processing", "next_attempt_at": None}, lock)
    buffer.append(buffer_path, {"id": 2, "status": "pending", "next_attempt_at": "some-value"}, lock)

    reset = buffer.reset_stuck_processing(buffer_path, lock)
    assert [r["id"] for r in reset] == [1]

    records = {r["id"]: r for r in buffer.load_all(buffer_path, lock)}
    assert records[1]["status"] == "pending"
    assert records[1]["next_attempt_at"] is None
    assert records[2]["status"] == "pending"
    assert records[2]["next_attempt_at"] == "some-value"


def test_compact_drops_done_records_and_keeps_everything_else(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, {"id": 1, "status": "pending"}, lock)
    buffer.append(buffer_path, {"id": 2, "status": "processing"}, lock)
    buffer.append(buffer_path, {"id": 3, "status": "done"}, lock)
    buffer.append(buffer_path, {"id": 4, "status": "failed"}, lock)

    buffer.compact(buffer_path, lock)

    ids = {r["id"] for r in buffer.load_all(buffer_path, lock)}
    assert ids == {1, 2, 4}
