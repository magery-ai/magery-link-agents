"""SSE ingest: one connection per room, appending every new (non-own) message to the shared
buffer file. Same reconnect/backoff/dedup/backfill guarantees as
agent-docs/examples/openclaw-bridge/bridge.py, adapted to append to a shared buffer instead of
calling an output sink.
"""
from __future__ import annotations

import json
import queue
import re
import sys
import threading
import time
from typing import Iterator

import requests

from buffer import FileLock, append as buffer_append

MIN_BACKOFF_SECONDS = 1
MAX_BACKOFF_SECONDS = 30
MIN_HEALTHY_CONNECTION_SECONDS = 5


class AuthError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


def iter_sse_events(lines) -> Iterator[tuple[str, dict]]:
    event_type: str | None = None
    for line in lines:
        if line is None or line == "":
            event_type = None
            continue
        if line.startswith("event: "):
            event_type = line.removeprefix("event: ")
        elif line.startswith("data: ") and event_type is not None:
            payload = json.loads(line.removeprefix("data: "))
            yield event_type, payload


def next_backoff(current: float) -> float:
    return min(current * 2, MAX_BACKOFF_SECONDS)


class Deduper:
    def __init__(self, last_id: int | None = None) -> None:
        self.last_id = last_id
        self.has_backfilled = last_id is not None

    def is_new(self, message_id: int) -> bool:
        return self.last_id is None or message_id > self.last_id

    def mark_seen(self, message_id: int) -> None:
        if self.last_id is None or message_id > self.last_id:
            self.last_id = message_id


def backfill(base_url: str, access_key: str, room_id: str, dedup: Deduper) -> list[dict]:
    is_bootstrap = not dedup.has_backfilled
    params: dict[str, int] = {}
    if dedup.last_id is not None:
        params["after"] = dedup.last_id
    resp = requests.get(
        f"{base_url}/rooms/{room_id}/messages",
        headers={"Authorization": f"Bearer {access_key}"},
        params=params,
        timeout=10,
    )
    if resp.status_code in (401, 404):
        raise AuthError(resp.status_code)
    resp.raise_for_status()
    dedup.has_backfilled = True
    messages = resp.json()["messages"]
    new_messages = [m for m in messages if not m["isOwn"] and dedup.is_new(m["id"])]
    if is_bootstrap:
        for m in new_messages:
            dedup.mark_seen(m["id"])
        return []
    return new_messages


def make_record(message: dict, room_id: str, agent: str) -> dict:
    return {
        "id": message["id"],
        "room_id": room_id,
        "author": message["authorName"],
        "text": message["message"],
        "created_at": message["createdAt"],
        "agent": agent,
        "session_key": f"agent:{agent}:magery-{room_id}",
        "status": "pending",
        "attempts": 0,
        "last_error": None,
        "next_attempt_at": None,
    }


def should_process(text: str, mention_names: list[str]) -> bool:
    """True if `text` should be processed: strict mention-filter mode is disabled
    (mention_names is empty), or text contains an @-mention of one of the configured names as a
    whole token — preceded by start-of-text or whitespace, not immediately followed by another
    word character, case-insensitive. Mirrors landing/src/lib/mentions.tsx's word-boundary
    matching convention for the same product's human-facing @mentions. Names are sorted
    longest-first so a name that's a prefix of another can't shadow the longer match.
    """
    if not mention_names:
        return True
    sorted_names = sorted(mention_names, key=len, reverse=True)
    escaped = [re.escape(name) for name in sorted_names]
    # ASCII-only boundary by design: Python's \w is Unicode-aware, JavaScript's is not.
    # This must stay in sync with landing/src/lib/mentions.tsx's boundary semantics.
    pattern = re.compile(rf"(?:^|(?<=\s))@(?:{'|'.join(escaped)})(?![A-Za-z0-9_])", re.IGNORECASE)
    return pattern.search(text) is not None


def emit(
    message: dict, room_id: str, agent: str, buffer_path: str, lock: FileLock,
    wake: "queue.Queue[None]",
) -> None:
    record = make_record(message, room_id, agent)
    buffer_append(buffer_path, record, lock)
    wake.put_nowait(None)


