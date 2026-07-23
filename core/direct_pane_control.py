"""Fail-closed direct pane control for the Owner OS runtime.

Provides a safe, auditable, idempotent way to inspect and act on the exact
pending input of ONE explicitly targeted tmux pane, without delegating to a
second agent. It never guesses a target and refuses to act on any ambiguity:
target mismatch, changed input, copy mode, dead pane, or duplicate matches all
fail closed. Every state-changing action emits an AuditRecord.

The tmux transport is injected as a `runner` callable so all logic is fully
testable without a live tmux server. This module is the safety core; the
deployed control surface wires a real subprocess runner and a durable audit
sink into it. Live deployment, systemd watcher wiring, and the SEO closure
audit are host-level steps that cannot be performed from a code plan alone.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional


class ControlError(Exception):
    """Raised whenever an action must fail closed."""


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PaneTarget:
    session: str
    window: int
    pane: int

    @classmethod
    def parse(cls, spec: str) -> "PaneTarget":
        # form: session:window.pane  e.g. "seo-audit:0.0"
        try:
            sess, rest = spec.rsplit(":", 1)
            win, pane = rest.split(".", 1)
            return cls(sess, int(win), int(pane))
        except Exception as exc:  # noqa: BLE001 - normalize to ControlError
            raise ControlError(f"unparseable pane target: {spec!r}") from exc

    @property
    def spec(self) -> str:
        return f"{self.session}:{self.window}.{self.pane}"


@dataclass(frozen=True)
class PaneSnapshot:
    target_spec: str
    pane_id: str
    dead: bool
    in_copy_mode: bool
    cwd: str
    pid: int
    pending_input: str

    @property
    def pending_fingerprint(self) -> str:
        return fingerprint(self.pending_input)


@dataclass
class AuditRecord:
    action: str
    target_spec: str
    pane_id: str
    pending_input: str
    pending_fingerprint: str
    reason: str
    outcome: str
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


Runner = Callable[[List[str]], str]


class DirectPaneController:
    """Server-side direct control of a single explicitly targeted pane."""

    def __init__(
        self,
        runner: Runner,
        audit_sink: Optional[Callable[[AuditRecord], None]] = None,
    ) -> None:
        self._runner = runner
        self._audit_sink = audit_sink or (lambda rec: None)
        # idempotency ledger: op-key -> outcome
        self._done: Dict[str, str] = {}

    # -- transport -----------------------------------------------------
    def _run(self, args: List[str]) -> str:
        try:
            return self._runner(args)
        except Exception as exc:  # noqa: BLE001 - normalize to ControlError
            raise ControlError(f"tmux transport failed: {exc}") from exc

    def _list_matches(self, target: PaneTarget) -> List[List[str]]:
        out = self._run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}:#{window_index}.#{pane_index}\t"
                "#{pane_id}\t#{pane_dead}\t#{pane_in_mode}\t"
                "#{pane_current_path}\t#{pane_pid}",
            ]
        )
        matches: List[List[str]] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) != 6:
                continue
            if parts[0] == target.spec:
                matches.append(parts)
        return matches

    def _capture_pending(self, target: PaneTarget) -> str:
        out = self._run(["tmux", "capture-pane", "-p", "-t", target.spec])
        for line in reversed(out.splitlines()):
            if line.strip():
                return line.rstrip("\n")
        return ""

    # -- read ----------------------------------------------------------
    def capture(self, target: PaneTarget) -> PaneSnapshot:
        matches = self._list_matches(target)
        if len(matches) == 0:
            raise ControlError(
                f"no pane matches target {target.spec}: dead or missing"
            )
        if len(matches) > 1:
            raise ControlError(
                f"ambiguous target {target.spec}: {len(matches)} matches"
            )
        _, pane_id, dead, in_mode, cwd, pid = matches[0]
        return PaneSnapshot(
            target_spec=target.spec,
            pane_id=pane_id,
            dead=(dead == "1"),
            in_copy_mode=(in_mode == "1"),
            cwd=cwd,
            pid=int(pid),
            pending_input=self._capture_pending(target),
        )

    def _guard(self, snap: PaneSnapshot, expected_pending: str) -> None:
        if snap.dead:
            raise ControlError(f"pane {snap.target_spec} is dead")
        if snap.in_copy_mode:
            raise ControlError(f"pane {snap.target_spec} is in copy mode")
        if snap.pending_input != expected_pending:
            raise ControlError(
                f"pending input changed for {snap.target_spec}; refusing to act"
            )

    def inspect(self, target_spec: str, expected_pending: str) -> PaneSnapshot:
        target = PaneTarget.parse(target_spec)
        snap = self.capture(target)
        self._guard(snap, expected_pending)
        return snap

    def _emit(self, rec: AuditRecord) -> AuditRecord:
        self._audit_sink(rec)
        return rec

    # -- cancel (defensive: clears without executing) ------------------
    def cancel_pending(
        self, target_spec: str, expected_pending: str, reason: str
    ) -> AuditRecord:
        target = PaneTarget.parse(target_spec)
        snap = self.capture(target)
        self._guard(snap, expected_pending)
        op_key = f"cancel:{snap.pane_id}:{snap.pending_fingerprint}"
        if op_key in self._done:
            return self._emit(
                AuditRecord(
                    "cancel", snap.target_spec, snap.pane_id,
                    snap.pending_input, snap.pending_fingerprint,
                    reason, outcome="idempotent-noop",
                )
            )
        # Clear the pending line WITHOUT submitting it. We send C-u only and
        # never send Enter/C-m, so the stale instruction cannot execute.
        self._run(["tmux", "send-keys", "-t", snap.target_spec, "C-u"])
        after = self._capture_pending(target)
        if after == expected_pending:
            raise ControlError("pending input still present after cancel")
        self._done[op_key] = "cancelled"
        return self._emit(
            AuditRecord(
                "cancel", snap.target_spec, snap.pane_id,
                snap.pending_input, snap.pending_fingerprint,
                reason, outcome="cancelled",
            )
        )

    # -- submit (press Enter on the exact verified pending line) --------
    def submit_pending(
        self, target_spec: str, expected_pending: str, reason: str
    ) -> AuditRecord:
        target = PaneTarget.parse(target_spec)
        snap = self.capture(target)
        self._guard(snap, expected_pending)
        op_key = f"submit:{snap.pane_id}:{snap.pending_fingerprint}"
        if op_key in self._done:
            return self._emit(
                AuditRecord(
                    "submit", snap.target_spec, snap.pane_id,
                    snap.pending_input, snap.pending_fingerprint,
                    reason, outcome="idempotent-noop",
                )
            )
        self._run(["tmux", "send-keys", "-t", snap.target_spec, "C-m"])
        self._done[op_key] = "submitted"
        return self._emit(
            AuditRecord(
                "submit", snap.target_spec, snap.pane_id,
                snap.pending_input, snap.pending_fingerprint,
                reason, outcome="submitted",
            )
        )

    # -- replace (clear verified pending, type explicit new text) ------
    def replace_pending(
        self,
        target_spec: str,
        expected_pending: str,
        new_text: str,
        reason: str,
        submit: bool = False,
    ) -> AuditRecord:
        if not isinstance(new_text, str):
            raise ControlError("new_text must be an explicit string")
        target = PaneTarget.parse(target_spec)
        snap = self.capture(target)
        self._guard(snap, expected_pending)
        op_key = (
            f"replace:{snap.pane_id}:{snap.pending_fingerprint}:"
            f"{fingerprint(new_text)}"
        )
        if op_key in self._done:
            return self._emit(
                AuditRecord(
                    "replace", snap.target_spec, snap.pane_id,
                    snap.pending_input, snap.pending_fingerprint,
                    reason, outcome="idempotent-noop",
                )
            )
        self._run(["tmux", "send-keys", "-t", snap.target_spec, "C-u"])
        self._run(["tmux", "send-keys", "-t", snap.target_spec, "-l", new_text])
        if submit:
            self._run(["tmux", "send-keys", "-t", snap.target_spec, "C-m"])
        self._done[op_key] = "replaced"
        return self._emit(
            AuditRecord(
                "replace", snap.target_spec, snap.pane_id,
                snap.pending_input, snap.pending_fingerprint,
                reason, outcome="replaced-submitted" if submit else "replaced",
            )
        )
