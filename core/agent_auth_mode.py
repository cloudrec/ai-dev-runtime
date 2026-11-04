"""Per-agent auth mode for NEW Claude sessions: subscription (default) or Anthropic API
credits (explicit, per-agent opt-in), 2026-11-02.

Why: when the subscription's limits are exhausted, the owner may want ONE selected future
agent to run on API credits through the verified launcher `/usr/local/bin/claude-owner-api`.
That launcher reads the key and workspace id from /root/.config/owner-os/secrets/ itself,
at exec time. Owner OS therefore never reads, holds, logs or passes any secret: it only
chooses WHICH program the new pane runs.

Rules (all fail closed):
  * subscription is the default for every session; nothing switches automatically —
    no fallback to API on a refused/limited subscription, no mass switch;
  * a subscription session is started with the API credential variables REMOVED from its
    environment, so it can never inherit an API key from the tmux server or the daemon;
  * API mode needs ALL of: an explicit per-launch request (`auth_mode="api"`), the master
    switch `enabled: true` in config/agent_auth_modes.yaml, an entry for exactly this
    session name AND project dir, a safe launcher file (root-owned, not group/other
    writable, executable), the spend guardrails (live API agents and API launches per
    24 h under their caps; an unknown population refuses), and a verified owner proof:
    signed with the owner key, single use, short-lived, bound to this session, directory,
    model, risk, conversation and launcher. No actor name, config edit or agent text can
    stand in for it. The proof is spent and the launch counted in one transaction, in an
    append-only, hash-chained table (agent_auth_api_chain), so deleting ledger rows
    resets nothing and a damaged chain refuses every API launch. A keyed head anchor in a
    file outside the DB catches truncation and a dropped table;
  * model: Sonnet by default; Opus only when the launch says `risk="high"` AND the
    agent's entry allows Opus for high-risk work; anything else is refused;
  * session recovery never starts API mode: a recorded API session is not revived
    (an explicit relaunch is required) and a resume command naming the API launcher
    is refused.

Not enforceable here (stated, not hidden): a dollar cap per interactive session — the
Claude CLI caps spend only in print mode. The hard money ceiling must be the API
workspace's own spend limit (owner, Anthropic console).

Security model of the proof ledger (R6). Agents run as root on this host, so nothing here
is a boundary against a process willing to work at it:
  * stopped outright: any UPDATE or DELETE on owner_proof_used or agent_auth_api_chain
    through SQL, accidental or scripted (triggers), and replay of a spent nonce;
  * detected, every API launch then refused: a dropped trigger, an edited row, a truncated
    or dropped chain, an old or forged anchor file. Routine use repairs none of it: only the
    owner's reanchor does. Unless the anchor vouches for the chain it also needs an explicit
    accept_chain, because that is the owner judging the chain complete. Each one is audited;
  * not detected: wiping chain, spent rows and anchor together (looks like a fresh install),
    or putting back an old copy of the anchor together with a chain cut to match it;
  * not stopped: reading the owner key, pointing AGENT_OWNER_PROOF_KEY_FILE at a key of
    one's own, or editing this code. Each forges a proof outright.
A non-API proof is protected only by the triggers and its 300 s expiry: a deleted row makes
it replayable for what is left of those 300 s.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import time
from typing import Callable, Optional

SUBSCRIPTION, API = "subscription", "api"
MODES = (SUBSCRIPTION, API)
DEFAULT_MODEL = "sonnet"
HIGH_RISK_MODEL = "opus"
DEFAULT_LAUNCHER = "/usr/local/bin/claude-owner-api"
API_PROOF_ACTION = "agent_launch_api"

# credential variables a subscription session must never inherit
# (F7: also the switches that re-route the CLI to another billing path or another host)
API_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
                "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
                "AWS_BEARER_TOKEN_BEDROCK", "CLAUDE_CODE_USE_FOUNDRY",
                "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_BASE_URL")

# F2: what a `claude` child run directly by this process may inherit. An allowlist, so a
# credential or a routing switch nobody has named yet stays out. It keeps what the CLI needs
# to find its login and its sessions (HOME, CLAUDE_CONFIG_DIR, the subscription's own
# CLAUDE_CODE_OAUTH_TOKEN) and to reach the network from this host (proxy and CA settings).
SUBSCRIPTION_ENV_KEEP = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TERM",
    "TMPDIR", "TEMP", "TMP", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
    "https_proxy", "all_proxy", "no_proxy", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    # R1: behaviour switches that hold no credential and move no traffic to another biller.
    # DISABLE_AUTOUPDATER matters most: without it a child could update the shared binary.
    "DISABLE_AUTOUPDATER", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "MAX_THINKING_TOKENS"})
SUBSCRIPTION_ENV_KEEP_PREFIXES = ("LC_", "XDG_")

_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                       "config", "agent_auth_modes.yaml")


def config_path() -> str:
    return os.getenv("AGENT_AUTH_MODES_CONFIG") or _CONFIG


def load_config(path: Optional[str] = None) -> dict:
    """The parsed config, or {"_error": why}. Missing file = API disabled (not an error)."""
    path = path or config_path()
    if not os.path.isfile(path):
        return {"enabled": False, "agents": {}, "limits": {}}
    try:
        import yaml
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as e:  # noqa: BLE001
        return {"_error": f"config_unreadable:{type(e).__name__}"}
    if not isinstance(data, dict):
        return {"_error": "config_not_a_mapping"}
    return data


def is_api_env_var(name: str) -> bool:
    """A named credential or switch, or one in a family that exists only to route the CLI."""
    return name in API_ENV_VARS or name.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))


def subscription_env(base: Optional[dict] = None) -> dict:
    """The environment for a `claude` child spawned directly (`env=`). Only the allowlist
    passes, and API_ENV_VARS never does. Panes cannot use this: tmux hands them the server's
    environment, so they go through subscription_argv / subscription_shell instead."""
    src = os.environ if base is None else base
    return {k: v for k, v in src.items()
            if (k in SUBSCRIPTION_ENV_KEEP or k.startswith(SUBSCRIPTION_ENV_KEEP_PREFIXES))
            and not is_api_env_var(k)}


def subscription_argv(base: Optional[list] = None) -> list:
    """`claude …` with every API credential variable removed from its environment."""
    argv = ["env"]
    for v in API_ENV_VARS:
        argv += ["-u", v]
    return argv + ["claude", *(base or [])]


def subscription_shell(cmd: str) -> str:
    """The same, for a shell command string (session recovery's resume shape)."""
    return "env " + " ".join(f"-u {v}" for v in API_ENV_VARS) + " " + cmd


def _launcher_problem(path: str) -> Optional[str]:
    """Checks the launcher FILE (never its content, never the secrets it reads)."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return "launcher_missing"
    if not stat.S_ISREG(st.st_mode):
        return "launcher_not_regular_file"
    if st.st_uid != 0:
        return "launcher_not_root_owned"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return "launcher_group_or_other_writable"
    if not st.st_mode & stat.S_IXUSR:
        return "launcher_not_executable"
    return None


# ── launch ledger (agent_control DB; additive table) ───────────────────────────
def _ledger(conn=None):
    if conn is not None:
        c = conn
    else:
        from core import agent_control as ac
        c = sqlite3.connect(ac._db_path(), timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS agent_auth_launch (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, session TEXT, project_dir TEXT,
        mode TEXT, model TEXT, risk TEXT, actor TEXT)""")
    return c


