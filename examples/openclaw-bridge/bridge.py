#!/usr/bin/env python3
"""Magery Link agent bridge: forwards new room messages to stdout, a webhook, or OpenClaw's
gateway in real time via Server-Sent Events — no polling, no LLM calls, no message sending.

Usage (single room):
    python bridge.py --room-id=<room-id> --access-key=<bearer-key> --mode=openclaw \
        --gateway-url=http://localhost:18787

Usage (multi-room):
    python bridge.py --config=rooms.yaml --access-key=<bearer-key> --mode=stdout

Requires: pip install -r requirements.txt
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Any, Iterator

import requests
import yaml

DEFAULT_BASE_URL = "https://link.magery.ai/api/v1"
DEFAULT_GATEWAY_URL = "http://localhost:18787"
MIN_BACKOFF_SECONDS = 1
MAX_BACKOFF_SECONDS = 30
# A connection that stayed up at least this long is treated as "genuinely established" for
# backoff-reset purposes. Without this floor, a connection that gets a 200 and then immediately
# drops (e.g. an OVERFLOW error, the failure most likely to recur immediately) would reset
# backoff to 1s and retry instantly — a potential reconnect storm against the API.
MIN_HEALTHY_CONNECTION_SECONDS = 5

_shutdown = threading.Event()


def _handle_signal(signum, frame) -> None:
    _shutdown.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


class AuthError(Exception):
    """Raised when the initial connection to a room's stream fails with HTTP 401 or 404 —
    not retryable, since the credentials or room id themselves are wrong."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


# ---------------------------------------------------------------------------
# Pure logic: SSE parsing, backoff, dedup, output payload shaping
# ---------------------------------------------------------------------------

def iter_sse_events(lines) -> Iterator[tuple[str, dict]]:
    """Parses an iterable of raw SSE lines (blank line or None marks the end of one event
    block, matching requests' iter_lines(decode_unicode=True) behavior) into (event_type,
    payload) pairs, one at a time as they're produced. A "data:" line with no preceding
    "event:" line is ignored. This is the actual parser connect_and_stream() consumes live —
    written as a generator so it can process events incrementally from a live streaming HTTP
    response rather than only after the full line sequence has been collected."""
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


def parse_sse_lines(lines) -> list[tuple[str, dict]]:
    """Thin convenience wrapper for callers (and tests) that want the full list rather than
    incremental iteration — built on the exact same logic connect_and_stream() uses."""
    return list(iter_sse_events(lines))


def next_backoff(current: float) -> float:
    """Doubles the current backoff, capped at MAX_BACKOFF_SECONDS."""
    return min(current * 2, MAX_BACKOFF_SECONDS)


class Deduper:
    """Tracks the highest message id seen for one room, in memory only. Message ids are
    monotonically increasing (an autoincrement primary key server-side), so "seen" means
    "id <= the highest id already processed".

    is_new() and mark_seen() are deliberately separate operations (rather than one
    check-and-advance call) so a caller can check, then attempt delivery, then only mark the
    message seen once delivery actually succeeded — if the output sink raises, the watermark
    must stay behind that message so the next reconnect's backfill re-attempts it instead of
    silently skipping it forever.

    has_backfilled tracks "has a backfill call ever completed successfully" as its own explicit
    flag, separate from last_id. It's tempting to infer "is this the first backfill attempt"
    from `last_id is None`, but that's wrong: last_id also stays None for a room that's
    genuinely empty (or has only the agent's own messages) at the time of a backfill call — and
    that can happen on a SECOND or later call too (a quiet room, reconnecting before any live
    message ever arrives to seed last_id). Conflating the two would make backfill() treat every
    such reconnect as a fresh bootstrap and silently suppress a real message forwarded during
    that gap — exactly the kind of silent loss this class was built to prevent.

    has_backfilled defaults to True when a last_id is supplied at construction time (i.e. an
    explicit --last-id "resume from here" request): that's the caller declaring this dedup
    already has a starting point, so its very first backfill() call should emit normally, not
    be treated as an unseeded bootstrap. A plain Deduper() (no seed) starts at False, matching
    the true "nothing established yet" case."""

    def __init__(self, last_id: int | None = None) -> None:
        self.last_id = last_id
        self.has_backfilled = last_id is not None

    def is_new(self, message_id: int) -> bool:
        return self.last_id is None or message_id > self.last_id

    def mark_seen(self, message_id: int) -> None:
        if self.last_id is None or message_id > self.last_id:
            self.last_id = message_id