def connect_and_stream(
    base_url: str, access_key: str, room_id: str, room_label: str | None, agent: str,
    mention_names: list[str], buffer_path: str, lock: FileLock, wake: "queue.Queue[None]",
    dedup: Deduper, shutdown: threading.Event,
) -> tuple[str, float]:
    for message in backfill(base_url, access_key, room_id, dedup):
        if should_process(message["message"], mention_names):
            emit(message, room_id, agent, buffer_path, lock, wake)
        dedup.mark_seen(message["id"])

    connected_at = time.monotonic()
    with requests.get(
        f"{base_url}/rooms/{room_id}/messages/stream",
        headers={"Authorization": f"Bearer {access_key}"},
        stream=True,
        timeout=(10, 90),
    ) as resp:
        if resp.status_code in (401, 404):
            raise AuthError(resp.status_code)
        resp.raise_for_status()

        try:
            for event_type, payload in iter_sse_events(resp.iter_lines(decode_unicode=True)):
                if shutdown.is_set():
                    return "shutdown", time.monotonic() - connected_at
                if event_type == "message":
                    if not payload["isOwn"] and dedup.is_new(payload["id"]):
                        if should_process(payload["message"], mention_names):
                            emit(payload, room_id, agent, buffer_path, lock, wake)
                        dedup.mark_seen(payload["id"])
                elif event_type == "room_expired":
                    return "expired", time.monotonic() - connected_at
                elif event_type == "error":
                    sys.stderr.write(f"[{room_label or room_id}] stream error: {payload}\n")
                    return "dropped", time.monotonic() - connected_at
        except requests.RequestException as exc:
            sys.stderr.write(f"[{room_label or room_id}] stream interrupted: {exc}\n")
            return "dropped", time.monotonic() - connected_at
    return "dropped", time.monotonic() - connected_at


def run_ingest(
    base_url: str, access_key: str, room_id: str, room_label: str | None, agent: str,
    mention_names: list[str], buffer_path: str, lock: FileLock, wake: "queue.Queue[None]",
    shutdown: threading.Event, exit_codes: dict[str, int],
) -> None:
    """Runs the reconnect loop for one room until it expires, hits an unrecoverable auth error,
    or the process shuts down. Records this room's outcome into exit_codes[room_id], mirroring
    agent-docs/examples/openclaw-bridge/bridge.py's run_room(): 1 for an auth error (not
    retryable — run.py's main() must exit non-zero so systemd's Restart=on-failure means
    something), 2 if shutdown arrived mid-backoff (a retry was in flight and got interrupted),
    0 for a clean "expired"/"shutdown" outcome. The outer try/finally is a last-resort safety
    net: every normal path already sets exit_codes[room_id] itself, so the finally's
    setdefault(..., 0) is a no-op in all of those cases — it only fires if something escapes
    even the broad `except Exception` below, ensuring this room is never silently missing from
    exit_codes."""
    dedup = Deduper()
    backoff: float = MIN_BACKOFF_SECONDS
    try:
        while not shutdown.is_set():
            try:
                outcome, duration = connect_and_stream(
                    base_url, access_key, room_id, room_label, agent, mention_names, buffer_path,
                    lock, wake, dedup, shutdown,
                )
            except AuthError as exc:
                sys.stderr.write(
                    f"[{room_label or room_id}] auth/room error: HTTP {exc.status_code}\n"
                )
                exit_codes[room_id] = 1
                return
            except requests.RequestException as exc:
                sys.stderr.write(f"[{room_label or room_id}] connection error: {exc}\n")
                if shutdown.wait(backoff):
                    exit_codes[room_id] = 2
                    return
                backoff = next_backoff(backoff)
                continue
            except Exception as exc:
                sys.stderr.write(f"[{room_label or room_id}] unexpected error: {exc}\n")
                if shutdown.wait(backoff):
                    exit_codes[room_id] = 2
                    return
                backoff = next_backoff(backoff)
                continue

            if outcome in ("expired", "shutdown"):
                exit_codes[room_id] = 0
                return
            if duration >= MIN_HEALTHY_CONNECTION_SECONDS:
                backoff = MIN_BACKOFF_SECONDS
            else:
                backoff = next_backoff(backoff)
            if shutdown.wait(backoff):
                exit_codes[room_id] = 2
                return
    finally:
        exit_codes.setdefault(room_id, 0)
