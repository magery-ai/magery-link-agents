import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import buffer  # noqa: E402
import worker  # noqa: E402
from config import Config  # noqa: E402


def _config(**overrides):
    defaults = dict(
        base_url="https://x/api/v1", access_key="key", buffer_path="unused",
        default_agent="main", prompt_template="📨 Magery | {author}: {text}",
        timeout=180, max_attempts=3, chunk_size=4096, rooms=[],
    )
    defaults.update(overrides)
    return Config(**defaults)


def _record(**overrides):
    base = {
        "id": 1, "room_id": "room-1", "author": "alice", "text": "hi",
        "created_at": "t1", "agent": "main", "session_key": "agent:main:magery-room-1",
        "status": "pending", "attempts": 0, "last_error": None, "next_attempt_at": None,
    }
    base.update(overrides)
    return base


def _mock_subprocess_result(returncode, stdout_obj):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = json.dumps(stdout_obj)
    result.stderr = ""
    return result


def _mock_response(status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

def test_build_message_formats_the_configured_prompt_template():
    config = _config(prompt_template="Custom | {author}: {text}")
    record = _record(author="alice", text="hello")
    assert worker.build_message(config, record) == "Custom | alice: hello"


def test_reply_text_joins_multiple_payloads():
    response = {"result": {"payloads": [{"text": "part one. "}, {"text": "part two."}]}}
    assert worker.reply_text(response) == "part one. part two."


def test_reply_text_returns_empty_string_for_no_payloads():
    assert worker.reply_text({"result": {"payloads": []}}) == ""
    assert worker.reply_text({"result": {}}) == ""
    assert worker.reply_text({}) == ""


def test_reply_text_strips_whitespace_only_replies_to_empty():
    response = {"result": {"payloads": [{"text": "   \n  "}]}}
    assert worker.reply_text(response) == ""


# ---------------------------------------------------------------------------
# run_agent_turn()
# ---------------------------------------------------------------------------

def test_run_agent_turn_invokes_openclaw_cli_with_the_documented_arguments():
    config = _config(timeout=180)
    record = _record()
    result = _mock_subprocess_result(0, {"status": "ok", "result": {"payloads": [{"text": "hi"}]}})
    with patch("worker.subprocess.run", return_value=result) as mock_run:
        worker.run_agent_turn(config, record)
    args, kwargs = mock_run.call_args
    assert args[0] == [
        "openclaw", "agent", "--agent", "main", "--session-key", "agent:main:magery-room-1",
        "--message", "📨 Magery | alice: hi", "--json", "--timeout", "180",
    ]
    assert kwargs["timeout"] == 190


def test_run_agent_turn_raises_on_non_ok_status():
    config = _config()
    record = _record()
    result = _mock_subprocess_result(0, {"status": "timeout", "summary": "no reply"})
    with patch("worker.subprocess.run", return_value=result):
        with pytest.raises(RuntimeError, match="timeout"):
            worker.run_agent_turn(config, record)


def test_run_agent_turn_raises_on_non_zero_exit_code():
    config = _config()
    record = _record()
    result = _mock_subprocess_result(1, {"status": "ok"})
    with patch("worker.subprocess.run", return_value=result):
        with pytest.raises(RuntimeError):
            worker.run_agent_turn(config, record)


def test_run_agent_turn_propagates_timeout_expired():
    config = _config()
    record = _record()
    with patch(
        "worker.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="openclaw", timeout=190),
    ):
        with pytest.raises(subprocess.TimeoutExpired):
            worker.run_agent_turn(config, record)


def test_run_agent_turn_propagates_malformed_json():
    config = _config()
    record = _record()
    result = MagicMock(returncode=0, stdout="not json", stderr="")
    with patch("worker.subprocess.run", return_value=result):
        with pytest.raises(json.JSONDecodeError):
            worker.run_agent_turn(config, record)


# ---------------------------------------------------------------------------
# process_one() — retry policy state transitions
# ---------------------------------------------------------------------------

@pytest.fixture
def buffer_path(tmp_path):
    return str(tmp_path / "buffer.jsonl")


def test_process_one_marks_done_and_posts_the_reply_on_success(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(), lock)
    config = _config(chunk_size=4096)
    result = _mock_subprocess_result(0, {"status": "ok", "result": {"payloads": [{"text": "hello back"}]}})
    with patch("worker.subprocess.run", return_value=result), \
         patch("worker.requests.post", return_value=_mock_response()) as mock_post:
        worker.process_one(config, buffer_path, lock, _record())
    record = buffer.load_all(buffer_path, lock)[0]
    assert record["status"] == "done"
    assert record["last_error"] is None
    posted_bodies = [c.kwargs["json"] for c in mock_post.call_args_list if "json" in c.kwargs]
    assert {"message": "hello back"} in posted_bodies


def test_process_one_posts_nothing_and_still_marks_done_on_an_empty_reply(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(), lock)
    config = _config()
    result = _mock_subprocess_result(0, {"status": "ok", "result": {"payloads": []}})
    with patch("worker.subprocess.run", return_value=result), \
         patch("worker.requests.post", return_value=_mock_response()) as mock_post:
        worker.process_one(config, buffer_path, lock, _record())
    record = buffer.load_all(buffer_path, lock)[0]
    assert record["status"] == "done"
    assert record["last_error"] is None
    reply_posts = [c for c in mock_post.call_args_list if "/messages" in c.args[0]]
    assert reply_posts == []


def test_process_one_chunks_a_reply_longer_than_chunk_size(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(), lock)
    config = _config(chunk_size=5)
    result = _mock_subprocess_result(0, {"status": "ok", "result": {"payloads": [{"text": "abcdefghij"}]}})
    with patch("worker.subprocess.run", return_value=result), \
         patch("worker.requests.post", return_value=_mock_response()) as mock_post:
        worker.process_one(config, buffer_path, lock, _record())
    reply_posts = [
        c.kwargs["json"]["message"] for c in mock_post.call_args_list if "/messages" in c.args[0]
    ]
    assert reply_posts == ["abcde", "fghij"]


def test_process_one_schedules_a_retry_with_backoff_when_attempts_remain(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(attempts=0), lock)
    config = _config(max_attempts=3)
    with patch(
        "worker.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="openclaw", timeout=190),
    ), patch("worker.requests.post", return_value=_mock_response()):
        worker.process_one(config, buffer_path, lock, _record(attempts=0))
    record = buffer.load_all(buffer_path, lock)[0]
    assert record["status"] == "pending"
    assert record["attempts"] == 1
    assert record["last_error"]
    assert record["next_attempt_at"] is not None


def test_process_one_marks_failed_at_max_attempts(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(attempts=2), lock)
    config = _config(max_attempts=3)
    with patch(
        "worker.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="openclaw", timeout=190),
    ), patch("worker.requests.post", return_value=_mock_response()):
        worker.process_one(config, buffer_path, lock, _record(attempts=2))
    record = buffer.load_all(buffer_path, lock)[0]
    assert record["status"] == "failed"
    assert record["attempts"] == 3
    assert record["next_attempt_at"] is None


def test_process_one_retries_on_a_failed_reply_post(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(), lock)
    config = _config()
    result = _mock_subprocess_result(0, {"status": "ok", "result": {"payloads": [{"text": "hi"}]}})
    with patch("worker.subprocess.run", return_value=result), \
         patch("worker.requests.post", return_value=_mock_response(status_code=500)):
        worker.process_one(config, buffer_path, lock, _record())
    record = buffer.load_all(buffer_path, lock)[0]
    assert record["status"] == "pending"
    assert record["attempts"] == 1


def test_process_one_attempts_thinking_before_running_the_agent_turn(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(), lock)
    config = _config()
    result = _mock_subprocess_result(0, {"status": "ok", "result": {"payloads": [{"text": "hi"}]}})
    with patch("worker.subprocess.run", return_value=result), \
         patch("worker.requests.post", return_value=_mock_response()) as mock_post:
        worker.process_one(config, buffer_path, lock, _record())
    thinking_posts = [c for c in mock_post.call_args_list if "/thinking" in c.args[0]]
    assert len(thinking_posts) == 1


def test_process_one_does_not_abort_the_turn_when_the_thinking_post_fails(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(), lock)
    config = _config()
    result = _mock_subprocess_result(0, {"status": "ok", "result": {"payloads": [{"text": "hi"}]}})

    def fake_post(url, **kwargs):
        if "/thinking" in url:
            raise requests.ConnectionError("thinking endpoint down")
        return _mock_response()

    with patch("worker.subprocess.run", return_value=result), \
         patch("worker.requests.post", side_effect=fake_post):
        worker.process_one(config, buffer_path, lock, _record())
    record = buffer.load_all(buffer_path, lock)[0]
    assert record["status"] == "done"


def test_process_one_re_pings_thinking_repeatedly_during_a_long_agent_turn(buffer_path):
    # The thinking indicator expires client-side 15s after the last ping, but an agent turn can
    # run far longer than that — process_one must keep re-pinging in the background for the
    # whole turn, not just once at the start.
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(), lock)
    config = _config()
    result = _mock_subprocess_result(0, {"status": "ok", "result": {"payloads": [{"text": "hi"}]}})

    def slow_run(*args, **kwargs):
        # Long enough, relative to the tiny patched ping interval below, for the background
        # pinger to fire more than once before this mocked call returns.
        time.sleep(0.25)
        return result

    with patch("worker.THINKING_PING_INTERVAL_SECONDS", 0.05), \
         patch("worker.subprocess.run", side_effect=slow_run), \
         patch("worker.requests.post", return_value=_mock_response()) as mock_post:
        worker.process_one(config, buffer_path, lock, _record())

    thinking_posts = [c for c in mock_post.call_args_list if "/thinking" in c.args[0]]
    assert len(thinking_posts) > 1


