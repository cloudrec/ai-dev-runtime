"""Code under test must import from its OWN checkout, never from the live tree.

Runtime job #136 (task 326) ran the suite in an isolated worktree of `main` and got
30 failures in test_cdp_composer / test_wake_assistant_proof /
test_wake_delivery_verification. The job's only change was one Markdown plan file.
Cause: tools/wake_companion.py, tools/cdp_composer.py, hooks/owneros_hook.py and
several tests prepended the hard-coded live repo path to sys.path, so once any of them
was imported, every not-yet-imported module (core.wake_bridge, cdp_composer, ...)
resolved to the live, possibly newer and dirty, tree. Old tests then exercised new
code. Production is unaffected by resolving the root from __file__: every entrypoint
(systemd unit, Claude hooks) runs the file in place under /root/ai-dev-runtime.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIVE = "/root/ai-dev-runtime"
_INSERT_LIVE = re.compile(r"""sys\.path\.insert\(\s*0\s*,\s*["']/root/ai-dev-runtime""")


def _py_files():
    for sub in ("api", "cli", "core", "hooks", "integrations", "runtime", "tools", "tests"):
        base = os.path.join(_ROOT, sub)
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(dirpath, f)


def test_no_source_file_puts_the_live_tree_on_sys_path():
    offenders = []
    for path in _py_files():
        with open(path, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if _INSERT_LIVE.search(line):
                    offenders.append(f"{os.path.relpath(path, _ROOT)}:{n}")
    assert offenders == [], "hard-coded live-tree sys.path inserts: " + ", ".join(offenders)


# A test that loads a module or config by absolute live path (spec_from_file_location,
# open(), subprocess of the hook) tests the live tree, not the change under review:
# job #136's test_wake_delivery_verification loaded the live cdp_composer.py this way.
_LIVE_SOURCE = re.compile(
    r"""["']/root/ai-dev-runtime/(tools|core|hooks|api|cli|config|runtime|integrations)/[^"']*\.(py|ya?ml)["']""")


def test_no_test_loads_code_or_config_from_the_live_tree():
    offenders = []
    tests_dir = os.path.join(_ROOT, "tests")
    for f in sorted(os.listdir(tests_dir)):
        if not f.endswith(".py"):
            continue
        with open(os.path.join(tests_dir, f), encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if _LIVE_SOURCE.search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"tests/{f}:{n}")
    assert offenders == [], "tests load live-tree source: " + ", ".join(offenders)


def _probe(code: str) -> str:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=_ROOT, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_importing_the_companion_resolves_core_from_this_checkout():
    got = _probe(
        "import sys; sys.path[:0] = [%r, %r]\n"
        "import wake_companion\n"
        "import core.wake_bridge as wb; print(wb.__file__)" % (_ROOT, os.path.join(_ROOT, "tools"))
    )
    assert got.startswith(_ROOT + os.sep), got



_LIVE_CONST = re.compile(r"""^(\w+)\s*=\s*["']/root/ai-dev-runtime/?["']\s*$""", re.M)


def test_no_live_tree_constant_is_put_on_sys_path():
    # tools/stage_done.py: REPO = "/root/ai-dev-runtime"; sys.path.insert(0, REPO)
    offenders = []
    for path in _py_files():
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        for name in _LIVE_CONST.findall(src):
            if re.search(r"sys\.path\.insert\(\s*0\s*,\s*%s\b" % re.escape(name), src):
                offenders.append(f"{os.path.relpath(path, _ROOT)}:{name}")
    assert offenders == [], "live-tree constants on sys.path: " + ", ".join(offenders)