_CHAIN_TRIGGERS = {
    "agent_auth_api_chain_no_update": "UPDATE",
    "agent_auth_api_chain_no_delete": "DELETE"}
_PROOF_TRIGGERS = {
    "owner_proof_used_no_update": "UPDATE",
    "owner_proof_used_no_delete": "DELETE"}


_PROOF_TABLE = ("CREATE TABLE IF NOT EXISTS owner_proof_used (nonce TEXT PRIMARY KEY, "
                "action TEXT, target TEXT, used_at TEXT)")


def _proof_triggers(c) -> None:
    for name, verb in _PROOF_TRIGGERS.items():
        c.execute(f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {verb} ON owner_proof_used"
                  " BEGIN SELECT RAISE(ABORT, 'owner_proof_used is append-only'); END")


def _chain_triggers(c) -> None:
    for name, verb in _CHAIN_TRIGGERS.items():
        c.execute(f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {verb} ON"
                  " agent_auth_api_chain BEGIN SELECT RAISE(ABORT,"
                  " 'agent_auth_api_chain is append-only'); END")


def ensure_proof_table(c) -> None:
    """The spent-nonce table for any owner proof, with its triggers until a chain exists.
    From then on this never adds them: it runs on every proof, so repairing here would erase
    what _chain_problem must keep seeing, and restoring them is the owner's step (reanchor).
    The caller owns the transaction."""
    have = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN"
        " ('owner_proof_used','agent_auth_api_chain')")}
    if "owner_proof_used" not in have:
        c.execute(_PROOF_TABLE)
    if "agent_auth_api_chain" not in have:
        _proof_triggers(c)


