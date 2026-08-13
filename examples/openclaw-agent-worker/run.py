#!/usr/bin/env python3
"""Magery Link <-> OpenClaw bidirectional agent worker: ingests new room messages via SSE,
runs one `openclaw agent` turn per message, and posts the reply back into the room.

Usage:
    python run.py --config config.yaml

Maintenance:
    python run.py --config config.yaml --compact
    python run.py --config config.yaml --reset-failed 123
"""
from __future__ import annotations

import argparse
import queue
import signal
import sys
import threading

from buffer import FileLock, compact as buffer_compact, update_record
from config import load_config
from ingest import run_ingest
from worker import run_worker

_shutdown = threading.Event()


def _handle_signal(signum, frame) -> None:
    _shutdown.set()


def _reset_failed(buffer_path: str, lock: FileLock, record_id: int) -> None:
    def _mutate(record: dict) -> dict:
        record["status"] = "pending"
        record["attempts"] = 0
        record["last_error"] = None
        record["next_attempt_at"] = None
        return record

    update_record(buffer_path, lock, record_id, _mutate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config.yaml", help="path to the YAML config file")
    parser.add_argument(
        "--compact", action="store_true", help="drop 'done' records from the buffer and exit",
    )
    parser.add_argument(
        "--reset-failed", type=int, metavar="ID",
        help="reset one failed record to pending and exit",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    lock = FileLock(config.buffer_path)

    if args.compact:
        buffer_compact(config.buffer_path, lock)
        return 0
    if args.reset_failed is not None:
        _reset_failed(config.buffer_path, lock, args.reset_failed)
        return 0

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    wake: "queue.Queue[None]" = queue.Queue()
    exit_codes: dict[str, int] = {}
    ingest_threads = []
    for room in config.rooms:
        agent = config.agent_for(room)
        t = threading.Thread(
            target=run_ingest,
            args=(
                config.base_url, config.access_key, room.room_id, room.label, agent,
                config.buffer_path, lock, wake, _shutdown, exit_codes,
            ),
            daemon=True,
        )
        t.start()
        ingest_threads.append(t)

    worker_thread = threading.Thread(
        target=run_worker, args=(config, config.buffer_path, lock, wake, _shutdown), daemon=True,
    )
    worker_thread.start()

    # Join only the ingest threads first. Once every ingest thread has exited, nothing is left
    # to ever feed the buffer again, so there's no point keeping the worker thread alive — set
    # _shutdown so it notices on its next poll interval and exits on its own, then join it too.
    for t in ingest_threads:
        t.join()
    _shutdown.set()
    worker_thread.join()

    if any(code == 1 for code in exit_codes.values()):
        return 1
    if any(code == 2 for code in exit_codes.values()):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
