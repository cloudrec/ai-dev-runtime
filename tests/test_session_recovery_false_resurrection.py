"""A finished session must stay finished, and a session must never come up in /root.

Live 2026-08-06/07, `mess-qa-automation:0.0` was resurrected five times across nine hours.
Each time it came up as a live Claude in `/root`, resuming the stale conversation
`406eab3c-…`, sitting on the folder-trust prompt, with no open ledger task behind it.

Four independent defects had to line up, and each is pinned here:

1. `config/managed_sessions.yaml` said `cwd: /opt/mess-qa-automation`; the project config
   (`project_queues.yaml`, the authority) said `/opt/mess`. Nothing reconciled them.
2. `tmux new-session -c <missing dir>` does NOT fail. It returns rc=0 and starts the pane
   in the server's default directory — `/root`. Verified against real tmux.
3. A dead pane was treated as sufficient reason to revive. The work had finished.
4. `recent_recoveries` counted only `ok=1`, so five `verify_failed` revivals were invisible
   to the crash-loop cap and neither backoff nor quarantine ever engaged.

And the consequence nobody caught: a recovery that failed verification left its wrongly
placed pane running, which the next discovery pass recorded as a real live agent.
"""
from __future__ import annotations

import pytest

from core import session_recovery as sr

MESS = "mess-qa-automation:0.0"
STALE_CONV = "406eab3c-66f4-4a35-b1cf-4a0f657480fc"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    # Hermetic: without this the helper reads the REAL project config and every temp
    # directory below is overridden by the live /opt/mess. (That precedence is the fix and
    # is asserted directly in the governed-wins test and against the real config at the
    # bottom of this file.)
    import core.continuation_governor as cg
    global _REAL_LOAD_CONFIG
    _REAL_LOAD_CONFIG = cg.load_config
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: {})
    yield


_REAL_LOAD_CONFIG = None


@pytest.fixture()
def real_project_config(monkeypatch):
    """Undo the hermetic stub: these tests check the ACTUAL shipped configuration."""
    import core.continuation_governor as cg
    monkeypatch.setattr(cg, "load_config", _REAL_LOAD_CONFIG)
    return _REAL_LOAD_CONFIG


@pytest.fixture()
def project(tmp_path):
    d = tmp_path / "opt" / "mess"
    d.mkdir(parents=True)
    return str(d)


def _registry(cwd, target=MESS, session="mess-qa-automation", enabled=True):
    return {"sessions": {target: {"target": target, "session": session, "cwd": cwd,
                                  "conversation_id": STALE_CONV,
                                  "resume_shape": "claude --resume {conversation_id}",
                                  "enabled": enabled}},
            "limits": {"max_recoveries_per_target": 3, "window_secs": 21600,
                       "backoff_base_secs": 0}}


class _Tmux:
    """Records tmux argv instead of running it."""

    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        return (0, "", "")

    def started_in(self):
        for c in self.calls:
            if c and c[0] in ("new-session", "respawn-pane") and "-c" in c:
                return c[c.index("-c") + 1]
        return None

    def killed(self):
        return any(c and c[0] == "kill-session" for c in self.calls)


@pytest.fixture()
def dead_pane(monkeypatch):
    monkeypatch.setattr(sr, "pane_state", lambda t: {"missing": True, "dead": True})
    monkeypatch.setattr(sr, "_capture", lambda t, n=30: "")
    monkeypatch.setattr(sr, "panes", lambda: [])


def _no_work(monkeypatch):
    monkeypatch.setattr(sr, "has_authoritative_work",
                        lambda t: {"open": False, "task_id": "", "reason": "no_active_task"})


def _open_work(monkeypatch):
    monkeypatch.setattr(sr, "has_authoritative_work",
                        lambda t: {"open": True, "task_id": "task-9", "reason": "active_task"})


# ── 1. completed work is not reopened ────────────────────────────────────────
def test_a_completed_task_is_not_resurrected(project, dead_pane, monkeypatch):
    """THE incident: the pane was dead because the work was DONE."""
    _no_work(monkeypatch)
    tm = _Tmux()
    res = sr.recover(MESS, registry=_registry(project), run_fn=tm, sleep=lambda s: None)
    assert res["recovered"] is False
    assert res["reason"] == "no_open_work"
    assert tm.calls == [], "nothing may be started for work that finished"