def test_process_one_stops_the_pinger_thread_even_when_the_turn_fails(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(), lock)
    config = _config()
    threads_before = threading.active_count()

    with patch("worker.THINKING_PING_INTERVAL_SECONDS", 0.05), \
         patch(
             "worker.subprocess.run",
             side_effect=subprocess.TimeoutExpired(cmd="openclaw", timeout=190),
         ), \
         patch("worker.requests.post", return_value=_mock_response()):
        worker.process_one(config, buffer_path, lock, _record())

    # process_one's finally block joins the pinger thread before returning, so no thread should
    # be left running past that point regardless of whether the turn succeeded or failed.
    assert threading.active_count() == threads_before


# ---------------------------------------------------------------------------
# run_worker() — backoff-skip and startup recovery
# ---------------------------------------------------------------------------

def test_run_worker_skips_a_pending_record_with_a_future_next_attempt_at_but_processes_a_ready_one(buffer_path):
    lock = buffer.FileLock(buffer_path)
    future = buffer.compute_next_attempt_at(1)  # ~30s from now
    buffer.append(buffer_path, _record(id=1, next_attempt_at=future), lock)
    buffer.append(buffer_path, _record(id=2, next_attempt_at=None), lock)
    config = _config()
    wake = queue.Queue()
    shutdown = threading.Event()
    processed = []

    def fake_process_one(cfg, path, lk, record):
        processed.append(record["id"])
        buffer.update_record(path, lk, record["id"], lambda r: {**r, "status": "done"})
        shutdown.set()

    with patch("worker.process_one", side_effect=fake_process_one):
        worker.run_worker(config, buffer_path, lock, wake, shutdown)

    assert processed == [2]


