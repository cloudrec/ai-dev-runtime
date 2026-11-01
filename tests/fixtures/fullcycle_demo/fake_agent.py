"""A stand-in for an LLM coding agent, for the full-cycle tests and demo ONLY.

It sits in a real tmux pane and reads the one-line prompt the tmux adapter types, opens
the envelope that prompt names, and acts out the behaviour the scenario file gives for
that task key and attempt:

  correct       write the scenario's solution files, report done
  wrong         write a broken solution, report done (verification must catch it)
  outside       write the solution AND a file outside the task scope
  crash         exit without a result (the pane dies)
  permission    print a Claude-style permission dialog, wait for "1", then do `correct`
  blocked       report blocked with a question
  hang          do nothing (stays "working")
  exhausted     report context_exhausted with a compact handoff

Scenario JSON (path in $FULLCYCLE_FAKE_SCENARIO):
  {"behaviours": {"<key>": ["crash", "correct"]},       # per attempt; last repeats
   "solutions": {"<key>": {"<relpath>": "<content>"}},
   "broken":    {"<key>": {"<relpath>": "<content>"}}}
Every envelope received is appended to $FULLCYCLE_FAKE_LOG (one JSON line).
"""
import json
import os
import re
import sys

PROMPT_RE = re.compile(r"\[Owner OS task (\S+) attempt (\d+)\] Read the task envelope (\S+)")


def _write(root, files):
    for rel, content in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(content)


def _result(env, status, summary, changed, question=None, **extra):
    r = {"task_id": env["task_id"], "attempt": env["attempt"], "status": status,
         "summary": summary, "changed_files": changed, **extra}
    if question:
        r["question"] = question
    os.makedirs(os.path.dirname(env["result_path"]), exist_ok=True)
    tmp = env["result_path"] + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(r, fh)
    os.replace(tmp, env["result_path"])


def main():
    sc = json.load(open(os.environ["FULLCYCLE_FAKE_SCENARIO"]))
    log = os.environ.get("FULLCYCLE_FAKE_LOG")
    print("fake agent ready", flush=True)
    for line in sys.stdin:
        m = PROMPT_RE.search(line)
        if not m:
            continue
        env = json.load(open(m.group(3)))
        if log:
            with open(log, "a") as fh:
                fh.write(json.dumps({"task_id": env["task_id"], "attempt": env["attempt"],
                                     "feedback": env["feedback"]}) + "\n")
        if os.path.exists(env["result_path"]):
            print("duplicate envelope ignored", flush=True)
            continue
        key = env["task_id"].split(":", 1)[1]
        seq = sc.get("behaviours", {}).get(key, ["correct"])
        how = seq[min(env["attempt"], len(seq)) - 1]
        root = env["workdir"]
        sol = sc.get("solutions", {}).get(key, {})
        if how == "crash":
            print("fake agent: crashing", flush=True)
            sys.exit(3)
        if how == "hang":
            continue
        if how == "exhausted":
            _result(env, "context_exhausted", "out of context", [],
                    handoff=f"HANDOFF {key}: started, nothing written yet; next: write the file.")
            continue
        if how == "blocked":
            _result(env, "blocked", "need a decision", [], question="Integers only, or floats too?")
            continue
        if how == "permission":
            print("╭──────────────────────────╮\n│ Bash command\n│\n│   rm -rf build\n│\n"
                  "│ Do you want to proceed?\n│ ❯ 1. Yes\n│   2. No\n╰──────────────────────────╯",
                  flush=True)
            for ans in sys.stdin:
                if ans.strip() == "1":
                    break
            how = "correct"
        if how == "wrong":
            files = sc.get("broken", {}).get(key, {})
            _write(root, files)
            _result(env, "done", "implemented (wrongly)", list(files))
            continue
        files = dict(sol)
        if how == "outside":
            files["README.md"] = "tampered\n"
        _write(root, files)
        _result(env, "done", f"implemented {key}", list(files))
        print(f"fake agent: done {key} attempt {env['attempt']}", flush=True)


if __name__ == "__main__":
    main()
