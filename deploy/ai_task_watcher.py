#!/usr/bin/env python3
"""Fallback poller for the event-driven command bus (cloudrec/ai-dev-runtime).

DETERMINISTIC + ZERO LLM. GitHub webhooks are the primary trigger; this runs only
every 10 minutes as a safety net. It lists open `ai-task` issues + comments via the
GitHub API (local code only), then hands any NEW events to the backend command bus,
which deduplicates and decides. On an empty poll it still posts an empty batch so the
bus records a free cycle (model_calls=0, token_est=0). It never calls an LLM itself.
"""
import json
import os
import re
import time
import urllib.request
import urllib.error

REPO = "cloudrec/ai-dev-runtime"
LABEL = "ai-task"
STATE_DIR = "/var/lib/ai-task-watcher"
SEEN = os.path.join(STATE_DIR, "seen.json")
LOG = os.path.join(STATE_DIR, "watcher.log")
BUS_URL = "http://localhost:8088/api/webhooks/github/poll"
BUS_SECRET_FILE = "/root/.bus_secret"
UA = "ai-task-poller/2.0"


def log(msg):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")


def gh_token():
    t = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if t:
        return t.strip()
    try:
        for line in open("/root/.git-credentials"):
            m = re.search(r"https://[^:]+:([^@]+)@github\.com", line.strip())
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def bus_secret():
    try:
        return open(BUS_SECRET_FILE).read().strip()
    except Exception:
        return None


def gh(path, token):
    req = urllib.request.Request("https://api.github.com" + path, headers={
        "Authorization": "token " + token, "User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def load_seen():
    try:
        return json.load(open(SEEN))
    except Exception:
        return {"issues": {}}


def save_seen(s):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = SEEN + ".tmp"
    json.dump(s, open(tmp, "w"))
    os.replace(tmp, SEEN)


def post_bus(events, secret):
    data = json.dumps({"events": events}).encode()
    req = urllib.request.Request(BUS_URL, data=data, method="POST", headers={
        "Content-Type": "application/json", "X-Bus-Secret": secret, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    token = gh_token()
    secret = bus_secret()
    if not token or not secret:
        log("ERROR missing github token or bus secret")
        return
    seen = load_seen()
    events = []
    try:
        issues = gh(f"/repos/{REPO}/issues?state=open&labels={LABEL}&per_page=50", token)
    except urllib.error.HTTPError as e:
        log(f"ERROR list issues HTTP {e.code}")
        return
    for iss in issues:
        if "pull_request" in iss:
            continue
        num = iss["number"]
        key = str(num)
        st = seen["issues"].setdefault(key, {"seen": False, "last_comment_id": 0})
        # normalize legacy entries from the old watcher format (no "seen" key = already handled)
        if "seen" not in st:
            st["seen"] = True
        st.setdefault("last_comment_id", 0)
        labels = [l.get("name", "") for l in (iss.get("labels") or [])]
        if not st["seen"]:
            events.append({"delivery_id": None, "event_type": "issues", "action": "opened",
                           "issue_number": num, "comment_id": None, "title": iss.get("title"),
                           "body": iss.get("body"), "labels": labels})
            st["seen"] = True
        try:
            comments = gh(f"/repos/{REPO}/issues/{num}/comments?per_page=100", token)
        except urllib.error.HTTPError:
            comments = []
        for c in comments:
            if c["id"] > st["last_comment_id"]:
                events.append({"delivery_id": None, "event_type": "issue_comment", "action": "created",
                               "issue_number": num, "comment_id": c["id"], "title": iss.get("title"),
                               "body": c.get("body"), "labels": labels})
                st["last_comment_id"] = c["id"]
    # Hand off to the bus. An ai-task is acknowledged only after both
    # OwnerTask and Runtime job IDs are returned.
    try:
        res = post_bus(events, secret)
        decisions = res.get("decisions") or []

        if len(decisions) != len(events):
            raise RuntimeError(
                f"bus returned {len(decisions)} decisions for {len(events)} events"
            )

        for raw, decision in zip(events, decisions):
            labels_lower = [
                str(label).lower() for label in (raw.get("labels") or [])
            ]
            is_task = (
                raw.get("event_type") == "issues"
                and raw.get("action") in ("opened", "reopened", "labeled")
                and "ai-task" in labels_lower
            )

            if is_task:
                task_id = decision.get("task_id")
                runtime_job_id = decision.get("runtime_job_id")

                if not task_id or not runtime_job_id:
                    raise RuntimeError(
                        "ai-task was not accepted: "
                        f"issue={raw.get('issue_number')} "
                        f"status={decision.get('status')} "
                        f"task_id={task_id} "
                        f"runtime_job_id={runtime_job_id} "
                        f"error={decision.get('error')}"
                    )

        save_seen(seen)

        log(
            f"poll: {len(events)} new events -> cycle {res.get('cycle_id')} "
            f"model_calls={res.get('model_calls')} "
            f"token_est={res.get('token_est')}"
        )

    except urllib.error.HTTPError as e:
        log(f"ERROR bus post HTTP {e.code}")
        raise
    except Exception as e:
        log(f"ERROR bus acknowledgement failed: {e}")
        raise


if __name__ == "__main__":
    main()