def test_an_open_ledger_task_still_allows_recovery(project, dead_pane, monkeypatch):
    _open_work(monkeypatch)
    monkeypatch.setattr(sr, "verify_recovered",
                        lambda t, c: {"ok": True, "checks": {"cwd_matches": True}, "pid": 1})
    monkeypatch.setattr(sr, "choose_summary_if_offered", lambda t: {"offered": False})
    tm = _Tmux()
    res = sr.recover(MESS, registry=_registry(project), run_fn=tm, sleep=lambda s: None)
    assert res["recovered"] is True
    assert tm.started_in() == project


def test_an_unreadable_ledger_fails_closed(project, dead_pane, monkeypatch):
    """An unknown ledger is not a licence to restart."""
    monkeypatch.setattr(sr, "has_authoritative_work",
                        lambda t: {"open": False, "task_id": "", "reason": "ledger_unavailable"})
    tm = _Tmux()
    res = sr.recover(MESS, registry=_registry(project), run_fn=tm, sleep=lambda s: None)
    assert res["recovered"] is False and res["reason"] == "no_open_work"
    assert tm.calls == []


# ── 2/3. the project directory is authoritative ──────────────────────────────
def test_the_project_config_wins_over_a_diverged_registry(project, dead_pane, monkeypatch):
    """Exactly the live drift: registry /opt/mess-qa-automation vs project /opt/mess."""
    monkeypatch.setattr(sr, "authoritative_cwd", lambda t, e=None: {
        "cwd": project, "governed": project, "registry": "/opt/mess-qa-automation",
        "diverged": True, "exists": True})
    _open_work(monkeypatch)
    monkeypatch.setattr(sr, "verify_recovered",
                        lambda t, c: {"ok": True, "checks": {"cwd_matches": True}, "pid": 1})
    monkeypatch.setattr(sr, "choose_summary_if_offered", lambda t: {"offered": False})
    tm = _Tmux()
    sr.recover(MESS, registry=_registry("/opt/mess-qa-automation"), run_fn=tm,
               sleep=lambda s: None)
    assert tm.started_in() == project, "the governed project dir must win"


def test_a_stale_conversation_cannot_redirect_the_project_dir(project, dead_pane,
                                                              monkeypatch):
    """The conversation id only selects what to resume — never WHERE."""
    _open_work(monkeypatch)
    monkeypatch.setattr(sr, "verify_recovered",
                        lambda t, c: {"ok": True, "checks": {"cwd_matches": True}, "pid": 1})
    monkeypatch.setattr(sr, "choose_summary_if_offered", lambda t: {"offered": False})
    tm = _Tmux()
    sr.recover(MESS, registry=_registry(project), run_fn=tm, sleep=lambda s: None)
    started = [c for c in tm.calls if c and c[0] in ("new-session", "respawn-pane")][0]
    assert STALE_CONV in " ".join(started), "the approved conversation is still resumed"
    assert tm.started_in() == project, "but it does not decide the directory"


def test_the_resolved_project_dir_is_read_fresh_each_time(project):
    """Nothing caches the path in memory, so a restart cannot lose it."""
    entry = {"cwd": "/opt/mess-qa-automation"}
    for _ in range(3):
        loc = sr.authoritative_cwd("nonexistent-target:0.0", entry)
        assert loc["cwd"] == "/opt/mess-qa-automation"
        assert loc["exists"] is False


# ── 4. a missing directory fails closed, never /root ─────────────────────────
def test_a_missing_project_dir_refuses_instead_of_falling_back_to_root(dead_pane,
                                                                      monkeypatch):
    """`tmux -c <missing>` returns rc=0 and lands in /root. So we must never call it."""
    _open_work(monkeypatch)
    tm = _Tmux()
    res = sr.recover(MESS, registry=_registry("/opt/mess-qa-automation-does-not-exist"),
                     run_fn=tm, sleep=lambda s: None)
    assert res["recovered"] is False
    assert res["reason"] == "project_dir_missing"
    assert res["owner_blocker"] is True
    assert tm.calls == [], "no tmux call may be made with an unusable directory"


