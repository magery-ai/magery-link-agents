import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as config_module  # noqa: E402


def _write_config(tmp_path, **overrides):
    data = {
        "base_url": "https://link.magery.ai/api/v1",
        "access_key": "plain-test-key",
        "buffer_path": "./buffer.jsonl",
        "agent": "main",
        "rooms": [{"room_id": "abc123", "label": "test"}],
    }
    data.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(data))
    return str(path)


def test_load_config_uses_documented_defaults_when_optional_fields_omitted(tmp_path):
    path = _write_config(tmp_path)
    cfg = config_module.load_config(path)
    assert cfg.base_url == "https://link.magery.ai/api/v1"
    assert cfg.access_key == "plain-test-key"
    assert cfg.buffer_path == "./buffer.jsonl"
    assert cfg.default_agent == "main"
    assert cfg.prompt_template == config_module.DEFAULT_PROMPT_TEMPLATE
    assert cfg.timeout == 180
    assert cfg.max_attempts == 3
    assert cfg.chunk_size == 4096
    assert len(cfg.rooms) == 1
    assert cfg.rooms[0].room_id == "abc123"
    assert cfg.rooms[0].label == "test"
    assert cfg.rooms[0].agent is None


def test_load_config_honors_explicit_prompt_template(tmp_path):
    path = _write_config(tmp_path, prompt_template="Custom | {author}: {text}")
    cfg = config_module.load_config(path)
    assert cfg.prompt_template == "Custom | {author}: {text}"


def test_load_config_resolves_env_var_access_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGERY_ACCESS_KEY", "resolved-from-env")
    path = _write_config(tmp_path, access_key="${MAGERY_ACCESS_KEY}")
    cfg = config_module.load_config(path)
    assert cfg.access_key == "resolved-from-env"


def test_load_config_raises_when_env_var_access_key_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGERY_ACCESS_KEY", raising=False)
    path = _write_config(tmp_path, access_key="${MAGERY_ACCESS_KEY}")
    with pytest.raises(ValueError, match="MAGERY_ACCESS_KEY"):
        config_module.load_config(path)


def test_load_config_raises_when_rooms_missing(tmp_path):
    data = {"access_key": "plain-test-key"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(data))
    with pytest.raises(KeyError):
        config_module.load_config(str(path))


def test_load_config_raises_when_access_key_missing(tmp_path):
    data = {"rooms": [{"room_id": "abc123"}]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(data))
    with pytest.raises(KeyError):
        config_module.load_config(str(path))


def test_load_config_raises_on_duplicate_room_ids(tmp_path):
    path = _write_config(tmp_path, rooms=[
        {"room_id": "abc123", "label": "first"}, {"room_id": "abc123", "label": "second"},
    ])
    with pytest.raises(ValueError, match="abc123"):
        config_module.load_config(path)


def test_agent_for_falls_back_to_default_when_room_has_no_override(tmp_path):
    path = _write_config(tmp_path, agent="main", rooms=[
        {"room_id": "abc123"}, {"room_id": "def456", "agent": "support-bot"},
    ])
    cfg = config_module.load_config(path)
    default_room, override_room = cfg.rooms
    assert cfg.agent_for(default_room) == "main"
    assert cfg.agent_for(override_room) == "support-bot"
