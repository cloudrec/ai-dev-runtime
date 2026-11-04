#!/usr/bin/env python3
"""Run the OFFLINE autonomous-cycle acceptance harness and write SHA-pinned evidence.

    python3 tools/offline_acceptance.py --out reports/offline_acceptance

Runs tests/test_offline_acceptance.py (fixture agent, fake ChatGPT page, deterministic
reviewer over the authenticated API, temp DBs; no network, no paid API, no live DB) and
writes <out>/acceptance_<sha>.json and .md: commit, dirty state, per-scenario verdict and
duration. Exit 0 only when every scenario passed. It changes nothing outside <out>.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = "tests/test_offline_acceptance.py"


def _git(*args) -> str:
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True,
                          timeout=30).stdout.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    sha = _git("rev-parse", "HEAD")
    dirty = [ln for ln in _git("status", "--porcelain").splitlines() if ln.strip()]
    os.makedirs(a.out, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        xml = os.path.join(tmp, "junit.xml")
        t0 = time.time()
        rc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                             MODULE, f"--junitxml={xml}"], cwd=ROOT,
                            timeout=1800).returncode
        took = round(time.time() - t0, 1)
        cases = []
        if os.path.exists(xml):
            for tc in ET.parse(xml).getroot().iter("testcase"):
                bad = [c.tag for c in tc if c.tag in ("failure", "error", "skipped")]
                cases.append({"scenario": tc.get("name"),
                              "result": bad[0] if bad else "passed",
                              "seconds": float(tc.get("time") or 0)})
    ok = rc == 0 and cases and all(c["result"] == "passed" for c in cases)
    report = {"commit": sha, "dirty": dirty, "module": MODULE, "pytest_exit": rc,
              "seconds": took, "ok": bool(ok), "scenarios": cases,
              "simulated": ["LLM agent (fixture script)", "ChatGPT page (fake CDP)",
                            "reviewer judgment (deterministic policy)",
                            "wall clock in the stall scenario"],
              "live_e2e_proven": False}
    base = os.path.join(a.out, f"acceptance_{sha[:7]}")
    with open(base + ".json", "w") as fh:
        json.dump(report, fh, indent=2)
    with open(base + ".md", "w") as fh:
        fh.write(f"# Offline acceptance @ {sha[:12]}\n\n"
                 f"- result: **{'PASS' if ok else 'FAIL'}** (pytest exit {rc}, {took}s)\n"
                 f"- dirty files at run: {len(dirty)}\n- live E2E proven: **no** "
                 "(agent, ChatGPT page, reviewer judgment simulated)\n\n"
                 "| scenario | result | s |\n|---|---|---|\n" +
                 "".join(f"| {c['scenario']} | {c['result']} | {c['seconds']:.1f} |\n"
                         for c in cases))
    print(json.dumps({"ok": bool(ok), "commit": sha[:12], "report": base + ".json"}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
