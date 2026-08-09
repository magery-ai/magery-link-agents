import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge  # noqa: E402


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

def test_parse_sse_lines_parses_a_complete_message_event():
    lines = ['event: message', 'data: {"id": 1, "message": "hi"}', '']
    events = bridge.parse_sse_lines(lines)
    assert events == [("message", {"id": 1, "message": "hi"})]


def test_parse_sse_lines_parses_multiple_events_separated_by_blank_lines():
    lines = [
        'event: heartbeat', 'data: {"ts": "2026-08-08T00:00:00"}', '',
        'event: message', 'data: {"id": 2, "message": "second"}', '',
    ]
    events = bridge.parse_sse_lines(lines)
    assert events == [
        ("heartbeat", {"ts": "2026-08-08T00:00:00"}),
        ("message", {"id": 2, "message": "second"}),
    ]


def test_parse_sse_lines_ignores_data_with_no_preceding_event_type():
    lines = ['data: {"id": 1}', '']
    assert bridge.parse_sse_lines(lines) == []


def test_parse_sse_lines_is_a_thin_wrapper_over_the_shared_iter_sse_events_generator():
    # This is the parser connect_and_stream() actually consumes live (Fix 6): parse_sse_lines
    # must be built on it, not a separate/duplicated implementation, so the two can't drift.
    lines = ['event: message', 'data: {"id": 1}', '']
    assert list(bridge.iter_sse_events(lines)) == bridge.parse_sse_lines(lines)


def test_iter_sse_events_yields_incrementally_rather_than_collecting_everything_first():
    calls = []

    def line_source():
        calls.append("before-1")
        yield 'event: message'
        calls.append("before-2")
        yield 'data: {"id": 1}'
        calls.append("before-3")
        yield ''

    gen = bridge.iter_sse_events(line_source())
    assert calls == []  # nothing pulled yet — it's lazy
    first = next(gen)
    assert first == ("message", {"id": 1})
    # Only the lines needed to produce one event were pulled — the "" blank line that ends the
    # block hasn't been consumed yet, proving this is genuinely incremental, not "parse
    # everything up front and hand back a list".
    assert calls == ["before-1", "before-2"]


def test_next_backoff_doubles_up_to_the_cap():
    assert bridge.next_backoff(1) == 2
    assert bridge.next_backoff(2) == 4
    assert bridge.next_backoff(16) == 30
    assert bridge.next_backoff(30) == 30


def test_deduper_is_new_reports_each_unmarked_id_as_new_until_marked_seen():
    dedup = bridge.Deduper()
    assert dedup.is_new(1) is True
    assert dedup.is_new(2) is True
    dedup.mark_seen(1)
    assert dedup.is_new(1) is False
    assert dedup.is_new(2) is True  # not marked yet — still new
    dedup.mark_seen(2)
    assert dedup.is_new(2) is False


def test_deduper_is_new_respects_a_seeded_last_id():
    dedup = bridge.Deduper(last_id=5)
    assert dedup.is_new(5) is False
    assert dedup.is_new(4) is False
    assert dedup.is_new(6) is True


def test_deduper_mark_seen_only_advances_the_watermark_forward():
    dedup = bridge.Deduper(last_id=5)
    dedup.mark_seen(3)  # lower than last_id — must not move it backward
    assert dedup.last_id == 5
    dedup.mark_seen(7)
    assert dedup.last_id == 7


def test_deduper_has_backfilled_starts_false_for_a_plain_deduper_but_true_when_seeded():
    # A plain Deduper() (no seed) represents "nothing established yet" — its first backfill()
    # call is a genuine bootstrap. A Deduper constructed with a last_id (an explicit --last-id
    # "resume from here" request) represents the caller declaring a starting point already
    # exists, so its first backfill() call must NOT be treated as an unseeded bootstrap.
    assert bridge.Deduper().has_backfilled is False
    assert bridge.Deduper(last_id=5).has_backfilled is True


def test_format_stdout_maps_wire_fields_to_output_fields():
    message = {"id": 42, "authorName": "alice", "message": "hi", "createdAt": "2026-08-08T00:00:00", "isOwn": False}
    line = bridge.format_stdout(message, room_id="room-1", room_label="test")
    assert json.loads(line) == {
        "id": 42, "author": "alice", "message": "hi",
        "room_id": "room-1", "room_label": "test", "created_at": "2026-08-08T00:00:00",
    }


