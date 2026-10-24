"""GPT CTO bridge: high-level reasoning events -> provider -> decision ledger -> outbox.

Only these event types ever reach the provider: architecture_conflict, repeated_failure,
shared_core_change, milestone_review, owner_needed. Routine continuation never does (that
stays inside Claude via `/goal`, see core/native_goals.py).

Flow, exactly once per event key:
  1. latch a `cto_decision` row (PK event_key) holding the compact context BEFORE the
     provider call — a replayed event finds the latch and does nothing;
  2. no provider configured (disabled / missing model / missing key) → the row stays
     `waiting_provider` (durable, not lost) and `retry_waiting()` decides it once the
     config exists; a provider error is retried up to MAX_ATTEMPTS, then `provider_failed`;
  3. the provider runs in ONE durable conversation per Owner OS project (`cto_conversation`,
     OpenAI Conversations API) so the CTO keeps context across events; instructions are
     sent on every response;
  4. the parsed output is recorded in `cto_decision` and the control-plane `decision` ledger;
  5. a message for the agent is ENQUEUED in `native_outbox` (PK msg_key) — delivered by a
     controller Claude session via SendMessage (default), or, only with
     OWNER_OS_CTO_DELIVERY_MODE=agent_control, by `deliver_pending_agent_control()`; never
     through ChatGPT browser automation;
  6. `owner_needed` (event type, or the provider says so) is never auto-approved and the
     advice is NOT forwarded to the agent; a provider escalation of a non-owner event emits
     OWNER_NEEDED once (an owner_needed event already notified the owner itself).

Provider config (no defaults for model or key; nothing hardcoded):
  OWNER_OS_CTO_PROVIDER = "" (disabled, default) | "openai"
  OWNER_OS_CTO_MODEL    = required when a provider is set
  OPENAI_API_KEY        = required for "openai" (read from the environment only; the value
                          is never logged, stored or returned)
  OWNER_OS_CTO_BASE_URL = optional, default https://api.openai.com/v1
Denied agents (payorch-ha-fresh, + OWNER_OS_CTO_DENY_AGENTS) never get an outbox message.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from typing import Callable, Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

REASONING_EVENTS = frozenset({"architecture_conflict", "repeated_failure",
                              "shared_core_change", "milestone_review", "owner_needed"})
HARD_DENY_AGENTS = frozenset({"payorch-ha-fresh"})
SOURCE = "cto_bridge"
MAX_ATTEMPTS = 3
ADVICE_PREFIX = "[Owner OS CTO advice — automated, not owner approval] "
_LIMITS = {"question": 2000, "goal": 600, "state": 200, "diff_summary": 4000,
           "decision": 300, "max_decisions": 5, "max_affected": 20}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cto_decision (
    event_key TEXT PRIMARY KEY, event_type TEXT, project TEXT, agent TEXT, provider TEXT,
    model TEXT, context_hash TEXT, status TEXT, decision TEXT, rationale TEXT,
    owner_needed INTEGER DEFAULT 0, message_to_agent TEXT, error TEXT,
    latched_ts REAL, latched_at TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS native_outbox (
    msg_key TEXT PRIMARY KEY, event_key TEXT, to_agent TEXT, message TEXT,
    state TEXT DEFAULT 'pending', created_at TEXT, sent_at TEXT, receipt TEXT);
CREATE TABLE IF NOT EXISTS cto_conversation (
    project TEXT, provider TEXT, conversation_id TEXT, created_at TEXT,
    PRIMARY KEY (project, provider));
"""
# additive columns on cto_decision (older DBs get them on first use)
_COLUMNS = {"context": "TEXT", "attempts": "INTEGER DEFAULT 0",
            "owner_emitted": "INTEGER DEFAULT 0", "conversation_id": "TEXT"}


