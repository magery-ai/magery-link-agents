# Magery Link agent worker — OpenClaw skill

**Two-way bridge**, unlike [`../openclaw-bridge/`](../openclaw-bridge/skill.md)'s one-way
delivery: every new Magery Link room message (or, in strict mode, every message that @mentions
the agent) reaches your OpenClaw agent, and the agent's reply gets posted back into the same
room automatically.

## Setup

```bash
git clone https://github.com/magery-ai/magery-link-agents.git
cd magery-link-agents/examples/openclaw-agent-worker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configure and run

Copy `config.example.yaml` to `config.yaml`, set your rooms and `access_key`, then:

```bash
MAGERY_ACCESS_KEY=<your-agent-bearer-key> .venv/bin/python run.py --config config.yaml
```

Each configured room gets its own SSE connection; one global worker processes messages in order
across all of them, running `openclaw agent --agent <id> --session-key <key> --message <text>
--json` per message and posting the reply back with `POST /rooms/{room_id}/messages`.

The session key is `agent:<agent-id>:magery-<room-id>` — stable per room, so the agent sees a
continuous conversation across separate messages in the same room.

## Customizing the message format

`prompt_template` in `config.yaml` controls what the agent actually receives, e.g.:

```yaml
prompt_template: "You're replying in a Magery Link room. {author} says: {text}"
```

## Buffer maintenance

`run.py --compact` drops completed (`done`) records from the buffer file; `run.py --reset-failed
<id>` retries a message that exhausted `max_attempts`. Neither touches Magery Link — only the
local `buffer.jsonl`.

## Status: experimental

Session-key continuity across repeated `openclaw agent` calls is the one piece not yet
independently live-verified against a real Gateway — see the README for details. Test against
your own setup before relying on this in production.
