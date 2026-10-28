"""One tab per conversation, and no tab replaced under a turn that is merely long.

2026-10-28, owner-os route (…de5162d4ac17): every failure stretch after the deploy began
when the single shared tab was navigated back to that chat while ChatGPT was still
answering there (09:45, 10:20, 10:44Z), and at 10:47:47Z a live turn was declared
`assistant_generating_wedged` and its tab replaced — 22 s before ChatGPT, from that very
chat, sent its next command. The fresh tab rendered "Could not load this ChatGPT
conversation", which reads as `composer_ambiguous_or_absent:0`, which replaced the tab
again.

Loads the composer from THIS checkout (the older suites hard-code the live path).
"""
from __future__ import annotations

import importlib.util
import os

import pytest

from core import wake_bridge as wb

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "cdp_composer_tabs", os.path.join(_HERE, "tools", "cdp_composer.py"))
cdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cdp)

OWNER_OS = "https://chatgpt.com/c/owner-os-chat"
PL = "https://chatgpt.com/c/gaika-pl-chat"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(cdp, "ASSISTANT_SETTLE_SECS", 0)
    monkeypatch.setattr(cdp, "_GENERATING_SINCE", {})
    monkeypatch.setattr(cdp, "browser_degraded", lambda *a, **k: {"degraded": False})
    monkeypatch.setattr(cdp, "page_responsive", lambda t, timeout=8.0: True)
    yield


def _pages(*urls):
    return [{"type": "page", "id": f"T{i}", "url": u, "webSocketDebuggerUrl": f"ws://{i}"}
            for i, u in enumerate(urls)]


# ── which tab may be pointed at a conversation that has none ──────────────────────────

def test_a_root_page_is_reclaimed_first(monkeypatch):
    pages = _pages(OWNER_OS, "https://chatgpt.com/")
    monkeypatch.setattr(cdp, "_http", lambda path, **k: pages)
    monkeypatch.setattr(cdp, "_page_generating", lambda t: pytest.fail("not asked"))
    assert cdp.find_chatgpt_page(PL)["id"] == "T1"


def test_under_the_cap_another_conversation_is_never_navigated_away(monkeypatch):
    """The defect: the owner-os tab was navigated to the PL chat mid-answer."""
    monkeypatch.setattr(cdp, "_http", lambda path, **k: _pages(OWNER_OS))
    monkeypatch.setattr(cdp, "CONVERSATION_TABS", 6)
    assert cdp.find_chatgpt_page(PL) is None, "None = open a tab of its own"


def test_at_the_cap_only_a_provably_idle_tab_is_reused(monkeypatch):
    pages = _pages(OWNER_OS, "https://chatgpt.com/c/a", "https://chatgpt.com/c/b")
    monkeypatch.setattr(cdp, "_http", lambda path, **k: pages)
    monkeypatch.setattr(cdp, "CONVERSATION_TABS", 3)
    state = {"T0": True, "T1": None, "T2": False}      # answering, cannot say, idle
    monkeypatch.setattr(cdp, "_page_generating", lambda t: state[t["id"]])
    assert cdp.find_chatgpt_page(PL)["id"] == "T2"


def test_at_the_cap_with_every_tab_busy_nothing_is_navigated(monkeypatch):
    pages = _pages(OWNER_OS, "https://chatgpt.com/c/a")
    monkeypatch.setattr(cdp, "_http", lambda path, **k: pages)
    monkeypatch.setattr(cdp, "CONVERSATION_TABS", 2)
    monkeypatch.setattr(cdp, "_page_generating", lambda t: None if t["id"] == "T1" else True)
    assert cdp.find_chatgpt_page(PL) is cdp.ALL_TABS_BUSY


def test_the_sidebar_scan_still_takes_any_page(monkeypatch):
    monkeypatch.setattr(cdp, "_http", lambda path, **k: _pages(OWNER_OS))
    assert cdp.find_chatgpt_page()["id"] == "T0"


def test_the_cap_stays_under_the_degradation_limit():
    assert cdp.CONVERSATION_TABS <= cdp.BROWSER_MAX_PAGES - 2


def test_all_tabs_busy_is_refused_before_anything_is_typed(monkeypatch):
    monkeypatch.setattr(cdp, "find_target", lambda url: None)
    monkeypatch.setattr(cdp, "find_chatgpt_page", lambda *a, **k: cdp.ALL_TABS_BUSY)
    monkeypatch.setattr(cdp, "_Session", lambda ws: pytest.fail("no session may open"))
    wb.bind_chat(PL)
    d = wb.should_wake(event_id=401, severity="critical")
    wb.record(d, event_id=401, severity="critical")
    r = cdp.submit_phrase(PL, "P", source="companion", event_id=401)
    assert r == {"ok": False, "reason": "all_conversation_tabs_busy"}
    assert wb.was_submitted(401) is False, "not latched: the wake is retried"