def test_an_empty_project_dir_refuses(dead_pane, monkeypatch):
    _open_work(monkeypatch)
    monkeypatch.setattr(sr, "authoritative_cwd", lambda t, e=None: {
        "cwd": "", "governed": "", "registry": "", "diverged": False, "exists": False})
    tm = _Tmux()
    res = sr.recover(MESS, registry=_registry(""), run_fn=tm, sleep=lambda s: None)
    assert res["reason"] == "no_project_dir"
    assert tm.calls == []


def test_recovery_never_starts_a_session_in_root(project, dead_pane, monkeypatch):
    """The blunt invariant, stated once: /root is never a project directory."""
    _open_work(monkeypatch)
    monkeypatch.setattr(sr, "verify_recovered",
                        lambda t, c: {"ok": True, "checks": {"cwd_matches": True}, "pid": 1})
    monkeypatch.setattr(sr, "choose_summary_if_offered", lambda t: {"offered": False})
    for cwd in (project, "/opt/mess-qa-automation-does-not-exist", ""):
        tm = _Tmux()
        sr.recover(MESS, registry=_registry(cwd), run_fn=tm, sleep=lambda s: None)
        assert tm.started_in() != "/root"


# ── 5. duplicates ────────────────────────────────────────────────────────────
def test_an_existing_correct_live_agent_prevents_a_duplicate(project, monkeypatch):
    _open_work(monkeypatch)
    monkeypatch.setattr(sr, "pane_state", lambda t: {"missing": True, "dead": True})
    monkeypatch.setattr(sr, "_capture", lambda t, n=30: "")
    monkeypatch.setattr(sr, "panes", lambda: [
        {"target": "mess-other:0.0", "cwd": project, "cmd": "claude", "pid": 1,
         "dead": False}])
    tm = _Tmux()
    res = sr.recover(MESS, registry=_registry(project), run_fn=tm, sleep=lambda s: None)
    assert res["recovered"] is False
    assert res["reason"] == "live_claude_exists_for_cwd"
    assert tm.calls == []


# ── 6. the owner's own resume still works ────────────────────────────────────
def test_explicit_resume_with_a_project_dir_still_works(project, dead_pane, monkeypatch):
    """The owner asking IS the reason; no open ledger task is required."""
    _no_work(monkeypatch)
    monkeypatch.setattr(sr, "verify_recovered",
                        lambda t, c: {"ok": True, "checks": {"cwd_matches": True}, "pid": 1})
    monkeypatch.setattr(sr, "choose_summary_if_offered", lambda t: {"offered": False})
    tm = _Tmux()
    res = sr.recover(MESS, registry=_registry(project), run_fn=tm, sleep=lambda s: None,
                     explicit=True)
    assert res["recovered"] is True
    assert tm.started_in() == project


def test_explicit_resume_still_refuses_a_missing_project_dir(dead_pane, monkeypatch):
    """Explicit is a reason to act, not a reason to act wrongly."""
    _no_work(monkeypatch)
    tm = _Tmux()
    res = sr.recover(MESS, registry=_registry("/opt/nope-does-not-exist"), run_fn=tm,
                     sleep=lambda s: None, explicit=True)
    assert res["reason"] == "project_dir_missing"
    assert tm.calls == []


# ── the zombie, and the cap that never fired ─────────────────────────────────
def test_a_wrongly_placed_pane_is_torn_down(project, dead_pane, monkeypatch):
    """A recovery that cannot prove itself must not leave a live Claude behind."""
    _open_work(monkeypatch)
    monkeypatch.setattr(sr, "verify_recovered",
                        lambda t, c: {"ok": False, "checks": {"cwd_matches": False}, "pid": 2})
    monkeypatch.setattr(sr, "choose_summary_if_offered", lambda t: {"offered": False})
    tm = _Tmux()
    res = sr.recover(MESS, registry=_registry(project), run_fn=tm, sleep=lambda s: None)
    assert res["recovered"] is False
    assert res["torn_down"] is True
    assert tm.killed(), "the pane we started in the wrong place must be killed"


