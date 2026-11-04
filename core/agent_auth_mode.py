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
    resets nothing and a damaged chain refuses every API launch;
  * model: Sonnet by default; Opus only when the launch says `risk="high"` AND the
    agent's entry allows Opus for high-risk work; anything else is refused;
  * session recovery never starts API mode: a recorded API session is not revived
    (an explicit relaunch is required) and a resume command naming the API launcher
    is refused.

Not enforceable here (stated, not hidden): a dollar cap per interactive session — the
Claude CLI caps spend only in print mode. The hard money ceiling must be the API
workspace's own spend limit (owner, Anthropic console). And agents run as root on this
host: one that reads the owner key file, or rebuilds the chain after dropping its
triggers, gets past these checks. They stop mistakes and text-level spoofing, not a
hostile root process.
"""
from __future__ import annotations

import hashlib
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
    "CLAUDE_CODE_TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy",
    "https_proxy", "no_proxy", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR"})
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


def _chain(c):
    """The append-only API launch chain. Created once, table and triggers together, and
    never repaired afterwards: a dropped trigger must stay visible to _chain_problem."""
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
        for name, verb in _CHAIN_TRIGGERS.items():
            c.execute(f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {verb} ON"
                      " agent_auth_api_chain BEGIN SELECT RAISE(ABORT,"
                      " 'agent_auth_api_chain is append-only'); END")
        c.execute("CREATE TABLE IF NOT EXISTS owner_proof_used (nonce TEXT PRIMARY KEY, "
                  "action TEXT, target TEXT, used_at TEXT)")
        c.commit()
    except BaseException:
        c.rollback()
        raise
    return c


def _chain_hash(prev: str, row: dict) -> str:
    return hashlib.sha256((prev + "|" + json.dumps(row, sort_keys=True)).encode()).hexdigest()


def _chain_problem(c) -> Optional[str]:
    """Why the chain cannot be trusted, or None: refuse rather than count from bad data."""
    have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND"
                                    " tbl_name='agent_auth_api_chain'")}
    if not set(_CHAIN_TRIGGERS) <= have:
        return "api_ledger_tampered"
    prev, rows = "", 0
    for ts, session, project_dir, model, risk, actor, nonce, row_prev, row_hash in c.execute(
            "SELECT ts, session, project_dir, model, risk, actor, nonce, prev_hash, hash FROM"
            " agent_auth_api_chain ORDER BY seq"):
        row = {"ts": ts, "session": session, "project_dir": project_dir, "model": model,
               "risk": risk, "actor": actor, "nonce": nonce}
        if row_prev != prev or row_hash != _chain_hash(prev, row):
            return "api_ledger_tampered"
        prev, rows = row_hash, rows + 1
    spent = c.execute("SELECT COUNT(*) FROM owner_proof_used WHERE action=?",
                      (API_PROOF_ACTION,)).fetchone()[0]
    return "api_ledger_tampered" if spent > rows else None      # a dropped or emptied chain


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
        if c.in_transaction:
            c.commit()
        c.execute("BEGIN IMMEDIATE")
        try:
            problem = _chain_problem(c) or _api_proof_problem(
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
            c.execute("INSERT INTO agent_auth_api_chain (ts, session, project_dir, model, risk,"
                      " actor, nonce, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
                      (ts, session, project_dir, model, row["risk"], row["actor"], nonce, prev,
                       _chain_hash(prev, row)))
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
        return ""
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