def test_a_conversation_without_a_tab_gets_its_own(monkeypatch):
    opened, navigated = [], []
    monkeypatch.setattr(cdp, "find_target", lambda url: None)
    monkeypatch.setattr(cdp, "find_chatgpt_page", lambda *a, **k: None)
    monkeypatch.setattr(cdp, "open_chatgpt_page", lambda url: opened.append(url) or None)
    monkeypatch.setattr(cdp, "_Session",
                        lambda ws: navigated.append(ws) or pytest.fail("nothing navigated"))
    cdp.submit_phrase(PL, "P", claim=False)
    assert opened == [PL] and navigated == []


# ── a long turn is back-pressure; only a long UNCHANGING one is wedged ────────────────

class Busy:
    """A page with ChatGPT mid-turn: stop control up, composer present and empty."""

    def __init__(self, text_lens):
        self.text_lens = list(text_lens)
        self.inserted = []

    def call(self, method, params=None):
        if method == "Input.insertText":
            self.inserted.append((params or {}).get("text"))
        expr = (params or {}).get("expression", "")
        if "innerText.length" in expr:
            v = self.text_lens.pop(0) if len(self.text_lens) > 1 else self.text_lens[0]
            return {"result": {"value": v}}
        return {}

    def boolean(self, e):
        if "readyState" in (e or "") or "__owComposer" in (e or ""):
            return True
        if cdp._STOP_BUTTON_SEL in (e or ""):
            return True
        if "activeElement" in (e or ""):
            return True
        return False

    def count(self, sel):
        return 1 if sel == cdp.COMPOSER_SEL else 3

    def last_attr(self, sel, attr):
        return ""

    def close(self):
        pass


