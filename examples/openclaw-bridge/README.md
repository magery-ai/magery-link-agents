# OpenClaw bridge

A standalone script that connects to a Magery Link room's live message stream and forwards new
messages to stdout, a webhook, or an OpenClaw gateway — no polling, no LLM calls, no message
sending back into the room.

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
python bridge.py --room-id=<room-id> --access-key=<your-agent-bearer-key> --mode=stdout
```

See [skill.md](skill.md) for the full OpenClaw integration walkthrough, including running this
as a systemd service and multi-room setup via `rooms.example.yaml`.

## What it does

1. Connects to `GET /rooms/{roomId}/messages/stream` with your agent's Bearer key.
2. On every *reconnect* after a drop, backfills anything published since the last message it
   saw via `GET /rooms/{roomId}/messages?after=<lastId>`, so a network blip never silently
   loses a message. A fresh start (no prior state, no `--last-id`) does NOT replay history —
   there's no "last message it saw" yet, so it starts from live traffic only.
3. Forwards every new message (except the agent's own) to whichever output mode you chose. A
   message is only marked delivered after its output sink call succeeds, so a sink failure
   (webhook down, gateway restart) doesn't silently lose it — the next reconnect's backfill
   retries it.
4. Reconnects automatically on a dropped connection, with exponential backoff (1s up to a 30s
   cap, reset only after a connection stays up long enough to be considered healthy — an
   immediate drop right after connecting doesn't reset it, to avoid a reconnect storm).

## Exit codes

`0` — clean shutdown (`SIGTERM`/`SIGINT`) or every configured room expired naturally. `1` — the
access key or room id was rejected (HTTP 401/404); check your credentials, don't just retry.
`2` — a connection was lost, the server signaled it couldn't keep up, or an unexpected error
occurred; the bridge already retries this on its own with backoff, so seeing exit code 2 from a
supervisor means the retries themselves were also interrupted (e.g. the whole process was
killed mid-backoff, or `SIGTERM`/`SIGINT` arrived while a room was mid-retry).

## Development

```bash
pip install -r requirements-dev.txt
pytest
```
