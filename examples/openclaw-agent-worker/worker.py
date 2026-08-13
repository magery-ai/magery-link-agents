"""FIFO worker: drains ready pending records from the shared buffer, runs one openclaw agent
turn per record, and posts the reply back into the Magery Link room."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading

import requests

from buffer import (
    FileLock, chunk_text, compute_next_attempt_at, is_ready, load_all,
    reset_stuck_processing, seconds_until, update_record,
)
from config import Config

POLL_INTERVAL_SECONDS = 5.0
# Per agent-docs/docs/api-overview.md, the thinking indicator "expires client-side 15 seconds
# after the last ping" — a caller must re-ping before that to keep it visible through a long
# turn. 10s leaves margin under the 15s expiry even accounting for network/scheduling jitter.
THINKING_PING_INTERVAL_SECONDS = 10


def _mark_processing(record: dict) -> dict:
    record["status"] = "processing"
    return record


def _mark_done(record: dict) -> dict:
    record["status"] = "done"
    return record


def _mark_retry_or_failed(record: dict, error: str, max_attempts: int) -> dict:
    record["attempts"] += 1
    record["last_error"] = error
    if record["attempts"] >= max_attempts:
        record["status"] = "failed"
        record["next_attempt_at"] = None
    else:
        record["status"] = "pending"
        record["next_attempt_at"] = compute_next_attempt_at(record["attempts"])
    return record


def build_message(config: Config, record: dict) -> str:
    return config.prompt_template.format(author=record["author"], text=record["text"])


def run_agent_turn(config: Config, record: dict) -> dict:
    result = subprocess.run(
        [
            "openclaw", "agent",
            "--agent", record["agent"],
            "--session-key", record["session_key"],
            "--message", build_message(config, record),
            "--json",
            "--timeout", str(config.timeout),
        ],
        capture_output=True, text=True, timeout=config.timeout + 10,
    )
    response = json.loads(result.stdout)
    if result.returncode != 0 or response.get("status") != "ok":
        raise RuntimeError(
            f"openclaw agent failed (exit {result.returncode}, status "
            f"{response.get('status')!r}): {response.get('summary') or result.stderr}"
        )
    return response


def reply_text(response: dict) -> str:
    payloads = (response.get("result") or {}).get("payloads") or []
    return "".join(p.get("text") or "" for p in payloads).strip()


def post_thinking(config: Config, room_id: str) -> None:
    try:
        requests.post(
            f"{config.base_url}/rooms/{room_id}/thinking",
            headers={"Authorization": f"Bearer {config.access_key}"},
            timeout=10,
        ).raise_for_status()
    except requests.RequestException as exc:
        sys.stderr.write(f"[{room_id}] thinking signal failed (non-fatal): {exc}\n")


def post_reply(config: Config, room_id: str, text: str) -> None:
    for chunk in chunk_text(text, config.chunk_size):
        requests.post(
            f"{config.base_url}/rooms/{room_id}/messages",
            json={"message": chunk},
            headers={"Authorization": f"Bearer {config.access_key}"},
            timeout=10,
        ).raise_for_status()


def _run_thinking_pinger(
    config: Config, room_id: str, stop_thinking: threading.Event,
) -> None:
    # wait() returning False means the interval elapsed without stop being set — re-ping. It
    # returning True means stop_thinking was set, so the loop (and thread) exits.
    while not stop_thinking.wait(THINKING_PING_INTERVAL_SECONDS):
        post_thinking(config, room_id)


def process_one(config: Config, buffer_path: str, lock: FileLock, record: dict) -> None:
    record = update_record(buffer_path, lock, record["id"], _mark_processing)
    post_thinking(config, record["room_id"])

    # A single post_thinking() call keeps the indicator visible for only ~15s (see
    # THINKING_PING_INTERVAL_SECONDS above), but run_agent_turn() can block for up to
    # config.timeout (default 180s). Re-post on a background thread so the indicator stays lit
    # for the whole turn; always torn down in the finally block below, success or failure.
    stop_thinking = threading.Event()
    pinger_thread = threading.Thread(
        target=_run_thinking_pinger, args=(config, record["room_id"], stop_thinking), daemon=True,
    )
    pinger_thread.start()
    try:
        response = run_agent_turn(config, record)
        text = reply_text(response)
        if text:
            post_reply(config, record["room_id"], text)
        update_record(buffer_path, lock, record["id"], _mark_done)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError,
            requests.RequestException) as exc:
        update_record(
            buffer_path, lock, record["id"],
            lambda r: _mark_retry_or_failed(r, str(exc), config.max_attempts),
        )
    finally:
        stop_thinking.set()
        pinger_thread.join(timeout=2)


def run_worker(
    config: Config, buffer_path: str, lock: FileLock, wake: "queue.Queue[None]",
    shutdown: threading.Event,
) -> None:
    try:
        reset_stuck_processing(buffer_path, lock)
    except Exception as exc:
        # Must not silently kill this thread at startup (e.g. a corrupted/truncated last line
        # in the buffer file from a process killed mid-append) — if this call raised uncaught,
        # ingest would keep appending forever while nothing ever processes the buffer again.
        sys.stderr.write(f"reset_stuck_processing failed at startup (continuing): {exc}\n")
    while not shutdown.is_set():
        try:
            records = load_all(buffer_path, lock)
            pending = [r for r in records if r["status"] == "pending"]
            ready = sorted((r for r in pending if is_ready(r)), key=lambda r: r["id"])
            if ready:
                process_one(config, buffer_path, lock, ready[0])
                continue
            not_ready = [r for r in pending if not is_ready(r)]
            if not_ready:
                soonest = min(r["next_attempt_at"] for r in not_ready)
                wait = min(seconds_until(soonest), POLL_INTERVAL_SECONDS)
            else:
                wait = POLL_INTERVAL_SECONDS
            try:
                wake.get(timeout=wait)
            except queue.Empty:
                pass
        except Exception as exc:
            sys.stderr.write(f"worker loop error (continuing): {exc}\n")
            # Rate-limit retries: a persistent (not one-off) exception would otherwise spin this
            # loop as fast as possible, pinning a CPU core and flooding stderr. wait() also lets
            # a shutdown signal interrupt this pause early.
            shutdown.wait(1)
