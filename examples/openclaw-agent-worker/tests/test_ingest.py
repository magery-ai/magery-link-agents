import queue
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import buffer  # noqa: E402
import ingest  # noqa: E402


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

def test_iter_sse_events_yields_incrementally_rather_than_collecting_everything_first():
    calls = []

    def line_source():
        calls.append("before-1")
        yield 'event: message'
        calls.append("before-2")
        yield 'data: {"id": 1}'
        calls.append("before-3")
        yield ''

    gen = ingest.iter_sse_events(line_source())
    assert calls == []
    first = next(gen)
    assert first == ("message", {"id": 1})
    assert calls == ["before-1", "before-2"]


def test_iter_sse_events_ignores_data_with_no_preceding_event_type():
    lines = ['data: {"id": 1}', '']
    assert list(ingest.iter_sse_events(lines)) == []


def test_next_backoff_doubles_up_to_the_cap():
    assert ingest.next_backoff(1) == 2
    assert ingest.next_backoff(2) == 4
    assert ingest.next_backoff(16) == 30
    assert ingest.next_backoff(30) == 30


def test_deduper_is_new_reports_each_unmarked_id_as_new_until_marked_seen():
    dedup = ingest.Deduper()
    assert dedup.is_new(1) is True
    dedup.mark_seen(1)
    assert dedup.is_new(1) is False


def test_deduper_is_new_respects_a_seeded_last_id():
    dedup = ingest.Deduper(last_id=5)
    assert dedup.is_new(5) is False
    assert dedup.is_new(6) is True


def test_deduper_mark_seen_only_advances_the_watermark_forward():
    dedup = ingest.Deduper(last_id=5)
    dedup.mark_seen(3)
    assert dedup.last_id == 5
    dedup.mark_seen(7)
    assert dedup.last_id == 7


def test_deduper_has_backfilled_starts_false_for_a_plain_deduper_but_true_when_seeded():
    assert ingest.Deduper().has_backfilled is False
    assert ingest.Deduper(last_id=5).has_backfilled is True


def test_make_record_maps_wire_fields_to_the_buffer_record_schema():
    message = {
        "id": 42, "authorName": "alice", "message": "hi", "createdAt": "2026-08-13T00:00:00",
        "isOwn": False,
    }
    record = ingest.make_record(message, room_id="room-1", agent="main")
    assert record == {
        "id": 42, "room_id": "room-1", "author": "alice", "text": "hi",
        "created_at": "2026-08-13T00:00:00", "agent": "main",
        "session_key": "agent:main:magery-room-1",
        "status": "pending", "attempts": 0, "last_error": None, "next_attempt_at": None,
    }


def test_should_process_returns_true_when_mention_names_is_empty():
    assert ingest.should_process("anything at all", []) is True


def test_should_process_matches_a_configured_name_case_insensitively():
    assert ingest.should_process("hey @Father can you help", ["father"]) is True
    assert ingest.should_process("hey @FATHER can you help", ["father"]) is True


def test_should_process_matches_at_the_very_start_of_the_text():
    assert ingest.should_process("@Father are you there", ["Father"]) is True


def test_should_process_matches_before_trailing_punctuation():
    assert ingest.should_process("thanks @Father!", ["Father"]) is True


def test_should_process_does_not_match_as_a_prefix_of_a_longer_handle():
    assert ingest.should_process("@father_john can you help", ["father"]) is False
    assert ingest.should_process("@fatherX can you help", ["father"]) is False


def test_should_process_does_not_match_without_a_leading_boundary():
    assert ingest.should_process("email@father.com", ["father"]) is False


def test_should_process_returns_false_when_no_configured_name_is_mentioned():
    assert ingest.should_process("just a regular message", ["father"]) is False


def test_should_process_matches_even_when_followed_by_a_non_ascii_letter():
    assert ingest.should_process("hey @Fatheré", ["Father"]) is True


def test_should_process_matches_even_when_followed_by_cjk_characters():
    assert ingest.should_process("hey @Father日本語", ["Father"]) is True