def test_failed_revivals_count_toward_the_crash_loop_cap(project, dead_pane, monkeypatch):
    """Five verify_failed revivals must exhaust the cap, not run forever."""
    _open_work(monkeypatch)
    monkeypatch.setattr(sr, "verify_recovered",
                        lambda t, c: {"ok": False, "checks": {"cwd_matches": True}, "pid": 2})
    monkeypatch.setattr(sr, "choose_summary_if_offered", lambda t: {"offered": False})
    reasons = []
    for _ in range(5):
        tm = _Tmux()
        reasons.append(sr.recover(MESS, registry=_registry(project), run_fn=tm,
                                  sleep=lambda s: None)["reason"])
    assert "quarantined_crash_loop" in reasons, reasons


# ── neighbouring projects ────────────────────────────────────────────────────
def test_the_real_registry_agrees_with_the_project_config_for_every_target(
        real_project_config):
    """The drift that caused this must not exist for any other project either."""
    reg = sr.load_registry()
    governed = real_project_config() or {}
    bad = []
    for target, entry in (reg.get("sessions") or {}).items():
        loc = sr.authoritative_cwd(target, entry)
        if target in governed and loc["diverged"]:
            bad.append((target, loc["governed"], loc["registry"]))
    assert not bad, f"registry/project cwd divergence: {bad}"


def test_every_registered_project_dir_exists(real_project_config):
    reg = sr.load_registry()
    missing = [(t, sr.authoritative_cwd(t, e)["cwd"])
               for t, e in (reg.get("sessions") or {}).items()
               if not sr.authoritative_cwd(t, e)["exists"]]
    assert not missing, f"registered project dirs that do not exist: {missing}"


# ── quarantine must be releasable ────────────────────────────────────────────
# recover() writes a quarantine after a crash loop, and until 2026-08-30 NO code
# path anywhere removed it. A quarantined session was therefore dead permanently:
# cp-canary:0.0 — this project's own disposable canary, the target safe end-to-end
# tests are supposed to run on — had been unrecoverable since 2026-08-07 for
# "crash loop: 3 recoveries within 21600s". A safety brake with no release is a
# broken brake, and it is what blocked the P0 wake acceptance canaries.

_CANARY = "cp-canary:0.0"
_CANARY_REG = {"sessions": [{"target": _CANARY, "session": "cp-canary",
                             "cwd": "/tmp/cp-canary", "conversation_id": "c1",
                             "enabled": True}]}


def _quarantine(target, reason="crash loop: 3 recoveries within 21600s"):
    c = sr._db()
    c.execute("INSERT OR REPLACE INTO session_quarantine VALUES (?,?,?)",
              (target, "2026-08-07T03:26:39+00:00", reason))
    c.commit()
    c.close()


def test_a_quarantined_registered_session_can_be_released():
    _quarantine(_CANARY)
    assert sr.is_quarantined(_CANARY) is not None
    r = sr.release_quarantine(_CANARY, reason="e2e canary", registry=_CANARY_REG)
    assert r["released"] is True
    assert "crash loop" in r["was_reason"]
    assert sr.is_quarantined(_CANARY) is None


def test_an_unregistered_target_can_never_be_released():
    """The registry is the authority: payment is absent from it and must stay absent."""
    _quarantine("payment:0.0")
    r = sr.release_quarantine("payment:0.0", registry=_CANARY_REG)
    assert r["released"] is False and r["reason"] == "not_in_registry"
    assert sr.is_quarantined("payment:0.0") is not None, "must remain quarantined"


def test_releasing_a_target_that_is_not_quarantined_is_a_noop():
    r = sr.release_quarantine(_CANARY, registry=_CANARY_REG)
    assert r["released"] is False and r["reason"] == "not_quarantined"


def test_the_release_is_audited_like_every_other_recovery_decision():
    _quarantine(_CANARY)
    sr.release_quarantine(_CANARY, reason="e2e canary", registry=_CANARY_REG)
    c = sr._db()
    row = c.execute("SELECT action,ok FROM session_recovery WHERE target=? "
                    "ORDER BY rowid DESC LIMIT 1", (_CANARY,)).fetchone()
    c.close()
    assert row and row[0] == "release_quarantine" and row[1] == 1
