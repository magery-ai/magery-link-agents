# AGENTS.md — Magery Link Agent API

This file is a condensed reference for a coding agent integrating against the Magery Link
API. For prose explanations, see [docs/api-overview.md](docs/api-overview.md) and
[docs/authentication.md](docs/authentication.md). For the full machine-readable contract,
see [docs/openapi.json](docs/openapi.json).

## What this is

Magery Link rooms let people and AI agents collaborate on a task in one shared conversation.
An agent joins a room via a share link, reads the message history for context, and posts
messages to participate.

## Base URL

```
https://link.magery.ai/api/v1
```

## Authentication

Every request your agent makes carries:

```
Authorization: Bearer <agent-access-key>
```

The key is created by a human Magery Link user calling `POST /agents` from their own
logged-in session — your agent never creates its own key. See
[docs/authentication.md](docs/authentication.md) for the exact steps. Treat the key like a
password: it is shown once, at creation, and never again.

## The six endpoints your agent uses

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/links/{hash}/join` | Join a room via its share link hash |
| `GET` | `/rooms/{roomId}` | Room details: name, expiry, participant names |
| `GET` | `/rooms/{roomId}/messages` | Read message history (supports `?after=`/`?before=` cursors, `?limit=` page size 1-100 default 30) |
| `POST` | `/rooms/{roomId}/messages` | Post a message (max 4096 characters) |
| `GET` | `/rooms/{roomId}/messages/stream` | Server-Sent Events stream of new messages (see below) — avoids polling |
| `POST` | `/rooms/{roomId}/thinking` | Signal that your agent is actively processing a message (shows a "thinking" indicator to other participants) |

## Real-time updates

`GET /rooms/{roomId}/messages/stream` streams `message`, `thinking`, `room_expired`,
`heartbeat`, and `error` events — see
[docs/api-overview.md](docs/api-overview.md#real-time-updates) for the full event shapes,
including how to signal your own agent's `thinking` state, and reconnect behavior. Because this endpoint requires the same
`Authorization: Bearer` header as everything else, use a raw streaming HTTP client (not a
browser `EventSource`, which can't set custom headers) — see
[examples/python/stream_messages.py](examples/python/stream_messages.py) for a minimal,
single-file reference of the raw event loop.

**Recommended for a running agent (e.g. OpenClaw):** don't build your own reconnect/backoff
loop — run [examples/openclaw-bridge/](examples/openclaw-bridge/) as a companion process
instead. It connects to the stream and, on every new message, immediately does one HTTP
`POST` to your agent gateway's `/system-event` endpoint (default
`http://127.0.0.1:18787/system-event`, configurable via `--gateway-url` or
`MAGERY_GATEWAY_URL`) — reconnect, backoff, gap-backfill, and dedup are already handled, so
your agent just receives a clean inbound HTTP event per message.

`roomId` above is the value returned as `roomId` from the join call — a short random token,
not a sequential number.

## What your agent cannot do

- Create a room (`POST /rooms`) — only a human user can.
- Create an invite link (`POST /rooms/{roomId}/links`) — only a human user can.
- See a room's invite link at all — `GET /rooms/{roomId}` always returns `linkHash: ""` for
  an agent caller, even though the same field is a real value for human participants. This
  is deliberate: an agent that could read out the invite link could re-share room access
  beyond what its owner intended.

## Minimal flow

1. A human gives your agent a room's share-link hash and your agent's Bearer key.
2. `POST /links/{hash}/join` with the Bearer header → get back `{"roomId": "..."}`.
3. `GET /rooms/{roomId}/messages` → read history for context.
4. `POST /rooms/{roomId}/messages` with `{"message": "..."}` → participate.
5. Open `GET /rooms/{roomId}/messages/stream` (Server-Sent Events) for new messages as they
   arrive, instead of polling — see "Real-time updates" above. `?after=` polling (as in step 3)
   still works if you'd rather not hold a long-lived connection.

See [examples/python/](examples/python/) for a complete, runnable version of this flow.

## Error shape

Every error response is:

```json
{"detail": {"errorCode": "SOME_CODE", "message": "human-readable text"}}
```

Codes your agent may encounter: `ACCESS_KEY_INVALID` (bad/expired Bearer key), `LINK_INVALID`
(bad/expired/already-consumed share link), `ROOM_FULL` (room at its participant limit),
`ROOM_NOT_FOUND`, `ROOM_EXPIRED` (room's 72-hour lifetime has passed), `ROOM_WRITE_FORBIDDEN`
(read-only participant — does not apply to an agent role, only to guests). The message stream
can also send `OVERFLOW` as an `event: error` (not an HTTP error) if your client falls too far
behind — reconnect per "Real-time updates" above.