# ── head anchor: the chain's latest row, recorded outside the DB and keyed ────────
def _anchor_path() -> str:
    from core import agent_control as ac
    return os.getenv("AGENT_AUTH_API_ANCHOR_FILE") or ac._db_path() + ".api_anchor"


def _anchor_mac(rows: int, head: str) -> str:
    from core import agent_control as ac
    key = ac._owner_key()
    if key is None:
        return ""
    return hmac.new(key, f"api-chain-anchor-v1|{rows}|{head}".encode(),
                    hashlib.sha256).hexdigest()


def _write_anchor(rows: int, head: str) -> None:
    path = _anchor_path()
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"rows": rows, "head": head, "mac": _anchor_mac(rows, head)}, fh)
    os.replace(tmp, path)


@contextlib.contextmanager
def _anchor_lock():
    """Commit and anchor write stay in order across processes: an anchor can never move
    backwards because a slower process wrote last."""
    import fcntl
    fd = os.open(_anchor_path() + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _anchored_rows() -> Optional[int]:
    """The row count the anchor file claims, unverified: 0 with no file, None if unreadable."""
    try:
        with open(_anchor_path(), encoding="utf-8") as fh:
            a = json.load(fh)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return None
    rows = a.get("rows") if isinstance(a, dict) else None
    return rows if isinstance(rows, int) else None


def _anchor_problem(hashes: list) -> bool:
    """True when the chain does not match its anchor. The anchor may lag by one row (a crash
    between COMMIT and the file write), never lead: fewer rows than anchored is truncation.
    The file exists before the first row (rows 0), so no file under a row means deletion:
    "no file" is the one state the key cannot vouch for."""
    try:
        with open(_anchor_path(), encoding="utf-8") as fh:
            a = json.load(fh)
    except FileNotFoundError:
        return len(hashes) > 0
    except (OSError, ValueError):
        return True
    if not isinstance(a, dict):
        return True
    rows, head, mac = a.get("rows"), a.get("head"), a.get("mac")
    if not isinstance(rows, int) or not isinstance(head, str) or not isinstance(mac, str):
        return True
    want = _anchor_mac(rows, head)
    if not want or not hmac.compare_digest(want, mac):
        return True
    if rows == 0:
        return head != "" or len(hashes) > 1
    return not (1 <= rows <= len(hashes) <= rows + 1 and hashes[rows - 1] == head)


def _chain(c):
    """The append-only API launch chain. Created once, table and triggers together, and
    repaired only by the owner (reanchor): a dropped trigger must stay visible to
    _chain_problem."""
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND"
                 " name='agent_auth_api_chain'").fetchone():
        return c
    if c.in_transaction:
        c.commit()
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS agent_auth_api_chain (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session TEXT NOT NULL,
            project_dir TEXT NOT NULL, model TEXT NOT NULL, risk TEXT NOT NULL,
            actor TEXT NOT NULL, nonce TEXT NOT NULL UNIQUE, prev_hash TEXT NOT NULL,
            hash TEXT NOT NULL)""")
        _chain_triggers(c)
        c.execute(_PROOF_TABLE)
        _proof_triggers(c)
        c.commit()
    except BaseException:
        c.rollback()
        raise
    return c


def _close_anchor_lag(c) -> Optional[str]:
    """After a failed anchor write the newest row is unanchored, so deleting it together with
    its spent nonce would go unseen. The next attempt, even a refused one, anchors it. An
    empty chain gets its rows-0 anchor here, before the first row can exist. Call under
    _anchor_lock, after _chain_problem has accepted the anchor."""
    from core import agent_control as ac
    if ac._owner_key() is None:                   # no key: the proof check refuses anyway
        return None
    _, hashes = _chain_rows_problem(c)
    rows = _anchored_rows()
    if rows is None:
        return "api_ledger_tampered"
    if rows == len(hashes) and os.path.exists(_anchor_path()):
        return None
    try:
        _write_anchor(len(hashes), hashes[-1] if hashes else "")
    except OSError:
        return "api_anchor_write_failed"
    return None


def _chain_hash(prev: str, row: dict) -> str:
    return hashlib.sha256((prev + "|" + json.dumps(row, sort_keys=True)).encode()).hexdigest()


def _chain_problem(c) -> Optional[str]:
    """Why the chain cannot be trusted, or None: refuse rather than count from bad data."""
    have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if not (set(_CHAIN_TRIGGERS) | set(_PROOF_TRIGGERS)) <= have:
        return "api_ledger_tampered"
    why, hashes = _chain_rows_problem(c)
    if why or not _anchor_problem(hashes):
        return why
    from core import agent_control as ac
    return "api_ledger_tampered" if ac._owner_key() is not None else "owner_proof_unconfigured"


def _chain_rows_problem(c) -> tuple:
    """(why the rows themselves are damaged or None, the row hashes in order)."""
    prev, hashes = "", []
    for ts, session, project_dir, model, risk, actor, nonce, row_prev, row_hash in c.execute(
            "SELECT ts, session, project_dir, model, risk, actor, nonce, prev_hash, hash FROM"
            " agent_auth_api_chain ORDER BY seq"):
        row = {"ts": ts, "session": session, "project_dir": project_dir, "model": model,
               "risk": risk, "actor": actor, "nonce": nonce}
        if row_prev != prev or row_hash != _chain_hash(prev, row):
            return "api_ledger_tampered", hashes
        prev = row_hash
        hashes.append(row_hash)
    spent = c.execute("SELECT COUNT(*) FROM owner_proof_used WHERE action=?",
                      (API_PROOF_ACTION,)).fetchone()[0]
    return ("api_ledger_tampered" if spent > len(hashes) else None), hashes


def commit_api_launch(*, session: str, project_dir: str, model: str, risk: str = "",
                      actor: str = "", proof: Optional[dict] = None,
                      conversation_id: Optional[str] = None,
                      launcher: str = DEFAULT_LAUNCHER, conn=None, now: Optional[float] = None,
                      verify_proof: Optional[Callable] = None) -> str:
    """Verify and spend the owner proof and count the launch, one transaction. "" or why
    refused. Trusts nothing resolve() decided: the proof is checked again against this exact
    launch (audit R4). The caller holds the creation guard and starts the session only on ""."""
    c = _chain(_ledger(conn))
    ts = now if now is not None else time.time()
    try:
        with _anchor_lock():
            if c.in_transaction:
                c.commit()
            c.execute("BEGIN IMMEDIATE")
            try:
                problem = _chain_problem(c) or _close_anchor_lag(c) or _api_proof_problem(
                    proof, session, project_dir, model, risk, conversation_id, launcher, c, now,
                    verify_proof)
                if problem:
                    c.rollback()
                    return problem
                nonce = str(proof.get("nonce"))
                r = c.execute("SELECT hash FROM agent_auth_api_chain ORDER BY seq DESC LIMIT 1"
                              ).fetchone()
                prev = r[0] if r else ""
                row = {"ts": ts, "session": session, "project_dir": project_dir, "model": model,
                       "risk": risk or "", "actor": actor or "", "nonce": nonce}
                head = _chain_hash(prev, row)
                c.execute("INSERT INTO agent_auth_api_chain (ts, session, project_dir, model, risk,"
                          " actor, nonce, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
                          (ts, session, project_dir, model, row["risk"], row["actor"], nonce, prev,
                           head))
                rows = c.execute("SELECT COUNT(*) FROM agent_auth_api_chain").fetchone()[0]
                c.execute("INSERT INTO owner_proof_used VALUES (?,?,?,?)",
                          (nonce, API_PROOF_ACTION, session,
                           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))))
                c.execute("INSERT INTO agent_auth_launch (ts, session, project_dir, mode, model,"
                          " risk, actor) VALUES (?,?,?,?,?,?,?)",
                          (ts, session, project_dir, API, model, row["risk"], row["actor"]))
                c.commit()
            except sqlite3.IntegrityError:
                c.rollback()
                return "owner_proof_replayed"
            except BaseException:
                c.rollback()
                raise
            try:
                _write_anchor(rows, head)
            except OSError:
                return "api_anchor_write_failed"      # counted and spent; start nothing
            return ""
    except OSError:                                   # the lock file: nothing spent
        return "api_anchor_write_failed"
    finally:
        if conn is None:
            c.close()


def reanchor(conn=None, *, accept_chain: bool = False) -> dict:
    """Owner step: restore missing triggers (a DB from before them, or a reported drop) and
    re-sign the head. Refused without accept_chain unless the present anchor vouches for
    the chain: rows deleted with their nonces look intact, and only the anchor knows, so a
    key rotation, a shorter chain or a missing anchor is the owner's explicit judgement.
    Rows that are themselves damaged are always refused. Every change is audited."""
    from core import agent_control as ac
    c = _chain(_ledger(conn))
    try:
        if ac._owner_key() is None:
            return {"ok": False, "reason": "owner_proof_unconfigured"}
        with _anchor_lock():
            if c.in_transaction:
                c.commit()
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute(_PROOF_TABLE)
                why, hashes = _chain_rows_problem(c)
                if why:
                    c.rollback()
                    return {"ok": False, "reason": why}
                before = _anchored_rows()
                vouched = not _anchor_problem(hashes)
                if not (vouched or accept_chain):
                    c.rollback()
                    return {"ok": False, "reason": "api_chain_needs_owner_review",
                            "rows": len(hashes), "anchored_rows_before": before}
                have = {r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'")}
                missing = sorted((set(_CHAIN_TRIGGERS) | set(_PROOF_TRIGGERS)) - have)
                _chain_triggers(c)
                _proof_triggers(c)
                c.commit()
            except BaseException:
                c.rollback()
                raise
            result = {"ok": True, "rows": len(hashes), "restored_triggers": missing,
                      "anchored_rows_before": before, "anchor_vouched": vouched}
            try:
                _write_anchor(len(hashes), hashes[-1] if hashes else "")
            except OSError:
                result.update(ok=False, reason="api_anchor_write_failed")
            ac.audit("api_chain_reanchor", "agent_auth_api_chain", **result)
            return result
    except OSError:
        return {"ok": False, "reason": "api_anchor_write_failed"}
    finally:
        if conn is None:
            c.close()


def record_launch(*, session: str, project_dir: str, mode: str, model: str = "",
                  risk: str = "", actor: str = "", conn=None,
                  now: Optional[float] = None) -> None:
    c = _ledger(conn)
    try:
        c.execute("INSERT INTO agent_auth_launch (ts, session, project_dir, mode, model, risk,"
                  " actor) VALUES (?,?,?,?,?,?,?)",
                  (now if now is not None else time.time(), session, project_dir, mode,
                   model, risk, actor or ""))
        c.commit()
    finally:
        if conn is None:
            c.close()


def last_mode(session: str, conn=None) -> Optional[str]:
    c = _ledger(conn)
    try:
        r = c.execute("SELECT mode FROM agent_auth_launch WHERE session=? ORDER BY id DESC"
                      " LIMIT 1", (session,)).fetchone()
        return r[0] if r else None
    finally:
        if conn is None:
            c.close()


def _api_usage(live_sessions: set, conn=None, now: Optional[float] = None) -> dict:
    """Counted from the append-only chain. agent_auth_launch can only raise a count: losing
    its rows resets nothing, and it still says when a session later moved to subscription."""
    c = _chain(_ledger(conn))
    try:
        now = now if now is not None else time.time()
        day = max(c.execute("SELECT COUNT(*) FROM agent_auth_launch WHERE mode=? AND ts>?",
                            (API, now - 86400)).fetchone()[0],
                  c.execute("SELECT COUNT(*) FROM agent_auth_api_chain WHERE ts>?",
                            (now - 86400,)).fetchone()[0])
        latest: dict = {}
        for session, mode, ts in c.execute("SELECT session, mode, ts FROM agent_auth_launch"
                                           " ORDER BY id"):
            latest[session] = (mode, ts)
        for session, ts in c.execute("SELECT session, MAX(ts) FROM agent_auth_api_chain"
                                     " GROUP BY session"):
            if session not in latest or latest[session][1] < ts:
                latest[session] = (API, ts)
        live = sorted(s for s, (m, _) in latest.items() if m == API and s in live_sessions)
        return {"launches_24h": day, "live_api_sessions": live}
    finally:
        if conn is None:
            c.close()


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


_MODEL_RE = re.compile(r"^[a-z0-9.\-]{1,40}$")


def api_launch_text(session: str, project_dir: str, model: str, risk: Optional[str] = None,
                    conversation_id: Optional[str] = None,
                    launcher: str = DEFAULT_LAUNCHER) -> str:
    """The exact launch an owner proof authorises. Sign and verify the same bytes."""
    return json.dumps({"session": session, "project_dir": os.path.realpath(project_dir),
                       "model": model, "risk": risk or "",
                       "conversation_id": conversation_id or "", "launcher": launcher},
                      sort_keys=True)


def _api_proof_problem(proof, session: str, project_dir: str, model: str, risk, conversation_id,
                       launcher: str, conn, now, verify_proof) -> Optional[str]:
    """Why this proof cannot authorise this launch, or None. Spends nothing."""
    if not isinstance(proof, dict):       # never a string, an actor name or agent text
        return "api_owner_proof_required"
    if verify_proof is None:
        from core import agent_control as ac
        verify_proof = ac.check_owner_proof
    why = verify_proof(proof, API_PROOF_ACTION, session,
                       api_launch_text(session, project_dir, model, risk, conversation_id,
                                       launcher), now=now)
    if why:
        return why
    c = _chain(_ledger(conn))
    try:
        nonce = str(proof.get("nonce"))
        if c.execute("SELECT 1 FROM owner_proof_used WHERE nonce=? UNION SELECT 1 FROM"
                     " agent_auth_api_chain WHERE nonce=?", (nonce, nonce)).fetchone():
            return "owner_proof_replayed"
    finally:
        if conn is None:
            c.close()
    return None


def resolve(session: str, project_dir: str, *, requested: Optional[str] = None,
            model: Optional[str] = None, risk: Optional[str] = None,
            live_sessions: Optional[Callable[[], set]] = None,
            config: Optional[dict] = None, conn=None,
            now: Optional[float] = None, conversation_id: Optional[str] = None,
            owner_proof: Optional[dict] = None,
            verify_proof: Optional[Callable] = None) -> dict:
    """Decide how a NEW session starts. Returns
    {"ok", "mode", "model", "argv" (the program + args for tmux), "reason", ...}.
    `ok=False` means: start nothing (never "start it on subscription instead")."""
    mode = (requested or SUBSCRIPTION).strip().lower()
    if mode not in MODES:
        return {"ok": False, "reason": f"unknown_auth_mode:{mode[:20]}"}
    if mode == SUBSCRIPTION:
        if model and model != DEFAULT_MODEL and not (model == HIGH_RISK_MODEL and risk == "high"):
            return {"ok": False, "reason": "model_not_allowed"}
        extra = ["--model", model] if model else []
        return {"ok": True, "mode": SUBSCRIPTION, "model": model or "",
                "argv": subscription_argv(extra), "reason": "default_subscription"}

    # ── API: explicit opt-in, every check must pass ─────────────────────────────
    cfg = config if config is not None else load_config()
    if cfg.get("_error"):
        return {"ok": False, "reason": cfg["_error"]}
    if cfg.get("enabled") is not True:
        return {"ok": False, "reason": "api_mode_disabled"}
    entry = (cfg.get("agents") or {}).get(session)
    if not isinstance(entry, dict):
        return {"ok": False, "reason": "agent_not_opted_in"}
    want_dir = os.path.realpath(str(entry.get("project_dir") or ""))
    if not entry.get("project_dir") or want_dir != os.path.realpath(project_dir):
        return {"ok": False, "reason": "project_dir_mismatch"}
    use = (model or DEFAULT_MODEL).strip().lower()
    if not _MODEL_RE.match(use):
        return {"ok": False, "reason": "model_invalid"}
    if use == HIGH_RISK_MODEL:
        if risk != "high" or entry.get("allow_opus_for_high_risk") is not True:
            return {"ok": False, "reason": "opus_requires_high_risk_and_entry_permission"}
    elif use != DEFAULT_MODEL:
        return {"ok": False, "reason": "model_not_allowed"}
    launcher = str(cfg.get("launcher") or DEFAULT_LAUNCHER)
    problem = _launcher_problem(launcher)
    if problem:
        return {"ok": False, "reason": problem}
    limits = cfg.get("limits") or {}
    max_live = _int(limits.get("max_live_api_agents"), 0)
    max_day = _int(limits.get("max_api_launches_per_day"), 0)
    if max_live < 1 or max_day < 1:
        return {"ok": False, "reason": "api_limits_not_configured"}
    if live_sessions is None:
        return {"ok": False, "reason": "live_population_unknown"}
    try:
        live = set(live_sessions())
    except Exception:  # noqa: BLE001 — unknown population: refuse
        return {"ok": False, "reason": "live_population_unknown"}
    c = _chain(_ledger(conn))
    try:
        tampered = _chain_problem(c)
    finally:
        if conn is None:
            c.close()
    if tampered:
        return {"ok": False, "reason": tampered}
    usage = _api_usage(live, conn=conn, now=now)
    if len(usage["live_api_sessions"]) >= max_live:
        return {"ok": False, "reason": "api_live_agent_cap_reached", **usage}
    if usage["launches_24h"] >= max_day:
        return {"ok": False, "reason": "api_daily_launch_cap_reached", **usage}
    why = _api_proof_problem(owner_proof, session, project_dir, use, risk, conversation_id,
                             launcher, conn, now, verify_proof)
    if why:
        return {"ok": False, "reason": why, **usage}
    return {"ok": True, "mode": API, "model": use, "argv": [launcher, "--model", use],
            "launcher": launcher,
            "reason": "explicit_api_opt_in", **usage}


# F7: panes can drop variables, but a settings file can bring a credential back
_SETTINGS_CREDENTIAL_KEYS = ("apiKeyHelper", "awsCredentialExport", "awsAuthRefresh")
_SETTINGS_MAX_BYTES = 1 << 20


def settings_guard_enforced() -> bool:
    """Off by default: a host whose settings use one of these keeps starting sessions until
    its owner has seen what the audit field reports. `enforce` refuses the launch."""
    return os.getenv("AGENT_AUTH_SETTINGS_GUARD", "").strip().lower() == "enforce"


def settings_api_overrides(project_dir: str, config_dir: Optional[str] = None) -> list:
    """`<file>:<key>` for each setting that could put a session on API billing. Names only:
    a value here may be a secret, so it is never returned, logged or stored. A file that
    cannot be read counts: its content is unknown."""
    base = config_dir or os.getenv("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    files = [os.path.join(base, "settings.json"),
             os.path.join(project_dir, ".claude", "settings.json"),
             os.path.join(project_dir, ".claude", "settings.local.json"),
             "/etc/claude-code/managed-settings.json"]
    found = []
    for path in files:
        if not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) > _SETTINGS_MAX_BYTES:
                raise ValueError("oversized")
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:      # an agent can write this file: no content may stop a launch
            found.append(f"{path}:unreadable")
            continue
        if not isinstance(data, dict):
            found.append(f"{path}:unreadable")
            continue
        found += [f"{path}:{k}" for k in _SETTINGS_CREDENTIAL_KEYS if data.get(k)]
        env = data.get("env")
        if isinstance(env, dict):
            found += [f"{path}:env.{k}" for k in sorted(env) if is_api_env_var(str(k))]
    return found


_PLAIN_CLAUDE_RE = re.compile(r"\Aclaude(?:[ \t]+[A-Za-z0-9_./=:@%+,-]+)*\Z")


def recovery_command(session: str, shape_cmd: str, conn=None) -> dict:
    """Session recovery's command, made safe: never API, never an inherited key.
    {"ok", "cmd"} or {"ok": False, "reason"}."""
    launcher_names = {os.path.basename(DEFAULT_LAUNCHER)}
    try:
        cfg = load_config()
        if cfg.get("launcher"):
            launcher_names.add(os.path.basename(str(cfg["launcher"])))
    except Exception:  # noqa: BLE001
        pass
    if any(n and n in shape_cmd for n in launcher_names) or "ANTHROPIC_API_KEY" in shape_cmd:
        return {"ok": False, "reason": "recovery_never_starts_api_mode"}
    try:
        prior = last_mode(session, conn=conn)
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "auth_ledger_unreadable"}
    if prior == API:
        return {"ok": False, "reason": "api_session_needs_explicit_relaunch"}
    cmd = shape_cmd.strip()
    # F4: fail closed. Only a plain `claude …` can be wrapped with `env -u …`; a prefix
    # (`cd x && claude`, `FOO=1 claude`, an absolute path, `bash -lc …`) would keep the
    # inherited credential, so it is refused instead of passed through.
    if not _PLAIN_CLAUDE_RE.match(cmd):
        return {"ok": False, "reason": "recovery_shape_not_plain_claude"}
    return {"ok": True, "cmd": subscription_shell(cmd)}