def test_format_openclaw_payload_matches_the_confirmed_shape():
    message = {"authorName": "alice", "message": "hello"}
    assert bridge.format_openclaw_payload(message) == {
        "text": "[Magery] alice: hello", "source": "magery-link",
    }


def test_parse_webhook_headers_splits_on_first_colon_space():
    headers = bridge.parse_webhook_headers(["X-Api-Key: secret", "Authorization: Bearer abc"])
    assert headers == {"X-Api-Key": "secret", "Authorization": "Bearer abc"}


def test_parse_webhook_headers_returns_empty_dict_for_no_headers():
    assert bridge.parse_webhook_headers([]) == {}


# ---------------------------------------------------------------------------
# backfill()
# ---------------------------------------------------------------------------

def _mock_json_response(status_code: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def test_backfill_bootstrap_seeds_dedup_from_history_without_emitting_anything():
    # On the very first call for a room (dedup.last_id is None), the server has no "since"
    # point to backfill from and returns recent history instead. That history must NOT be
    # surfaced as "new" — a fresh start/restart shouldn't replay stale messages — so backfill()
    # silently seeds the watermark from it and returns nothing to emit.
    dedup = bridge.Deduper()
    body = {"messages": [
        {"id": 1, "authorName": "alice", "message": "old", "createdAt": "t1", "isOwn": False},
        {"id": 2, "authorName": "bob", "message": "older-but-higher-id", "createdAt": "t2", "isOwn": False},
        {"id": 3, "authorName": "me", "message": "mine", "createdAt": "t3", "isOwn": True},
    ]}
    with patch("bridge.requests.get", return_value=_mock_json_response(200, body)) as mock_get:
        result = bridge.backfill("https://x/api/v1", "key", "room-1", dedup)
    assert result == []
    assert dedup.last_id == 2
    assert mock_get.call_args.kwargs["params"] == {}


def test_backfill_after_bootstrap_returns_unseen_non_own_messages_without_marking_them_seen():
    # Once dedup already has a watermark (a prior reconnect, or an explicit --last-id), a
    # backfill call is a genuine gap-fill and should return what's new. It must NOT mark those
    # messages seen itself, though — that's the caller's job, done only after each message is
    # actually delivered (see connect_and_stream), so a sink failure can't silently lose one.
    dedup = bridge.Deduper(last_id=5)
    body = {"messages": [
        {"id": 6, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": False},
        {"id": 7, "authorName": "me", "message": "mine", "createdAt": "t2", "isOwn": True},
    ]}
    with patch("bridge.requests.get", return_value=_mock_json_response(200, body)) as mock_get:
        result = bridge.backfill("https://x/api/v1", "key", "room-1", dedup)
    assert [m["id"] for m in result] == [6]
    assert dedup.last_id == 5  # unchanged — backfill() itself no longer marks seen
    assert mock_get.call_args.kwargs["params"] == {"after": 5}


def test_backfill_passes_after_param_once_a_last_id_is_known():
    dedup = bridge.Deduper(last_id=5)
    with patch("bridge.requests.get", return_value=_mock_json_response(200, {"messages": []})) as mock_get:
        bridge.backfill("https://x/api/v1", "key", "room-1", dedup)
    assert mock_get.call_args.kwargs["params"] == {"after": 5}


def test_backfill_second_call_after_an_empty_first_call_is_not_treated_as_bootstrap():
    # The specific bug this guards against: a room that's genuinely empty (or has only the
    # agent's own messages) when the bridge starts leaves dedup.last_id at None even after a
    # successful first backfill() call — there's nothing to seed the watermark from. That must
    # NOT make a LATER reconnect's backfill() call also look like a fresh bootstrap and get
    # suppressed; is_bootstrap has to be driven by has_backfilled (an explicit "has a backfill
    # call ever completed" flag), not by last_id's nullability, which stays None in both the
    # true-first-call case and this quiet-room case.
    dedup = bridge.Deduper()
    with patch("bridge.requests.get", return_value=_mock_json_response(200, {"messages": []})):
        first_result = bridge.backfill("https://x/api/v1", "key", "room-1", dedup)
    assert first_result == []
    assert dedup.last_id is None  # nothing to seed from — still None, same as it started
    assert dedup.has_backfilled is True  # but this call DID complete successfully

    # A reconnect happens (simulated here as a second backfill() call on the same Deduper
    # instance) and a genuinely new message has arrived in the meantime.
    body = {"messages": [
        {"id": 1, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": False},
    ]}
    with patch("bridge.requests.get", return_value=_mock_json_response(200, body)):
        second_result = bridge.backfill("https://x/api/v1", "key", "room-1", dedup)
    assert [m["id"] for m in second_result] == [1]  # NOT suppressed as a bootstrap


# ---------------------------------------------------------------------------
# connect_and_stream()
# ---------------------------------------------------------------------------

def _mock_stream_response(status_code: int, lines: list[str]) -> MagicMock:
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


def test_connect_and_stream_raises_auth_error_on_401():
    with patch("bridge.requests.get", return_value=_mock_stream_response(401, [])):
        with pytest.raises(bridge.AuthError) as exc_info:
            bridge.connect_and_stream("https://x/api/v1", "bad-key", "room-1", None, "stdout", {}, bridge.Deduper())
    assert exc_info.value.status_code == 401


def test_connect_and_stream_raises_auth_error_on_404():
    with patch("bridge.requests.get", return_value=_mock_stream_response(404, [])):
        with pytest.raises(bridge.AuthError) as exc_info:
            bridge.connect_and_stream("https://x/api/v1", "key", "missing-room", None, "stdout", {}, bridge.Deduper())
    assert exc_info.value.status_code == 404


def test_connect_and_stream_returns_expired_on_room_expired_event():
    lines = ['event: room_expired', 'data: {"roomId": "room-1"}', '']
    with patch("bridge.requests.get", return_value=_mock_stream_response(200, lines)):
        outcome, duration = bridge.connect_and_stream("https://x/api/v1", "key", "room-1", None, "stdout", {}, bridge.Deduper())
    assert outcome == "expired"
    assert duration >= 0


def test_connect_and_stream_returns_dropped_on_overflow_error_event():
    lines = ['event: error', 'data: {"code": "OVERFLOW", "message": "too slow"}', '']
    with patch("bridge.requests.get", return_value=_mock_stream_response(200, lines)):
        outcome, duration = bridge.connect_and_stream("https://x/api/v1", "key", "room-1", None, "stdout", {}, bridge.Deduper())
    assert outcome == "dropped"
    assert duration >= 0


def test_connect_and_stream_emits_new_messages_and_skips_own_and_duplicates():
    lines = [
        'event: message',
        'data: {"id": 1, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": false}', '',
        'event: message',
        'data: {"id": 2, "authorName": "me", "message": "mine", "createdAt": "t2", "isOwn": true}', '',
        'event: message',
        'data: {"id": 1, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": false}', '',
    ]
    emitted = []
    with patch("bridge.requests.get", return_value=_mock_stream_response(200, lines)):
        with patch.dict(bridge.OUTPUT_SINKS, {"stdout": lambda m, room_id, room_label, **_: emitted.append(m["id"])}):
            bridge.connect_and_stream("https://x/api/v1", "key", "room-1", None, "stdout", {}, bridge.Deduper())
    assert emitted == [1]


def test_connect_and_stream_backfills_before_resuming_the_stream():
    # A seeded (non-bootstrap) dedup, representing a genuine reconnect after having already
    # seen some messages — a first-ever connect (no seed) intentionally does NOT emit
    # backfilled history; see the bootstrap-suppression tests above.
    dedup = bridge.Deduper(last_id=0)
    backfill_body = {"messages": [
        {"id": 1, "authorName": "alice", "message": "missed", "createdAt": "t1", "isOwn": False},
    ]}
    emitted = []
    with patch("bridge.requests.get") as mock_get:
        mock_get.side_effect = [_mock_json_response(200, backfill_body), _mock_stream_response(200, [])]
        with patch.dict(bridge.OUTPUT_SINKS, {"stdout": lambda m, room_id, room_label, **_: emitted.append(m["id"])}):
            bridge.connect_and_stream("https://x/api/v1", "key", "room-1", None, "stdout", {}, dedup)
    assert emitted == [1]
    assert dedup.last_id == 1


def test_connect_and_stream_dedups_a_message_delivered_by_both_backfill_and_the_live_stream():
    # A realistic race: backfill picks up id=1 (published just before this connection opened),
    # and the live stream ALSO happens to deliver id=1 again (e.g. it was in-flight to the
    # publish/queue at the moment backfill ran). The shared Deduper instance must catch this.
    dedup = bridge.Deduper(last_id=0)
    backfill_body = {"messages": [
        {"id": 1, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": False},
    ]}
    stream_lines = [
        'event: message',
        'data: {"id": 1, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": false}', '',
        'event: message',
        'data: {"id": 2, "authorName": "alice", "message": "new", "createdAt": "t2", "isOwn": false}', '',
    ]
    emitted = []
    with patch("bridge.requests.get") as mock_get:
        mock_get.side_effect = [_mock_json_response(200, backfill_body), _mock_stream_response(200, stream_lines)]
        with patch.dict(bridge.OUTPUT_SINKS, {"stdout": lambda m, room_id, room_label, **_: emitted.append(m["id"])}):
            bridge.connect_and_stream("https://x/api/v1", "key", "room-1", None, "stdout", {}, dedup)
    assert emitted == [1, 2]


def test_connect_and_stream_returns_dropped_when_the_stream_raises_mid_iteration():
    # A realistic disconnect: the connection is established (past the 401/404 check and
    # raise_for_status()), some events are processed normally, and then iterating the response
    # raises (e.g. ChunkedEncodingError/ConnectionError) instead of the iterator just ending
    # cleanly. This must be treated the same as a clean "dropped" outcome, not left to propagate
    # as an uncaught exception.
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

    emitted = []
    with patch("bridge.requests.get", return_value=resp):
        with patch.dict(bridge.OUTPUT_SINKS, {"stdout": lambda m, room_id, room_label, **_: emitted.append(m["id"])}):
            outcome, duration = bridge.connect_and_stream("https://x/api/v1", "key", "room-1", None, "stdout", {}, bridge.Deduper())
    assert outcome == "dropped"
    assert emitted == [1]


def test_connect_and_stream_returns_dropped_on_a_read_timeout_mid_stream():
    # The stream's bounded read timeout surfaces as requests.exceptions.ReadTimeout when the
    # connection goes half-open (e.g. NAT/firewall state expiry) with no further bytes arriving
    # — the single most common real-world SSE failure mode. It's a RequestException subclass,
    # so it's handled by the exact same code path as any other mid-stream disconnect.
    def line_generator():
        yield 'event: message'
        yield 'data: {"id": 1, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": false}'
        yield ''
        raise requests.exceptions.ReadTimeout("read timed out")

    resp = MagicMock()
    resp.status_code = 200
    resp.iter_lines.return_value = line_generator()
    resp.raise_for_status.return_value = None
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False

    emitted = []
    with patch("bridge.requests.get", return_value=resp):
        with patch.dict(bridge.OUTPUT_SINKS, {"stdout": lambda m, room_id, room_label, **_: emitted.append(m["id"])}):
            outcome, duration = bridge.connect_and_stream("https://x/api/v1", "key", "room-1", None, "stdout", {}, bridge.Deduper())
    assert outcome == "dropped"
    assert emitted == [1]


def test_connect_and_stream_uses_a_bounded_read_timeout_for_the_stream_request():
    # An infinite read timeout would let a half-open connection block iter_lines() forever,
    # with the bridge never noticing and never reconnecting.
    lines = ['event: room_expired', 'data: {"roomId": "room-1"}', '']
    with patch("bridge.requests.get", return_value=_mock_stream_response(200, lines)) as mock_get:
        bridge.connect_and_stream("https://x/api/v1", "key", "room-1", None, "stdout", {}, bridge.Deduper())
    stream_call = mock_get.call_args_list[-1]  # backfill call, then the stream call
    assert stream_call.kwargs["timeout"] == (10, 90)


def test_connect_and_stream_does_not_mark_a_message_seen_if_the_sink_raises():
    # If the output sink fails (webhook down, gateway restart), the message must not be marked
    # seen — otherwise the next reconnect's backfill would skip re-attempting it and it would
    # be lost forever. is_new()/mark_seen() being separate operations is what makes this
    # possible: only mark_seen() advances the watermark, and it must only run after a
    # successful sink call.
    lines = [
        'event: message',
        'data: {"id": 1, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": false}', '',
    ]
    dedup = bridge.Deduper(last_id=0)  # seeded, so this isn't a bootstrap call

    def failing_sink(m, room_id, room_label, **_):
        raise RuntimeError("webhook is down")

    with patch("bridge.requests.get", return_value=_mock_stream_response(200, lines)):
        with patch.dict(bridge.OUTPUT_SINKS, {"stdout": failing_sink}):
            with pytest.raises(RuntimeError):
                bridge.connect_and_stream("https://x/api/v1", "key", "room-1", None, "stdout", {}, dedup)
    assert dedup.last_id == 0


def test_connect_and_stream_backfill_replay_stops_at_the_first_sink_failure_without_losing_later_messages():
    # Fix-1 regression check specific to the backfill-replay path: if the sink fails partway
    # through replaying several backfilled messages, only the ones actually delivered get
    # marked seen — the failed one and everything after it remain "new" for the next
    # reconnect's backfill to retry, instead of all of them being lost.
    dedup = bridge.Deduper(last_id=0)
    backfill_body = {"messages": [
        {"id": 1, "authorName": "alice", "message": "one", "createdAt": "t1", "isOwn": False},
        {"id": 2, "authorName": "alice", "message": "two", "createdAt": "t2", "isOwn": False},
        {"id": 3, "authorName": "alice", "message": "three", "createdAt": "t3", "isOwn": False},
    ]}
    emitted = []

    def sink(m, room_id, room_label, **_):
        if m["id"] == 2:
            raise RuntimeError("webhook is down")
        emitted.append(m["id"])

    with patch("bridge.requests.get", return_value=_mock_json_response(200, backfill_body)):
        with patch.dict(bridge.OUTPUT_SINKS, {"stdout": sink}):
            with pytest.raises(RuntimeError):
                bridge.connect_and_stream("https://x/api/v1", "key", "room-1", None, "stdout", {}, dedup)
    assert emitted == [1]
    assert dedup.last_id == 1


def test_emit_webhook_posts_the_formatted_body_with_custom_headers():
    message = {"id": 1, "authorName": "alice", "message": "hi", "createdAt": "t1", "isOwn": False}
    with patch("bridge.requests.post", return_value=_mock_json_response(200, {})) as mock_post:
        bridge.emit_webhook(
            message, room_id="room-1", room_label="test",
            webhook_url="https://example.com/hook", webhook_headers={"X-Api-Key": "secret"},
        )
    mock_post.assert_called_once()
    assert mock_post.call_args.args[0] == "https://example.com/hook"
    assert mock_post.call_args.kwargs["headers"] == {"X-Api-Key": "secret"}
    assert mock_post.call_args.kwargs["json"] == {
        "id": 1, "author": "alice", "message": "hi",
        "room_id": "room-1", "room_label": "test", "created_at": "t1",
    }


# ---------------------------------------------------------------------------
# run_room()
# ---------------------------------------------------------------------------

def test_run_room_records_exit_code_1_on_auth_error_and_does_not_retry():
    exit_codes = {}
    with patch("bridge.connect_and_stream", side_effect=bridge.AuthError(401)):
        bridge.run_room("https://x/api/v1", "key", "room-1", None, "stdout", {}, None, exit_codes)
    assert exit_codes == {"room-1": 1}


def test_run_room_records_exit_code_0_on_room_expired():
    exit_codes = {}
    with patch("bridge.connect_and_stream", return_value=("expired", 10.0)):
        bridge.run_room("https://x/api/v1", "key", "room-1", None, "stdout", {}, None, exit_codes)
    assert exit_codes == {"room-1": 0}


def test_run_room_retries_on_dropped_then_succeeds_and_resets_backoff_after_a_healthy_connection():
    exit_codes = {}
    call_count = {"n": 0}

    def fake_connect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "dropped", 10.0  # stayed up well past MIN_HEALTHY_CONNECTION_SECONDS
        return "expired", 10.0

    with patch("bridge.connect_and_stream", side_effect=fake_connect), \
         patch("bridge._shutdown") as mock_shutdown:
        mock_shutdown.is_set.return_value = False
        mock_shutdown.wait.return_value = False
        bridge.run_room("https://x/api/v1", "key", "room-1", None, "stdout", {}, None, exit_codes)

    assert call_count["n"] == 2
    assert exit_codes == {"room-1": 0}
    mock_shutdown.wait.assert_called_once_with(bridge.MIN_BACKOFF_SECONDS)


def test_run_room_does_not_reset_backoff_after_a_connection_that_barely_stayed_up():
    # A connection that gets a 200 and then immediately drops (e.g. an instant OVERFLOW) is not
    # evidence the server is healthy — resetting backoff to 1s for it risks a reconnect storm.
    exit_codes = {}
    call_count = {"n": 0}

    def fake_connect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "dropped", 0.1  # well under MIN_HEALTHY_CONNECTION_SECONDS
        return "expired", 10.0

    with patch("bridge.connect_and_stream", side_effect=fake_connect), \
         patch("bridge._shutdown") as mock_shutdown:
        mock_shutdown.is_set.return_value = False
        mock_shutdown.wait.return_value = False
        bridge.run_room("https://x/api/v1", "key", "room-1", None, "stdout", {}, None, exit_codes)

    assert call_count["n"] == 2
    assert exit_codes == {"room-1": 0}
    # Backoff grew instead of resetting: next_backoff(MIN_BACKOFF_SECONDS), not MIN_BACKOFF_SECONDS.
    mock_shutdown.wait.assert_called_once_with(bridge.next_backoff(bridge.MIN_BACKOFF_SECONDS))


def test_run_room_records_exit_code_2_when_shutdown_arrives_during_a_backoff_wait():
    # A connection attempt failed outright (RequestException, e.g. never even connected) and,
    # while waiting out the backoff before retrying, shutdown is signaled. This must leave the
    # room's exit code at 2 (a retry was in flight and got interrupted) — NOT get overwritten
    # to 0, since that would misrepresent a genuinely-interrupted retry as a clean outcome.
    exit_codes = {}
    with patch("bridge.connect_and_stream", side_effect=requests.ConnectionError("boom")), \
         patch("bridge._shutdown") as mock_shutdown:
        mock_shutdown.is_set.return_value = False
        mock_shutdown.wait.return_value = True
        bridge.run_room("https://x/api/v1", "key", "room-1", None, "stdout", {}, None, exit_codes)
    assert exit_codes == {"room-1": 2}


def test_run_room_records_exit_code_2_when_shutdown_arrives_during_a_post_drop_backoff_wait():
    # Same principle as above, but via the "dropped" outcome branch (a connection was
    # established, then lost) rather than a RequestException from connect_and_stream itself.
    exit_codes = {}
    with patch("bridge.connect_and_stream", return_value=("dropped", 0.0)), \
         patch("bridge._shutdown") as mock_shutdown:
        mock_shutdown.is_set.return_value = False
        mock_shutdown.wait.return_value = True
        bridge.run_room("https://x/api/v1", "key", "room-1", None, "stdout", {}, None, exit_codes)
    assert exit_codes == {"room-1": 2}


def test_run_room_defaults_to_exit_code_0_when_shutdown_is_already_set_before_any_attempt():
    # If _shutdown is already set at the top-of-loop check, the loop body never runs and no
    # terminal `return` sets exit_codes[room_id] — run_room must still leave a 0 behind.
    exit_codes = {}
    with patch("bridge.connect_and_stream") as mock_connect, \
         patch("bridge._shutdown") as mock_shutdown:
        mock_shutdown.is_set.return_value = True
        bridge.run_room("https://x/api/v1", "key", "room-1", None, "stdout", {}, None, exit_codes)
    mock_connect.assert_not_called()
    assert exit_codes == {"room-1": 0}


def test_run_room_handles_an_unexpected_exception_without_crashing_and_marks_exit_code_2():
    # Anything other than AuthError/requests.RequestException (a malformed SSE payload causing
    # json.JSONDecodeError, an unexpected message shape causing KeyError, etc.) must not
    # silently kill this thread and leave exit_codes[room_id] undefined — that would make
    # main() report a clean exit 0 despite a real crash.
    exit_codes = {}
    with patch("bridge.connect_and_stream", side_effect=KeyError("isOwn")), \
         patch("bridge._shutdown") as mock_shutdown:
        mock_shutdown.is_set.return_value = False
        mock_shutdown.wait.return_value = True
        bridge.run_room("https://x/api/v1", "key", "room-1", None, "stdout", {}, None, exit_codes)
    assert exit_codes == {"room-1": 2}


def test_run_room_finally_block_never_overwrites_a_code_a_normal_return_already_set():
    # The outer try/finally's setdefault(..., 2) is a last-resort safety net and must be a
    # no-op whenever a normal path (here: room_expired) already recorded the real outcome.
    exit_codes = {}
    with patch("bridge.connect_and_stream", return_value=("expired", 10.0)):
        bridge.run_room("https://x/api/v1", "key", "room-1", None, "stdout", {}, None, exit_codes)
    assert exit_codes == {"room-1": 0}


# ---------------------------------------------------------------------------
# Multi-room independence
# ---------------------------------------------------------------------------

def test_two_rooms_run_independently_one_expiring_does_not_stop_the_other():
    exit_codes = {}
    room_a_started = threading.Event()

    def fake_connect(base_url, access_key, room_id, room_label, mode, sink_kwargs, dedup):
        if room_id == "room-a":
            room_a_started.set()
            return "expired", 10.0
        room_a_started.wait(timeout=5)
        return "expired", 10.0

    with patch("bridge.connect_and_stream", side_effect=fake_connect):
        t1 = threading.Thread(target=bridge.run_room, args=("https://x/api/v1", "key", "room-a", "a", "stdout", {}, None, exit_codes))
        t2 = threading.Thread(target=bridge.run_room, args=("https://x/api/v1", "key", "room-b", "b", "stdout", {}, None, exit_codes))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

    assert exit_codes == {"room-a": 0, "room-b": 0}


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

def test_parse_args_requires_access_key():
    with pytest.raises(SystemExit):
        bridge.parse_args(["--room-id", "room-1"])


def test_parse_args_requires_exactly_one_of_room_id_or_config():
    with pytest.raises(SystemExit):
        bridge.parse_args(["--access-key", "k"])
    with pytest.raises(SystemExit):
        bridge.parse_args(["--access-key", "k", "--room-id", "r", "--config", "rooms.yaml"])


def test_parse_args_requires_webhook_url_for_webhook_mode():
    with pytest.raises(SystemExit):
        bridge.parse_args(["--access-key", "k", "--room-id", "r", "--mode", "webhook"])


def test_parse_args_accepts_a_valid_single_room_invocation():
    args = bridge.parse_args(["--access-key", "k", "--room-id", "r"])
    assert args.access_key == "k"
    assert args.room_id == "r"
    assert args.mode == "stdout"
    assert args.base_url == bridge.DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# main() exit code aggregation
# ---------------------------------------------------------------------------

def test_main_returns_1_when_any_room_has_an_auth_failure_even_if_others_are_retrying():
    def fake_run_room(base_url, access_key, room_id, room_label, mode, sink_kwargs, last_id, exit_codes):
        exit_codes[room_id] = 1 if room_id == "room-a" else 2

    with patch("bridge.load_rooms_config", return_value=[{"id": "room-a"}, {"id": "room-b"}]), \
         patch("bridge.run_room", side_effect=fake_run_room):
        code = bridge.main(["--access-key", "k", "--config", "rooms.yaml"])
    assert code == 1


def test_main_returns_2_when_all_rooms_are_retrying_at_shutdown_and_none_have_auth_failures():
    def fake_run_room(base_url, access_key, room_id, room_label, mode, sink_kwargs, last_id, exit_codes):
        exit_codes[room_id] = 2

    with patch("bridge.load_rooms_config", return_value=[{"id": "room-a"}, {"id": "room-b"}]), \
         patch("bridge.run_room", side_effect=fake_run_room):
        code = bridge.main(["--access-key", "k", "--config", "rooms.yaml"])
    assert code == 2


def test_main_returns_0_when_every_room_exits_cleanly():
    def fake_run_room(base_url, access_key, room_id, room_label, mode, sink_kwargs, last_id, exit_codes):
        exit_codes[room_id] = 0

    with patch("bridge.load_rooms_config", return_value=[{"id": "room-a"}, {"id": "room-b"}]), \
         patch("bridge.run_room", side_effect=fake_run_room):
        code = bridge.main(["--access-key", "k", "--config", "rooms.yaml"])
    assert code == 0