def _schema(conn) -> None:
    conn.executescript(_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(cto_decision)")}
    for col, decl in _COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE cto_decision ADD COLUMN {col} {decl}")


class ProviderError(Exception):
    pass


class Provider:
    name = "none"
    model = ""

    def decide(self, context: dict, conversation_id: Optional[str] = None) -> dict:
        raise NotImplementedError  # pragma: no cover — interface

    def create_conversation(self, project: str) -> Optional[str]:
        return None                  # providers without persistent threads stay stateless


class OpenAIResponsesProvider(Provider):
    """OpenAI Responses API + Conversations API.

    `create_conversation` → POST {base}/conversations, returns the conversation id;
    `decide` → POST {base}/responses with `conversation` set, so every event for a project
    lands in the same durable thread. `instructions` are sent on every response (they do not
    carry over between responses). Asks for one JSON object back."""
    name = "openai"
    INSTRUCTIONS = (
        "You are the CTO reviewer for an autonomous engineering fleet. You receive one "
        "structured event at a time for this project; earlier events and your earlier "
        "decisions are in this conversation. Reply with ONLY a JSON object: {\"decision\": "
        "short imperative, \"rationale\": <=3 sentences, \"owner_needed\": true|false, "
        "\"message_to_agent\": text for the agent or \"\"}. Set owner_needed=true for "
        "anything involving production release, money, credentials, CAPTCHA/anti-bot, paid "
        "proxy/VPN, irreversible actions or product decisions; never approve those yourself.")

    def __init__(self, *, model: str, api_key: str, base_url: str = "", timeout: int = 60,
                 opener=None):
        if not model or not api_key:
            raise ProviderError("model and api key are required")
        self.model, self._key = model, api_key
        self.base = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.timeout, self._open = timeout, opener or urllib.request.urlopen

    def __repr__(self):                       # never expose the key
        return f"OpenAIResponsesProvider(model={self.model!r})"

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(f"{self.base}/{path}", data=json.dumps(payload).encode(),
                                     method="POST",
                                     headers={"Authorization": f"Bearer {self._key}",
                                              "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 — never echo the request (it holds the key)
            raise ProviderError(f"request_failed:{type(e).__name__}") from None

    def create_conversation(self, project: str) -> Optional[str]:
        data = self._post("conversations", {"metadata": {"owner_os_project": project[:64]}})
        cid = data.get("id")
        if not isinstance(cid, str) or not cid:
            raise ProviderError("conversation_create_returned_no_id")
        return cid

    def decide(self, context: dict, conversation_id: Optional[str] = None) -> dict:
        body = {"model": self.model, "instructions": self.INSTRUCTIONS,
                "input": json.dumps(context, sort_keys=True)}
        if conversation_id:
            body["conversation"] = conversation_id
        return parse_output(_output_text(self._post("responses", body)))


def _output_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    parts = []
    for item in data.get("output") or []:
        for c in (item or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                parts.append(c.get("text") or "")
    return "".join(parts)


def parse_output(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").split("\n", 1)[-1]
    try:
        obj = json.loads(t[t.find("{"): t.rfind("}") + 1])
    except ValueError:
        raise ProviderError("unparseable_provider_output") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("decision"), str):
        raise ProviderError("provider_output_missing_decision")
    return {"decision": obj["decision"][:500], "rationale": str(obj.get("rationale") or "")[:2000],
            "owner_needed": obj.get("owner_needed") is True,
            "message_to_agent": str(obj.get("message_to_agent") or "")[:4000]}


def provider_from_env() -> tuple:
    """(provider | None, gate_reason). Disabled is the default and is not an error."""
    kind = os.getenv("OWNER_OS_CTO_PROVIDER", "").strip().lower()
    if not kind:
        return None, "cto_provider_disabled"
    if kind != "openai":
        return None, f"cto_provider_unknown:{kind}"
    model = os.getenv("OWNER_OS_CTO_MODEL", "").strip()
    if not model:
        return None, "missing_config:OWNER_OS_CTO_MODEL"
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return None, "missing_secret:OPENAI_API_KEY"
    return OpenAIResponsesProvider(model=model, api_key=os.environ["OPENAI_API_KEY"].strip(),
                                   base_url=os.getenv("OWNER_OS_CTO_BASE_URL", "")), ""


def _cut(v, n):
    return (str(v) if v is not None else "")[:n]


def build_context(ev: dict) -> dict:
    """Compact, bounded, structured — never a raw pane tail or transcript."""
    return {"event_type": ev.get("type"), "project": _cut(ev.get("project"), 120),
            "agent": _cut(ev.get("agent"), 120), "goal": _cut(ev.get("goal"), _LIMITS["goal"]),
            "state": _cut(ev.get("state"), _LIMITS["state"]),
            "question": _cut(ev.get("question"), _LIMITS["question"]),
            "git_diff_summary": _cut(ev.get("diff_summary"), _LIMITS["diff_summary"]),
            "recent_decisions": [_cut(d, _LIMITS["decision"]) for d in
                                 (ev.get("recent_decisions") or [])[:_LIMITS["max_decisions"]]],
            "affected_agents": [_cut(a, 120) for a in
                                (ev.get("affected_agents") or [])[:_LIMITS["max_affected"]]]}


def agent_denied(agent: str) -> bool:
    extra = {s.strip() for s in os.getenv("OWNER_OS_CTO_DENY_AGENTS", "").split(",")
             if s.strip()}
    base = (agent or "").split(":")[0]
    return base in HARD_DENY_AGENTS or base in extra


def conversation_for(project: str, provider: Provider, conn) -> Optional[str]:
    """The project's durable conversation id, creating (and storing) it on first use."""
    key = project or "_default"
    r = conn.execute("SELECT conversation_id FROM cto_conversation WHERE project=? AND "
                     "provider=?", (key, provider.name)).fetchone()
    if r:
        return r[0]
    cid = provider.create_conversation(key)
    if not cid:
        return None
    conn.execute("INSERT OR IGNORE INTO cto_conversation (project,provider,conversation_id,"
                 "created_at) VALUES (?,?,?,?)", (key, provider.name, cid, now_iso()))
    conn.commit()
    # a concurrent creator may have won: everyone uses the stored one
    return conn.execute("SELECT conversation_id FROM cto_conversation WHERE project=? AND "
                        "provider=?", (key, provider.name)).fetchone()[0]


def _decide_row(conn, key: str, provider: Optional[Provider], gate: str,
                emit_owner: Optional[Callable]) -> dict:
    row = conn.execute("SELECT event_type,context,attempts,owner_emitted FROM cto_decision "
                       "WHERE event_key=?", (key,)).fetchone()
    etype, ctx = row[0], json.loads(row[1] or "{}")
    attempts, owner_emitted = row[2] or 0, row[3] or 0
    owner_needed = etype == "owner_needed"
    out, err, cid = None, None, None
    if provider is None:
        status, err = "waiting_provider", gate or "cto_provider_disabled"
    else:
        attempts += 1
        try:
            cid = conversation_for(ctx.get("project") or "", provider, conn)
            out, status = provider.decide(ctx, conversation_id=cid), "decided"
        except ProviderError as e:
            err = str(e)[:200]
            status = "provider_failed" if attempts >= MAX_ATTEMPTS else "provider_error"
    if out:
        owner_needed = owner_needed or out["owner_needed"]
    msg = (out or {}).get("message_to_agent") or ""
    conn.execute("UPDATE cto_decision SET status=?, decision=?, rationale=?, owner_needed=?, "
                 "message_to_agent=?, error=?, finished_at=?, attempts=?, conversation_id=?, "
                 "provider=?, model=? WHERE event_key=?",
                 (status, (out or {}).get("decision"), (out or {}).get("rationale"),
                  1 if owner_needed else 0, msg, err, now_iso(), attempts, cid,
                  getattr(provider, "name", "none"), getattr(provider, "model", ""), key))
    if out:
        from core.control_plane import api as cp
        cp.record_decision("agent", ctx.get("agent") or "", "cto_advice",
                           out["decision"][:300], out["rationale"][:1000],
                           model_tier=f"cto:{provider.model}", conn=conn)
    queued = False
    agent = ctx.get("agent") or ""
    if msg and agent and not agent_denied(agent) and not owner_needed:
        # owner-needed advice is NOT forwarded as an instruction: the owner decides first
        queued = conn.execute(
            "INSERT OR IGNORE INTO native_outbox (msg_key,event_key,to_agent,message,"
            "created_at) VALUES (?,?,?,?,?)",
            (f"cto:{key}", key, agent, msg, now_iso())).rowcount == 1
    # An owner_needed EVENT already reached the owner through its own emission; the bridge
    # notifies only when the provider escalates something that was not owner-flagged.
    escalated = bool(out and out["owner_needed"]) and etype != "owner_needed"
    emit_now = escalated and not owner_emitted and emit_owner is not None
    if emit_now:
        conn.execute("UPDATE cto_decision SET owner_emitted=1 WHERE event_key=?", (key,))
    conn.commit()
    if emit_now:
        emit_owner(ctx, (out or {}).get("decision") or etype)
    return {"handled": True, "status": status, "owner_needed": owner_needed,
            "message_queued": queued, "error": err, "conversation_id": cid}


def handle_event(ev: dict, *, provider: Optional[Provider] = None, gate: str = "",
                 emit_owner=None, conn=None) -> dict:
    """ev: {key, type, project, agent, goal, state, question, diff_summary, ...}."""
    if ev.get("type") not in REASONING_EVENTS:
        return {"handled": False, "reason": "not_a_reasoning_event"}
    if not ev.get("key"):
        return {"handled": False, "reason": "event_key_required"}
    if provider is None and not gate:
        provider, gate = provider_from_env()
    ctx = build_context(ev)
    ctx_json = json.dumps(ctx, sort_keys=True)
    conn, own = _c(conn)
    try:
        _schema(conn)
        cur = conn.execute(
            "INSERT OR IGNORE INTO cto_decision (event_key,event_type,project,agent,"
            "context_hash,context,status,latched_ts,latched_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (ev["key"], ev["type"], ctx["project"], ctx["agent"],
             hashlib.sha256(ctx_json.encode()).hexdigest()[:16], ctx_json, "latched",
             now_ts(), now_iso()))
        conn.commit()
        if cur.rowcount != 1:
            return {"handled": False, "reason": "duplicate_event_key"}
        return _decide_row(conn, ev["key"], provider, gate, emit_owner)
    finally:
        if own:
            conn.close()


def retry_waiting(*, provider: Optional[Provider] = None, gate: str = "", emit_owner=None,
                  limit: int = 20, conn=None) -> list:
    """Decide rows left `waiting_provider` / `provider_error` once a provider exists.
    `latched` rows (a crash mid-call: outcome unknown) are never retried automatically."""
    if provider is None and not gate:
        provider, gate = provider_from_env()
    if provider is None:
        return []
    conn, own = _c(conn)
    try:
        _schema(conn)
        keys = [r[0] for r in conn.execute(
            "SELECT event_key FROM cto_decision WHERE status IN ('waiting_provider',"
            "'provider_error') ORDER BY latched_ts LIMIT ?", (int(limit),))]
        return [dict(_decide_row(conn, k, provider, gate, emit_owner), event_key=k)
                for k in keys]
    finally:
        if own:
            conn.close()


def pending_messages(conn=None) -> list:
    conn, own = _c(conn)
    try:
        _schema(conn)
        return [dict(zip(("msg_key", "to_agent", "message", "created_at"), r)) for r in
                conn.execute("SELECT msg_key,to_agent,message,created_at FROM native_outbox "
                             "WHERE state='pending' ORDER BY created_at")]
    finally:
        if own:
            conn.close()


def mark_sent(msg_key: str, receipt: str, conn=None) -> bool:
    """Called after a PROVEN delivery. Only pending -> sent, once."""
    conn, own = _c(conn)
    try:
        _schema(conn)
        n = conn.execute("UPDATE native_outbox SET state='sent', sent_at=?, receipt=? WHERE "
                         "msg_key=? AND state='pending'",
                         (now_iso(), (receipt or "")[:500], msg_key)).rowcount
        conn.commit()
        return n == 1
    finally:
        if own:
            conn.close()


def _mark(conn, msg_key: str, state: str, receipt: str) -> None:
    conn.execute("UPDATE native_outbox SET state=?, receipt=? WHERE msg_key=? AND "
                 "state='pending'", (state, receipt[:500], msg_key))
    conn.commit()


def delivery_mode() -> str:
    v = os.getenv("OWNER_OS_CTO_DELIVERY_MODE", "native_controller").strip().lower()
    return v if v in ("native_controller", "agent_control", "off") else "off"


def deliver_pending_agent_control(listing: dict, *, send: Optional[Callable] = None,
                                  conn=None) -> list:
    """Server-side fallback (OWNER_OS_CTO_DELIVERY_MODE=agent_control only): deliver pending
    CTO advice with the proven `agent_control.agent_send`, only into an IDLE session located
    through native state. Busy/unlocated → stays pending. Denied → `denied`, never sent.
    Marked sent only when agent_send reports a submitted delivery."""
    if delivery_mode() != "agent_control":
        return []
    from core import native_agents as na
    if send is None:
        from core import agent_control as ac

        def send(target, text, key):
            return ac.agent_send(target, text, idempotency_key=key, actor=SOURCE,
                                 source=SOURCE)
    conn, own = _c(conn)
    out = []
    try:
        _schema(conn)
        for m in pending_messages(conn=conn):
            target = m["to_agent"]
            if agent_denied(target):
                _mark(conn, m["msg_key"], "denied", "hard_denied_agent")
                out.append({"msg_key": m["msg_key"], "result": "denied"})
                continue
            s = na.find(target, listing) if listing.get("source") == na.SOURCE_NATIVE else None
            if not s or s.get("tmux_target") != target:
                out.append({"msg_key": m["msg_key"], "result": "pending_unlocated"})
                continue
            if s.get("state") != "idle":
                out.append({"msg_key": m["msg_key"], "result": f"pending_{s.get('state')}"})
                continue
            try:
                res = send(target, ADVICE_PREFIX + m["message"], m["msg_key"])
            except Exception as e:  # noqa: BLE001 — unknown: agent_send dedupes the key
                res = {"error": type(e).__name__}
            if res.get("submitted") and res.get("delivered"):
                mark_sent(m["msg_key"], f"agent_control:{res.get('idempotency_key', '')}",
                          conn=conn)
                out.append({"msg_key": m["msg_key"], "result": "sent"})
            else:
                out.append({"msg_key": m["msg_key"], "result": "pending_unproven",
                            "reason": res.get("refused") or res.get("error") or ""})
        return out
    finally:
        if own:
            conn.close()