def test_should_process_matches_when_preceded_by_a_unicode_whitespace_character():
    assert ingest.should_process("hi\xa0@Father", ["Father"]) is True


# ---------------------------------------------------------------------------
# backfill()
# ---------------------------------------------------------------------------

def _mock_json_response(status_code, body):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = str(body)
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def test_backfill_bootstrap_seeds_dedup_from_history_without_emitting_anything():
    dedup = ingest.Deduper()
    body = {"messages": [
        {"id": 1, "authorName": "alice", "message": "old", "createdAt": "t1", "isOwn": False},
        {"id": 2, "authorName": "bob", "message": "older-but-higher-id", "createdAt": "t2", "isOwn": False},
        {"id": 3, "authorName": "me", "message": "mine", "createdAt": "t3", "isOwn": True},
    ]}
    with patch("ingest.requests.get", return_value=_mock_json_response(200, body)) as mock_get:
        result = ingest.backfill("https://x/api/v1", "key", "room-1", dedup)
    assert result == []
    assert dedup.last_id == 2
    assert mock_get.call_args.kwargs["params"] == {}


def test_backfill_after_bootstrap_returns_unseen_non_own_messages_without_marking_them_seen():
    dedup = ingest.Deduper(last_id=5)
    body = {"messages": [
        {"id": 6, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": False},
        {"id": 7, "authorName": "me", "message": "mine", "createdAt": "t2", "isOwn": True},
    ]}
    with patch("ingest.requests.get", return_value=_mock_json_response(200, body)) as mock_get:
        result = ingest.backfill("https://x/api/v1", "key", "room-1", dedup)
    assert [m["id"] for m in result] == [6]
    assert dedup.last_id == 5
    assert mock_get.call_args.kwargs["params"] == {"after": 5}


def test_backfill_raises_auth_error_on_401():
    dedup = ingest.Deduper()
    with patch("ingest.requests.get", return_value=_mock_json_response(401, {})):
        with pytest.raises(ingest.AuthError) as exc_info:
            ingest.backfill("https://x/api/v1", "bad-key", "room-1", dedup)
    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# connect_and_stream()
# ---------------------------------------------------------------------------

def _mock_stream_response(status_code, lines):
    resp = MagicMock()
    resp.status_code = status_code
    resp.iter_lines.return_value = iter(lines)
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _no_shutdown():
    shutdown = MagicMock(spec=threading.Event)
    shutdown.is_set.return_value = False
    shutdown.wait.return_value = False
    return shutdown


def test_connect_and_stream_raises_auth_error_on_401(tmp_path):
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    with patch("ingest.requests.get", return_value=_mock_stream_response(401, [])):
        with pytest.raises(ingest.AuthError) as exc_info:
            ingest.connect_and_stream(
                "https://x/api/v1", "bad-key", "room-1", None, "main", [], buffer_path, lock,
                queue.Queue(), ingest.Deduper(), _no_shutdown(),
            )
    assert exc_info.value.status_code == 401


def test_connect_and_stream_emits_new_messages_and_skips_own_and_duplicates(tmp_path):
    lines = [
        'event: message',
        'data: {"id": 1, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": false}', '',
        'event: message',
        'data: {"id": 2, "authorName": "me", "message": "mine", "createdAt": "t2", "isOwn": true}', '',
        'event: message',
        'data: {"id": 1, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": false}', '',
    ]
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    wake = queue.Queue()
    with patch("ingest.requests.get", return_value=_mock_stream_response(200, lines)):
        ingest.connect_and_stream(
            "https://x/api/v1", "key", "room-1", None, "main", [], buffer_path, lock, wake,
            ingest.Deduper(), _no_shutdown(),
        )
    records = buffer.load_all(buffer_path, lock)
    assert [r["id"] for r in records] == [1]
    assert wake.qsize() == 1


def test_connect_and_stream_returns_expired_on_room_expired_event(tmp_path):
    lines = ['event: room_expired', 'data: {"roomId": "room-1"}', '']
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    with patch("ingest.requests.get", return_value=_mock_stream_response(200, lines)):
        outcome, duration = ingest.connect_and_stream(
            "https://x/api/v1", "key", "room-1", None, "main", [], buffer_path, lock,
            queue.Queue(), ingest.Deduper(), _no_shutdown(),
        )
    assert outcome == "expired"
    assert duration >= 0


def test_connect_and_stream_returns_dropped_on_overflow_error_event(tmp_path):
    lines = ['event: error', 'data: {"code": "OVERFLOW", "message": "too slow"}', '']
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    with patch("ingest.requests.get", return_value=_mock_stream_response(200, lines)):
        outcome, duration = ingest.connect_and_stream(
            "https://x/api/v1", "key", "room-1", None, "main", [], buffer_path, lock,
            queue.Queue(), ingest.Deduper(), _no_shutdown(),
        )
    assert outcome == "dropped"
    assert duration >= 0


def test_connect_and_stream_backfills_before_resuming_the_stream(tmp_path):
    dedup = ingest.Deduper(last_id=0)
    backfill_body = {"messages": [
        {"id": 1, "authorName": "alice", "message": "missed", "createdAt": "t1", "isOwn": False},
    ]}
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    with patch("ingest.requests.get") as mock_get:
        mock_get.side_effect = [_mock_json_response(200, backfill_body), _mock_stream_response(200, [])]
        ingest.connect_and_stream(
            "https://x/api/v1", "key", "room-1", None, "main", [], buffer_path, lock,
            queue.Queue(), dedup, _no_shutdown(),
        )
    records = buffer.load_all(buffer_path, lock)
    assert [r["id"] for r in records] == [1]
    assert dedup.last_id == 1


def test_connect_and_stream_returns_dropped_when_the_stream_raises_mid_iteration(tmp_path):
    def line_generator():
        yield 'event: message'
        yield 'data: {"id": 1, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": false}'
        yield ''
        raise requests.ConnectionError("connection reset by peer")

    resp = MagicMock()
    resp.status_code = 200
    resp.iter_lines.return_value = line_generator()
    resp.raise_for_status.return_value = None
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False

    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    with patch("ingest.requests.get", return_value=resp):
        outcome, duration = ingest.connect_and_stream(
            "https://x/api/v1", "key", "room-1", None, "main", [], buffer_path, lock,
            queue.Queue(), ingest.Deduper(), _no_shutdown(),
        )
    assert outcome == "dropped"
    assert [r["id"] for r in buffer.load_all(buffer_path, lock)] == [1]


# ---------------------------------------------------------------------------
# run_ingest()
# ---------------------------------------------------------------------------

def test_run_ingest_returns_immediately_on_auth_error(tmp_path):
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    shutdown = _no_shutdown()
    exit_codes: dict[str, int] = {}
    with patch("ingest.connect_and_stream", side_effect=ingest.AuthError(401)) as mock_connect:
        ingest.run_ingest(
            "https://x/api/v1", "key", "room-1", None, "main", [], buffer_path, lock,
            queue.Queue(), shutdown, exit_codes,
        )
    mock_connect.assert_called_once()
    assert exit_codes == {"room-1": 1}


def test_run_ingest_retries_on_dropped_then_returns_on_expired_and_resets_backoff_after_a_healthy_connection(tmp_path):
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    shutdown = _no_shutdown()
    call_count = {"n": 0}

    def fake_connect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "dropped", 10.0
        return "expired", 10.0

    exit_codes: dict[str, int] = {}
    with patch("ingest.connect_and_stream", side_effect=fake_connect):
        ingest.run_ingest(
            "https://x/api/v1", "key", "room-1", None, "main", [], buffer_path, lock,
            queue.Queue(), shutdown, exit_codes,
        )
    assert call_count["n"] == 2
    shutdown.wait.assert_called_once_with(ingest.MIN_BACKOFF_SECONDS)
    assert exit_codes == {"room-1": 0}


def test_run_ingest_does_not_reset_backoff_after_a_connection_that_barely_stayed_up(tmp_path):
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    shutdown = _no_shutdown()
    call_count = {"n": 0}

    def fake_connect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "dropped", 0.1
        return "expired", 10.0

    exit_codes: dict[str, int] = {}
    with patch("ingest.connect_and_stream", side_effect=fake_connect):
        ingest.run_ingest(
            "https://x/api/v1", "key", "room-1", None, "main", [], buffer_path, lock,
            queue.Queue(), shutdown, exit_codes,
        )
    assert call_count["n"] == 2
    shutdown.wait.assert_called_once_with(ingest.next_backoff(ingest.MIN_BACKOFF_SECONDS))
    assert exit_codes == {"room-1": 0}


def test_run_ingest_stops_immediately_when_shutdown_already_set(tmp_path):
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    shutdown = MagicMock(spec=threading.Event)
    shutdown.is_set.return_value = True
    exit_codes: dict[str, int] = {}
    with patch("ingest.connect_and_stream") as mock_connect:
        ingest.run_ingest(
            "https://x/api/v1", "key", "room-1", None, "main", [], buffer_path, lock,
            queue.Queue(), shutdown, exit_codes,
        )
    mock_connect.assert_not_called()
    assert exit_codes == {"room-1": 0}


def test_run_ingest_handles_an_unexpected_exception_without_crashing(tmp_path):
    # Anything other than AuthError/requests.RequestException (a malformed SSE payload causing
    # json.JSONDecodeError, an unexpected message shape causing KeyError, etc.) must not
    # silently kill this thread.
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    shutdown = MagicMock(spec=threading.Event)
    shutdown.is_set.return_value = False
    shutdown.wait.return_value = True  # shutdown arrives during the post-error backoff wait
    exit_codes: dict[str, int] = {}
    with patch("ingest.connect_and_stream", side_effect=KeyError("isOwn")):
        ingest.run_ingest(
            "https://x/api/v1", "key", "room-1", None, "main", [], buffer_path, lock,
            queue.Queue(), shutdown, exit_codes,
        )
    # Returning without raising is part of the assertion; shutdown arrived mid-backoff, so the
    # room's outcome is coded 2 (a retry was in flight and got interrupted), not a clean 0.
    assert exit_codes == {"room-1": 2}


def test_connect_and_stream_skips_a_non_matching_sse_message_but_still_marks_it_seen(tmp_path):
    lines = [
        'event: message',
        'data: {"id": 1, "authorName": "alice", "message": "hi everyone", "createdAt": "t1", "isOwn": false}', '',
        'event: message',
        'data: {"id": 2, "authorName": "alice", "message": "hi @Father", "createdAt": "t2", "isOwn": false}', '',
    ]
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    wake = queue.Queue()
    dedup = ingest.Deduper()
    with patch("ingest.requests.get", return_value=_mock_stream_response(200, lines)):
        ingest.connect_and_stream(
            "https://x/api/v1", "key", "room-1", None, "main", ["Father"], buffer_path, lock, wake,
            dedup, _no_shutdown(),
        )
    records = buffer.load_all(buffer_path, lock)
    assert [r["id"] for r in records] == [2]
    assert dedup.last_id == 2


def test_connect_and_stream_skips_a_non_matching_backfilled_message_but_still_marks_it_seen(tmp_path):
    dedup = ingest.Deduper(last_id=0)
    backfill_body = {"messages": [
        {"id": 1, "authorName": "alice", "message": "missed, no mention", "createdAt": "t1", "isOwn": False},
        {"id": 2, "authorName": "alice", "message": "missed, @Father are you there", "createdAt": "t2", "isOwn": False},
    ]}
    buffer_path = str(tmp_path / "buffer.jsonl")
    lock = buffer.FileLock(buffer_path)
    with patch("ingest.requests.get") as mock_get:
        mock_get.side_effect = [_mock_json_response(200, backfill_body), _mock_stream_response(200, [])]
        ingest.connect_and_stream(
            "https://x/api/v1", "key", "room-1", None, "main", ["Father"], buffer_path, lock,
            queue.Queue(), dedup, _no_shutdown(),
        )
    records = buffer.load_all(buffer_path, lock)
    assert [r["id"] for r in records] == [2]
    assert dedup.last_id == 2
