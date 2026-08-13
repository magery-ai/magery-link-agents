"""Durable JSONL buffer for the bidirectional bridge.

Ingest only ever appends (true O_APPEND). Every status mutation goes through update_record(),
which re-reads the file fresh inside the same lock hold before writing back — so a worker's
status change can never silently clobber a concurrently appended new record.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone


class FileLock:
    """Exclusive lock on a companion `<path>.lock` file, held for one `with` block. The open
    file descriptor is kept in thread-local storage, not a plain instance attribute — a single
    FileLock instance is shared across every ingest thread and the worker thread in run.py, and
    a plain `self._fd` would let one thread's __enter__ clobber another's fd mid-lock, corrupting
    __exit__'s unlock/close and deadlocking or leaking descriptors under real concurrent use."""

    def __init__(self, buffer_path: str) -> None:
        self._lock_path = f"{buffer_path}.lock"
        self._local = threading.local()

    def __enter__(self) -> "FileLock":
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self._local.fd = fd
        return self

    def __exit__(self, *exc_info) -> None:
        fd = self._local.fd
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        del self._local.fd


def _read_all_unlocked(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    records = []
    with open(path) as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                # A single truncated/corrupted line (e.g. from a process killed mid-append) must
                # not take down every reader of this file — load_all(), update_record(), and
                # compact() all funnel through here. Skip it and keep going.
                sys.stderr.write(f"buffer: skipping malformed record at line {line_number}: {exc}\n")
    return records


def _write_all_unlocked(path: str, records: list[dict]) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".buffer-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def append(path: str, record: dict, lock: FileLock) -> None:
    with lock:
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())


def load_all(path: str, lock: FileLock) -> list[dict]:
    with lock:
        return _read_all_unlocked(path)


def update_record(
    path: str, lock: FileLock, record_id: int, mutate_fn: Callable[[dict], dict],
) -> dict:
    with lock:
        records = _read_all_unlocked(path)
        for i, r in enumerate(records):
            if r["id"] == record_id:
                records[i] = mutate_fn(dict(r))
                updated = records[i]
                _write_all_unlocked(path, records)
                return updated
        raise KeyError(record_id)


def reset_stuck_processing(path: str, lock: FileLock) -> list[dict]:
    with lock:
        records = _read_all_unlocked(path)
        reset = []
        for r in records:
            if r["status"] == "processing":
                r["status"] = "pending"
                r["next_attempt_at"] = None
                reset.append(r)
        if reset:
            _write_all_unlocked(path, records)
        return reset


def compact(path: str, lock: FileLock) -> None:
    with lock:
        records = _read_all_unlocked(path)
        kept = [r for r in records if r["status"] != "done"]
        _write_all_unlocked(path, kept)


def chunk_text(text: str, chunk_size: int) -> list[str]:
    if not text:
        return []
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


def compute_backoff_seconds(attempts: int) -> float:
    return min(30 * (2 ** (attempts - 1)), 600)


def compute_next_attempt_at(attempts: int) -> str:
    delay = compute_backoff_seconds(attempts)
    when = datetime.now(timezone.utc) + timedelta(seconds=delay)
    return when.isoformat(timespec="microseconds")


def is_ready(record: dict) -> bool:
    scheduled = record.get("next_attempt_at")
    if not scheduled:
        return True
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    return scheduled <= now


def seconds_until(iso_timestamp: str) -> float:
    when = datetime.fromisoformat(iso_timestamp)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delta)
