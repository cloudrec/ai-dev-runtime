"""Unit tests for the fail-closed direct pane control core."""
from __future__ import annotations

import pytest

from core.direct_pane_control import (
    AuditRecord,
    ControlError,
    DirectPaneController,
    PaneTarget,
    fingerprint,
)

STALE_LINE = "\u0440\u0430\u0437\u0440\u0435\u0448\u0438, \u043f\u0443\u0431\u043b\u0438\u043a\u0443\u0439 \u0438 \u0423\u0410 \u0442\u043e\u0436\u0435 \u0441\u0434\u0435\u043b\u0430\u0439"


class FakeTmux:
    def __init__(self, panes):
        # panes: dict spec -> dict(id, dead, mode, cwd, pid, pending)
        self.panes = panes
        self.sent = []

    def __call__(self, args):
        if args[:2] == ["tmux", "list-panes"]:
            lines = []
            for spec, p in self.panes.items():
                lines.append(
                    "\t".join([spec, p["id"], p["dead"], p["mode"], p["cwd"], p["pid"]])
                )
            return "\n".join(lines) + "\n"
        if args[:2] == ["tmux", "capture-pane"]:
            spec = args[args.index("-t") + 1]
            return self.panes[spec]["pending"] + "\n"
        if args[:2] == ["tmux", "send-keys"]:
            spec = args[args.index("-t") + 1]
            keys = args[args.index("-t") + 2:]
            self.sent.append((spec, keys))
            if "C-u" in keys:
                self.panes[spec]["pending"] = ""
            elif "-l" in keys:
                self.panes[spec]["pending"] = keys[keys.index("-l") + 1]
            return ""
        raise AssertionError("unexpected " + " ".join(args))


def _pane(pending, dead="0", mode="0", cwd="/opt/seo", pid="1234", pid_id="%1"):
    return {"id": pid_id, "dead": dead, "mode": mode, "cwd": cwd, "pid": pid, "pending": pending}


def test_parse_target():
    t = PaneTarget.parse("seo-audit:0.0")
    assert t.session == "seo-audit" and t.window == 0 and t.pane == 0
    assert t.spec == "seo-audit:0.0"


def test_parse_target_bad():
    with pytest.raises(ControlError):
        PaneTarget.parse("garbage")


def test_capture_single():
    fake = FakeTmux({"seo-audit:0.0": _pane(STALE_LINE)})
    ctl = DirectPaneController(fake)
    snap = ctl.capture(PaneTarget.parse("seo-audit:0.0"))
    assert snap.pending_input == STALE_LINE
    assert snap.pending_fingerprint == fingerprint(STALE_LINE)
    assert snap.pane_id == "%1"


def test_missing_fails_closed():
    fake = FakeTmux({"other:0.0": _pane("x")})
    ctl = DirectPaneController(fake)
    with pytest.raises(ControlError):
        ctl.capture(PaneTarget.parse("seo-audit:0.0"))


def test_ambiguous_fails_closed():
    # emulate two panes reporting the same spec
    class Dup(FakeTmux):
        def __call__(self, args):
            if args[:2] == ["tmux", "list-panes"]:
                row = "\t".join(["seo-audit:0.0", "%1", "0", "0", "/opt/seo", "1"])
                return row + "\n" + row + "\n"
            return super().__call__(args)

    ctl = DirectPaneController(Dup({"seo-audit:0.0": _pane("x")}))
    with pytest.raises(ControlError):
        ctl.capture(PaneTarget.parse("seo-audit:0.0"))


def test_dead_fails_closed():
    fake = FakeTmux({"seo-audit:0.0": _pane(STALE_LINE, dead="1")})
    ctl = DirectPaneController(fake)
    with pytest.raises(ControlError):
        ctl.inspect("seo-audit:0.0", STALE_LINE)


def test_copy_mode_fails_closed():
    fake = FakeTmux({"seo-audit:0.0": _pane(STALE_LINE, mode="1")})
    ctl = DirectPaneController(fake)
    with pytest.raises(ControlError):
        ctl.inspect("seo-audit:0.0", STALE_LINE)


def test_changed_input_fails_closed():
    fake = FakeTmux({"seo-audit:0.0": _pane("something else")})
    ctl = DirectPaneController(fake)
    with pytest.raises(ControlError):
        ctl.cancel_pending("seo-audit:0.0", STALE_LINE, reason="recover")


def test_cancel_success_clears_and_audits():
    fake = FakeTmux({"seo-audit:0.0": _pane(STALE_LINE)})
    records = []
    ctl = DirectPaneController(fake, audit_sink=records.append)
    rec = ctl.cancel_pending("seo-audit:0.0", STALE_LINE, reason="stale publish")
    assert isinstance(rec, AuditRecord)
    assert rec.outcome == "cancelled"
    assert rec.pending_input == STALE_LINE
    assert fake.panes["seo-audit:0.0"]["pending"] == ""
    assert records and records[0].pending_fingerprint == fingerprint(STALE_LINE)


def test_cancel_never_sends_enter():
    fake = FakeTmux({"seo-audit:0.0": _pane(STALE_LINE)})
    ctl = DirectPaneController(fake)
    ctl.cancel_pending("seo-audit:0.0", STALE_LINE, reason="stale publish")
    for _spec, keys in fake.sent:
        assert "C-m" not in keys and "Enter" not in keys


def test_cancel_idempotent():
    fake = FakeTmux({"seo-audit:0.0": _pane(STALE_LINE)})
    ctl = DirectPaneController(fake)
    first = ctl.cancel_pending("seo-audit:0.0", STALE_LINE, reason="r")
    assert first.outcome == "cancelled"
    # restore the same line and fire again -> idempotent no-op, no second clear
    fake.panes["seo-audit:0.0"]["pending"] = STALE_LINE
    sent_before = len(fake.sent)
    second = ctl.cancel_pending("seo-audit:0.0", STALE_LINE, reason="r")
    assert second.outcome == "idempotent-noop"
    assert len(fake.sent) == sent_before


def test_seo_recovery_scenario():
    """End-to-end: prove the exact stale line, preserve it, cancel it once."""
    fake = FakeTmux({"seo-audit:0.0": _pane(STALE_LINE)})
    records = []
    ctl = DirectPaneController(fake, audit_sink=records.append)
    snap = ctl.inspect("seo-audit:0.0", STALE_LINE)  # prove not submitted
    preserved = snap.pending_input  # recovery audit copy
    rec = ctl.cancel_pending("seo-audit:0.0", STALE_LINE, reason="stale Mess publish")
    assert preserved == STALE_LINE
    assert rec.outcome == "cancelled"
    assert fake.panes["seo-audit:0.0"]["pending"] == ""
    assert '"outcome": "cancelled"' in rec.to_json()
