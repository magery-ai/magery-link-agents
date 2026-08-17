"""Bidirectional bridge configuration: load and validate the YAML config file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

DEFAULT_BASE_URL = "https://link.magery.ai/api/v1"
DEFAULT_PROMPT_TEMPLATE = "📨 Magery | {author}: {text}"


@dataclass
class RoomConfig:
    room_id: str
    label: str | None = None
    agent: str | None = None


@dataclass
class Config:
    base_url: str
    access_key: str
    buffer_path: str
    default_agent: str
    prompt_template: str
    timeout: int
    max_attempts: int
    chunk_size: int
    rooms: list[RoomConfig]
    mention_names: list[str] = field(default_factory=list)

    def agent_for(self, room: RoomConfig) -> str:
        return room.agent or self.default_agent


def load_config(path: str) -> Config:
    with open(path) as f:
        data = yaml.safe_load(f)

    rooms = [
        RoomConfig(room_id=r["room_id"], label=r.get("label"), agent=r.get("agent"))
        for r in data["rooms"]
    ]
    _check_no_duplicate_room_ids(rooms)

    return Config(
        base_url=data.get("base_url", DEFAULT_BASE_URL),
        access_key=_resolve_access_key(data["access_key"]),
        buffer_path=data.get("buffer_path", "./buffer.jsonl"),
        default_agent=data.get("agent", "main"),
        prompt_template=data.get("prompt_template", DEFAULT_PROMPT_TEMPLATE),
        timeout=data.get("timeout", 180),
        max_attempts=data.get("max_attempts", 3),
        chunk_size=data.get("chunk_size", 4096),
        rooms=rooms,
        mention_names=data.get("mention_names", []),
    )


def _check_no_duplicate_room_ids(rooms: list[RoomConfig]) -> None:
    """buffer.update_record() matches records by id alone and mutates only the first match — it
    has no way to know two ingest threads are both writing records for the same room. If a
    room_id is accidentally listed twice (an easy config typo/copy-paste), two ingest threads
    would both watch that room and every message would get ingested and processed twice,
    forever, with duplicate replies posted back. Catch that at load time instead."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for room in rooms:
        if room.room_id in seen and room.room_id not in duplicates:
            duplicates.append(room.room_id)
        seen.add(room.room_id)
    if duplicates:
        raise ValueError(f"duplicate room_id(s) in config: {', '.join(duplicates)}")


def _resolve_access_key(raw: str) -> str:
    if raw.startswith("${") and raw.endswith("}"):
        env_var = raw[2:-1]
        value = os.environ.get(env_var)
        if not value:
            raise ValueError(f"environment variable {env_var} is not set")
        return value
    return raw