def _wire(monkeypatch, page, recovered):
    monkeypatch.setattr(cdp, "find_target", lambda url: {"id": "T", "webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(cdp, "_Session", lambda ws: page)
    monkeypatch.setattr(cdp, "composer_is_stable", lambda s: True)
    monkeypatch.setattr(cdp, "focus_composer", lambda s: True)
    monkeypatch.setattr(cdp, "generating_is_wedged", lambda s, **k: True)  # the 4 s sample
    monkeypatch.setattr(cdp, "recover_wedged_tab",
                        lambda t, u: recovered.append(u) or None)


def test_a_turn_seen_generating_for_the_first_time_is_never_wedged(monkeypatch):
    recovered = []
    page = Busy([5000])
    _wire(monkeypatch, page, recovered)
    r = cdp.submit_phrase(OWNER_OS, "P", claim=False)
    assert r["reason"] == "assistant_still_generating"
    assert recovered == [] and page.inserted == []


def test_an_unchanged_turn_past_the_threshold_is_recovered_once(monkeypatch):
    recovered = []
    _wire(monkeypatch, Busy([5000]), recovered)
    t = [1000.0]
    monkeypatch.setattr(cdp.time, "time", lambda: t[0])
    # These steps jump a whole threshold at once; live retries come every few minutes.
    monkeypatch.setattr(cdp, "WEDGE_GAP_SECS", 10 * cdp.WEDGE_CEILING_SECS)
    cdp.submit_phrase(OWNER_OS, "P", claim=False)          # first seen
    t[0] += cdp.WEDGE_MIN_SECS + 1
    r = cdp.submit_phrase(OWNER_OS, "P", claim=False)      # same length, past threshold
    assert recovered == [OWNER_OS]
    assert r["reason"] == "assistant_generating_wedged"


def test_a_changing_page_restarts_the_clock(monkeypatch):
    """Tokens, a "Thinking"/"Worked for" timer, a tool step: any change is life."""
    recovered = []
    _wire(monkeypatch, Busy([5000, 5000, 5212]), recovered)
    t = [1000.0]
    monkeypatch.setattr(cdp.time, "time", lambda: t[0])
    # These steps jump a whole threshold at once; live retries come every few minutes.
    monkeypatch.setattr(cdp, "WEDGE_GAP_SECS", 10 * cdp.WEDGE_CEILING_SECS)
    cdp.submit_phrase(OWNER_OS, "P", claim=False)
    t[0] += cdp.WEDGE_MIN_SECS + 1
    cdp.submit_phrase(OWNER_OS, "P", claim=False)          # still 5000 -> wedged once
    recovered.clear()
    t[0] += cdp.WEDGE_MIN_SECS + 1
    r = cdp.submit_phrase(OWNER_OS, "P", claim=False)      # 5212: alive, clock restarts
    assert recovered == [] and r["reason"] == "assistant_still_generating"


def test_an_idle_observation_forgets_the_conversation():
    cdp._generating_for(OWNER_OS, 10, now=0.0)
    assert cdp._generating_for(OWNER_OS, 10, now=500.0)[0] == 500.0
    cdp._GENERATING_SINCE.pop(OWNER_OS, None)                # what gen_before False does
    assert cdp._generating_for(OWNER_OS, 10, now=600.0)[0] == 0.0


def test_the_clock_is_per_conversation():
    cdp._generating_for(OWNER_OS, 10, now=0.0)
    assert cdp._generating_for(PL, 10, now=5000.0)[0] == 0.0


def test_an_unreadable_length_neither_proves_life_nor_wedge():
    cdp._generating_for(OWNER_OS, None, now=0.0)
    assert cdp._generating_for(OWNER_OS, None, now=100.0)[0] == 100.0
    assert cdp._generating_for(OWNER_OS, 77, now=200.0)[0] == 200.0   # first real length


def test_a_new_tab_never_inherits_an_old_observation():
    """Review: a stale entry must not make a fresh sighting count as aged at once."""
    cdp._generating_for(OWNER_OS, 10, now=0.0, tab_id="OLD")
    assert cdp._generating_for(OWNER_OS, 10, now=5000.0, tab_id="NEW") == (0.0, 0.0)


def test_life_signs_cannot_postpone_recovery_past_the_ceiling(monkeypatch):
    """Review: a ticking timer changes the length forever; a stuck turn must still be
    recovered once the ceiling is reached."""
    recovered = []
    _wire(monkeypatch, Busy(list(range(5000, 5100))), recovered)
    t = [1000.0]
    monkeypatch.setattr(cdp.time, "time", lambda: t[0])
    # These steps jump a whole threshold at once; live retries come every few minutes.
    monkeypatch.setattr(cdp, "WEDGE_GAP_SECS", 10 * cdp.WEDGE_CEILING_SECS)
    for _ in range(3):
        cdp.submit_phrase(OWNER_OS, "P", claim=False)
        assert recovered == []
        t[0] += cdp.WEDGE_MIN_SECS + 1                     # length changed every time
    cdp.submit_phrase(OWNER_OS, "P", claim=False)          # past 3x since first sighting
    assert recovered == [OWNER_OS]


def test_a_malformed_tab_cap_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setenv("CDP_CONVERSATION_TABS", "six")
    assert cdp._int_env("CDP_CONVERSATION_TABS", 6) == 6


def test_page_text_len_reads_a_length_never_text():
    seen = {}

    class S:
        def call(self, m, p=None):
            seen["e"] = (p or {}).get("expression", "")
            return {"result": {"value": 42}}
    assert cdp._page_text_len(S()) == 42
    assert seen["e"].endswith(".length")


def test_a_gap_between_sightings_starts_over():
    """Review: hours of attempts that never reached the generating check (a focus trap)
    must not make the next sighting count as already aged."""
    cdp._generating_for(OWNER_OS, 10, now=0.0, tab_id="T")
    assert cdp._generating_for(OWNER_OS, 10, now=cdp.WEDGE_GAP_SECS / 2, tab_id="T")[0] > 0
    later = cdp.WEDGE_GAP_SECS / 2 + cdp.WEDGE_GAP_SECS + 1
    assert cdp._generating_for(OWNER_OS, 10, now=later, tab_id="T") == (0.0, 0.0)


def test_regular_sightings_keep_the_clock_running():
    t = 0.0
    for _ in range(5):
        age, total = cdp._generating_for(OWNER_OS, 10, now=t, tab_id="T")
        t += cdp.WEDGE_GAP_SECS - 1
    assert age == total and total >= 4 * (cdp.WEDGE_GAP_SECS - 1)


def test_the_gap_outlasts_the_slowest_retry_cadence():
    """Review: a non-actionable wake is re-claimed only every COOLDOWN + backoff; a gap
    shorter than that would reset the clocks forever and a stuck chat never recovers."""
    from core import wake_bridge
    assert cdp.WEDGE_GAP_SECS > wake_bridge.COOLDOWN_SECS + wake_bridge.RETRY_BACKOFF_SECS


def test_a_stuck_chat_with_only_slow_retries_still_reaches_the_ceiling(monkeypatch):
    recovered = []
    _wire(monkeypatch, Busy(list(range(5000, 5100))), recovered)   # length always changes
    t = [1000.0]
    monkeypatch.setattr(cdp.time, "time", lambda: t[0])
    from core import wake_bridge
    step = wake_bridge.COOLDOWN_SECS + wake_bridge.RETRY_BACKOFF_SECS   # 1200 s apart
    while t[0] - 1000.0 < cdp.WEDGE_CEILING_SECS:
        cdp.submit_phrase(OWNER_OS, "P", claim=False)
        assert recovered == []
        t[0] += step
    cdp.submit_phrase(OWNER_OS, "P", claim=False)
    assert recovered == [OWNER_OS]
