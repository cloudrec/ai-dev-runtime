from core import notify_format


FULL = {
    "event_id": "evt-1",
    "type": "runtime.job.completed",
    "payload": {
        "job_id": 4242,
        "task_title": "Fix live Telegram completion messages",
        "project": "ai-dev-runtime",
        "branch": "ai-runtime/109-telegram",
        "commit": "abcdef1234567890abcdef",
        "tests_result": "passed (37 tests)",
        "duration_seconds": 125,
        "next_action": "review PR #109",
    },
}


def test_completed_message_contains_all_known_fields():
    text = notify_format.render_event(FULL)
    assert "Fix live Telegram completion messages" in text
    assert "Runtime job: 4242" in text
    assert "Project: ai-dev-runtime" in text
    assert "Branch: ai-runtime/109-telegram" in text
    assert "Commit: abcdef123456" in text
    assert "Tests: passed (37 tests)" in text
    assert "Duration: 2m 5s" in text
    assert "Next action: review PR #109" in text


def test_missing_fields_are_omitted_not_none():
    event = {"event_id": "e", "type": "runtime.job.completed", "payload": {"job_id": 7}}
    text = notify_format.render_event(event)
    assert "None" not in text
    assert "Runtime job: 7" in text
    assert "Next action" not in text


def test_two_distinct_completions_render_distinct_text():
    a = notify_format.render_event(FULL)
    other = {
        "event_id": "evt-2",
        "type": "runtime.job.completed",
        "payload": dict(FULL["payload"], job_id=4243, task_title="Second canary"),
    }
    b = notify_format.render_event(other)
    assert a != b
    assert "Second canary" in b


def test_other_event_types_pass_through_unchanged():
    warning = {"event_id": "w1", "type": "runtime.warning", "text": "disk almost full"}
    assert notify_format.render_event(warning) == "disk almost full"
    decision = {
        "event_id": "d1",
        "type": "runtime.decision.required",
        "payload": {"message": "approve deploy?"},
    }
    assert notify_format.render_event(decision) == "approve deploy?"


def test_event_without_text_renders_nothing():
    assert notify_format.render_event({"event_id": "x", "type": "runtime.tick"}) is None


def test_duration_accepts_preformatted_string_and_hours():
    assert notify_format._format_duration("3 minutes") == "3 minutes"
    assert notify_format._format_duration(3725) == "1h 2m 5s"
