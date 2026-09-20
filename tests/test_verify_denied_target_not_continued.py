"""The post-restart proof for efae654 must say the right thing in each of its three
states, from the journal alone."""
from tools import verify_denied_target_not_continued as vd


def test_no_decision_yet_is_not_evidence():
    r = vd.judge([])
    assert r["status"] == "not_yet_observable" and r["exit"] == 2
    # gate-cooldown skips are what the pre-fix code also produces — they prove nothing
    r = vd.judge([(10, "event_path", "skip", "continuation_gate_open"),
                  (20, "event_path", "skip", "cap_reached_but_recent_intentional_external_wait")])
    assert r["status"] == "not_yet_observable"


def test_the_terminal_skip_first_and_no_continue_confirms():
    r = vd.judge([(5, "event_path", "skip", "continuation_gate_open"),
                  (30, "idle_sweep", "skip", "value_bearing_send_blocked"),
                  (40, "event_path", "skip", "value_bearing_send_blocked")])
    assert r["status"] == "confirmed" and r["exit"] == 0


def test_any_continue_contradicts_even_after_a_correct_skip():
    r = vd.judge([(30, "event_path", "skip", "value_bearing_send_blocked"),
                  (90, "idle_sweep", "continue", "continued_same_agent_idle_sweep")])
    assert r["status"] == "contradicted" and r["exit"] == 3
    assert "idle_sweep" in r["detail"]


def test_a_different_first_decision_contradicts():
    # e.g. the pre-fix predicate letting it through to a transient at-rest check
    r = vd.judge([(30, "event_path", "skip", "not_registered")])
    assert r["status"] == "contradicted"


def test_order_is_by_time_not_by_table():
    # idle-sweep row is earlier in time even though it is listed second
    r = vd.judge([(50, "event_path", "skip", "value_bearing_send_blocked"),
                  (20, "idle_sweep", "continue", "continued_same_agent_idle_sweep")])
    assert r["status"] == "contradicted"