def test_run_worker_resets_a_stuck_processing_record_at_startup_and_then_processes_it(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(id=1, status="processing"), lock)
    config = _config()
    wake = queue.Queue()
    shutdown = threading.Event()
    processed = []

    def fake_process_one(cfg, path, lk, record):
        processed.append((record["id"], record["status"]))
        buffer.update_record(path, lk, record["id"], lambda r: {**r, "status": "done"})
        shutdown.set()

    with patch("worker.process_one", side_effect=fake_process_one):
        worker.run_worker(config, buffer_path, lock, wake, shutdown)

    assert processed == [(1, "pending")]


def test_run_worker_survives_reset_stuck_processing_raising_at_startup(buffer_path):
    # e.g. a corrupted/truncated last line in the buffer file from a process killed mid-append.
    # This must not silently kill the worker thread at startup — ingest would keep appending
    # forever while nothing ever processes the buffer again.
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(id=1), lock)
    config = _config()
    wake = queue.Queue()
    shutdown = threading.Event()
    processed = []

    def fake_process_one(cfg, path, lk, record):
        processed.append(record["id"])
        buffer.update_record(path, lk, record["id"], lambda r: {**r, "status": "done"})
        shutdown.set()

    with patch("worker.reset_stuck_processing", side_effect=RuntimeError("corrupted line")), \
         patch("worker.process_one", side_effect=fake_process_one):
        worker.run_worker(config, buffer_path, lock, wake, shutdown)

    assert processed == [1]


def test_run_worker_stops_immediately_when_shutdown_already_set(buffer_path):
    lock = buffer.FileLock(buffer_path)
    config = _config()
    wake = queue.Queue()
    shutdown = threading.Event()
    shutdown.set()
    with patch("worker.process_one") as mock_process:
        worker.run_worker(config, buffer_path, lock, wake, shutdown)
    mock_process.assert_not_called()


def test_run_worker_continues_after_an_unexpected_exception_in_process_one(buffer_path):
    lock = buffer.FileLock(buffer_path)
    buffer.append(buffer_path, _record(id=1), lock)
    config = _config()
    wake = queue.Queue()
    shutdown = threading.Event()
    call_count = {"n": 0}

    def fake_process_one(cfg, path, lk, record):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        shutdown.set()

    with patch("worker.process_one", side_effect=fake_process_one):
        worker.run_worker(config, buffer_path, lock, wake, shutdown)

    assert call_count["n"] >= 2