def format_stdout(message: dict, room_id: str, room_label: str | None) -> str:
    return json.dumps({
        "id": message["id"],
        "author": message["authorName"],
        "message": message["message"],
        "room_id": room_id,
        "room_label": room_label,
        "created_at": message["createdAt"],
    })


def format_openclaw_payload(message: dict) -> dict:
    return {
        "text": f"[Magery] {message['authorName']}: {message['message']}",
        "source": "magery-link",
    }


def parse_webhook_headers(raw_headers: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in raw_headers:
        name, _, value = raw.partition(": ")
        headers[name] = value
    return headers


# ---------------------------------------------------------------------------
# Output sinks — each receives the raw message dict plus room context and, via **sink_kwargs,
# whatever the other modes need (ignored via **_ by modes that don't need them).
# ---------------------------------------------------------------------------

def emit_stdout(message: dict, room_id: str, room_label: str | None, **_: Any) -> None:
    print(format_stdout(message, room_id, room_label), flush=True)


def emit_webhook(message: dict, room_id: str, room_label: str | None,
                  webhook_url: str, webhook_headers: dict[str, str], **_: Any) -> None:
    body = json.loads(format_stdout(message, room_id, room_label))
    requests.post(webhook_url, json=body, headers=webhook_headers, timeout=10).raise_for_status()


def emit_openclaw(message: dict, room_id: str, room_label: str | None,
                   gateway_url: str, **_: Any) -> None:
    requests.post(
        f"{gateway_url}/system-event", json=format_openclaw_payload(message), timeout=10,
    ).raise_for_status()


OUTPUT_SINKS = {
    "stdout": emit_stdout,
    "webhook": emit_webhook,
    "openclaw": emit_openclaw,
}


# ---------------------------------------------------------------------------
# Connection loop
# ---------------------------------------------------------------------------

def backfill(base_url: str, access_key: str, room_id: str, dedup: Deduper) -> list[dict]:
    """Fetches messages published since dedup.last_id (or the last 30 messages if nothing's
    been seen yet), returning only the unseen, non-own ones, in order. Does NOT mark them seen
    itself (except for the bootstrap case below) — the caller marks each message seen only
    after it's been successfully delivered, so a sink failure can't silently lose it.

    On the very first call for a room (dedup.has_backfilled is still False — no backfill call
    has ever completed successfully, including nothing seeded via --last-id), the server has no
    "since" point to backfill from and returns recent history instead. Forwarding that as if it
    just arrived would replay up to 30 stale messages on every fresh start/restart, which isn't
    what this script is for — so on that first call we seed the dedup watermark from what came
    back WITHOUT emitting any of it, and return an empty list. Every call after that (a genuine
    reconnect, or an explicit --last-id "resume from here" request) backfills and emits
    normally. This is deliberately NOT inferred from `dedup.last_id is None`: a room that's
    genuinely empty (or has only the agent's own messages) also leaves last_id at None, and that
    can be true on a second or later call too — so has_backfilled is tracked as its own flag,
    set only after a request actually succeeds (past the 401/404 check and raise_for_status()),
    so a failed first attempt doesn't consume the one-time suppression before a real first
    success happens."""
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


def connect_and_stream(
    base_url: str, access_key: str, room_id: str, room_label: str | None,
    mode: str, sink_kwargs: dict, dedup: Deduper,
) -> tuple[str, float]:
    """Backfills, then opens one stream connection and processes events until the connection
    drops, the room expires, or shutdown is requested. Returns (outcome, duration_seconds)
    where outcome is "expired", "dropped", or "shutdown", and duration_seconds is how long the
    stream connection stayed open — run_room() uses it to decide whether to reset backoff, since
    a connection that barely stayed up (e.g. an immediate OVERFLOW) shouldn't be treated the
    same as one that ran healthily for a while. Raises AuthError if the initial connection fails
    with HTTP 401/404 (from either the backfill request or the stream request)."""
    for message in backfill(base_url, access_key, room_id, dedup):
        OUTPUT_SINKS[mode](message, room_id, room_label, **sink_kwargs)
        dedup.mark_seen(message["id"])

    connected_at = time.monotonic()
    with requests.get(
        f"{base_url}/rooms/{room_id}/messages/stream",
        headers={"Authorization": f"Bearer {access_key}"},
        stream=True,
        # A generous but finite read timeout: the server heartbeats every 30s, so a gap beyond
        # ~3 heartbeats means the connection is genuinely dead (the most common real-world SSE
        # failure mode is a half-open TCP connection — NAT/firewall state expiry, a silent
        # load-balancer drop — that an infinite read timeout would never notice).
        timeout=(10, 90),
    ) as resp:
        if resp.status_code in (401, 404):
            raise AuthError(resp.status_code)
        resp.raise_for_status()

        try:
            for event_type, payload in iter_sse_events(resp.iter_lines(decode_unicode=True)):
                if _shutdown.is_set():
                    return "shutdown", time.monotonic() - connected_at
                if event_type == "message":
                    if not payload["isOwn"] and dedup.is_new(payload["id"]):
                        OUTPUT_SINKS[mode](payload, room_id, room_label, **sink_kwargs)
                        dedup.mark_seen(payload["id"])
                elif event_type == "room_expired":
                    return "expired", time.monotonic() - connected_at
                elif event_type == "error":
                    sys.stderr.write(f"[{room_label or room_id}] stream error: {payload}\n")
                    return "dropped", time.monotonic() - connected_at
                # "heartbeat" needs no action.
        except requests.RequestException as exc:
            # We were connected — we're past the initial connect/auth check above — and the
            # connection dropped mid-stream (e.g. ChunkedEncodingError/ConnectionError/
            # ReadTimeout raised while iterating). That's the same "dropped" outcome as a clean
            # iterator exhaustion below.
            sys.stderr.write(f"[{room_label or room_id}] stream interrupted: {exc}\n")
            return "dropped", time.monotonic() - connected_at
    return "dropped", time.monotonic() - connected_at


def run_room(
    base_url: str, access_key: str, room_id: str, room_label: str | None,
    mode: str, sink_kwargs: dict, last_id: int | None, exit_codes: dict[str, int],
) -> None:
    """Runs the reconnect loop for one room until it expires, hits an unrecoverable auth
    error, or the process shuts down. Records this room's outcome into exit_codes[room_id].

    The outer try/finally is a last-resort safety net: every normal path already sets
    exit_codes[room_id] itself, so the finally's setdefault(..., 2) is a no-op in all of those
    cases — it only fires if something escapes even the broad `except Exception` below (e.g. a
    BaseException), ensuring this room is never silently missing from exit_codes."""
    dedup = Deduper(last_id)
    backoff: float = MIN_BACKOFF_SECONDS
    try:
        while not _shutdown.is_set():
            try:
                outcome, duration = connect_and_stream(
                    base_url, access_key, room_id, room_label, mode, sink_kwargs, dedup,
                )
            except AuthError as exc:
                sys.stderr.write(f"[{room_label or room_id}] auth/room error: HTTP {exc.status_code}\n")
                exit_codes[room_id] = 1
                return
            except requests.RequestException as exc:
                sys.stderr.write(f"[{room_label or room_id}] connection error: {exc}\n")
                exit_codes.setdefault(room_id, 2)
                if _shutdown.wait(backoff):
                    # Shutdown arrived while we were mid-backoff, never having reconnected —
                    # leave the code at 2 (a retry was in flight and got interrupted).
                    return
                backoff = next_backoff(backoff)
                continue
            except Exception as exc:
                # Anything unexpected (malformed payload, KeyError on an unexpected message
                # shape, a broken output sink, etc.) must not silently kill this thread and
                # leave exit_codes[room_id] undefined — that would make main() report a clean
                # exit 0 despite a real crash. Treat it like a transient connection failure:
                # log it, mark 2, retry with backoff.
                sys.stderr.write(f"[{room_label or room_id}] unexpected error: {exc}\n")
                exit_codes.setdefault(room_id, 2)
                if _shutdown.wait(backoff):
                    return
                backoff = next_backoff(backoff)
                continue

            if outcome == "expired":
                exit_codes[room_id] = 0
                return
            if outcome == "shutdown":
                exit_codes[room_id] = 0
                return
            # outcome == "dropped": we connected before losing the connection. Only reset
            # backoff if the connection stayed up long enough to be considered genuinely
            # healthy — an immediate drop (e.g. OVERFLOW right after connecting) is not
            # evidence the server is fine, and resetting to 1s for it risks a reconnect storm.
            exit_codes.setdefault(room_id, 2)
            if duration >= MIN_HEALTHY_CONNECTION_SECONDS:
                backoff = MIN_BACKOFF_SECONDS
            else:
                backoff = next_backoff(backoff)
            if _shutdown.wait(backoff):
                # Same reasoning as above: shutdown during the retry wait, not a clean outcome.
                return
        # Loop exited because _shutdown was already set at the top-of-loop check, before the
        # body ever ran this iteration (e.g. this room never got to connect at all). Treat that
        # as a clean 0 — setdefault so it never overwrites a code a terminal `return` above
        # already set.
        exit_codes.setdefault(room_id, 0)
    finally:
        exit_codes.setdefault(room_id, 2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--access-key", default=os.environ.get("MAGERY_ACCESS_KEY"),
                         help="Bearer key from POST /agents (env: MAGERY_ACCESS_KEY)")
    parser.add_argument("--room-id", default=os.environ.get("MAGERY_ROOM_ID"),
                         help="single room to stream (env: MAGERY_ROOM_ID); mutually exclusive with --config")
    parser.add_argument("--config", help="path to a rooms.yaml for multi-room mode; mutually exclusive with --room-id")
    parser.add_argument("--base-url", default=os.environ.get("MAGERY_BASE_URL", DEFAULT_BASE_URL),
                         help=f"e.g. {DEFAULT_BASE_URL} (env: MAGERY_BASE_URL)")
    parser.add_argument("--mode", default="stdout", choices=["stdout", "webhook", "openclaw"])
    parser.add_argument("--webhook-url", help="required when --mode=webhook")
    parser.add_argument("--webhook-header", action="append", default=[],
                         help="repeatable, format 'Header-Name: value'")
    parser.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL, help="OpenClaw gateway base URL")
    parser.add_argument("--last-id", type=int, default=None,
                         help="resume backfill from this message id (single-room mode only)")
    args = parser.parse_args(argv)

    if not args.access_key:
        parser.error("--access-key or MAGERY_ACCESS_KEY is required")
    if bool(args.room_id) == bool(args.config):
        parser.error("exactly one of --room-id or --config is required")
    if args.mode == "webhook" and not args.webhook_url:
        parser.error("--webhook-url is required when --mode=webhook")
    return args


def load_rooms_config(path: str) -> list[dict]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["rooms"]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sink_kwargs = {
        "webhook_url": args.webhook_url,
        "webhook_headers": parse_webhook_headers(args.webhook_header),
        "gateway_url": args.gateway_url,
    }

    if args.room_id:
        rooms = [{"id": args.room_id, "label": None}]
    else:
        rooms = load_rooms_config(args.config)

    exit_codes: dict[str, int] = {}
    threads = []
    for room in rooms:
        last_id = args.last_id if len(rooms) == 1 else None
        t = threading.Thread(
            target=run_room,
            args=(args.base_url, args.access_key, room["id"], room.get("label"), args.mode, sink_kwargs, last_id, exit_codes),
            daemon=True,
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    # Aggregate purely from the per-room exit codes (each room's run_room() call always leaves
    # one behind — see run_room()'s own setdefault(..., 0)/finally for the "never got to
    # connect"/"unexpected crash" cases). Auth failures take priority over a room that was
    # mid-retry at shutdown, which takes priority over a fully clean outcome.
    if any(code == 1 for code in exit_codes.values()):
        return 1
    if any(code == 2 for code in exit_codes.values()):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
