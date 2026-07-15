"""Regression tests for the hardened backup stage + orphan reaping (incident:
Runtime job 27 / OwnerTask 94 stuck forever in `backing_up` — worker died mid
backup, no error, no terminal transition, no periodic reaper).

Covers: successful backup, progress heartbeat, timeout, dead child, orphaned
child, no-progress detection, incomplete-temp cleanup, dirty-workspace
preservation, service-restart recovery, read-only light snapshot, no duplicate
job, and no-infinite-backing_up (reaper)."""
import os
import subprocess
import tarfile
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("RUNTIME_DB", os.path.join(tempfile.gettempdir(), "rt_backup_jobs.db"))

from core import backup_engine as be  # noqa: E402
from core.backup_engine import BackupEngine, BackupError  # noqa: E402
from core import job_store  # noqa: E402


def setup_module(_m):
    try:
        os.remove(os.environ["RUNTIME_DB"])
    except FileNotFoundError:
        pass
    job_store.init_db()


def _repo(tmp_path, files=("a.py", "b.py", "c.txt"), git=True):
    repo = tmp_path / "proj"
    repo.mkdir()
    for f in files:
        (repo / f).write_text(f"content of {f}\n")
    if git:
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "-m", "init"], check=True, capture_output=True)
    return repo


class _FakeTar:
    """Stand-in for tarfile so timeout/dead/orphaned paths are deterministic."""
    def __init__(self, on_addfile):
        self._on_addfile = on_addfile
        self.added = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def gettarinfo(self, abspath, arcname=None):
        return types.SimpleNamespace(isreg=lambda: False, name=arcname)

    def addfile(self, ti, fh=None):
        self._on_addfile(self)
        self.added += 1


# ── 1: successful backup (atomic, no leftover temp) ──────────────────────────

def test_successful_backup(tmp_path):
    repo = _repo(tmp_path)
    eng = BackupEngine(str(repo))
    meta = eng.snapshot(reason="test")
    arc = repo / ".ai-runtime-backups" / meta["archive"]
    assert arc.exists() and meta["size"] > 0 and meta["files"] >= 3
    assert tarfile.is_tarfile(arc)
    # no leftover .tmp
    assert not any(p.name.endswith(".tmp") for p in (repo / ".ai-runtime-backups").iterdir())


# ── 2: progress heartbeat (progress_cb fires with measurable fields) ─────────

class _WritingFakeTar:
    """Writes a real temp file (so atomic os.replace succeeds) but sleeps per
    addfile, so the bounded monitor loop deterministically ticks progress."""
    def __init__(self, path, per_addfile_sleep=0.03):
        self._f = open(path, "wb")
        self._sleep = per_addfile_sleep

    def __enter__(self):
        self._f.write(b"HDR")
        return self

    def __exit__(self, *a):
        self._f.close()
        return False

    def gettarinfo(self, abspath, arcname=None):
        return types.SimpleNamespace(isreg=lambda: False, name=arcname)

    def addfile(self, ti, fh=None):
        time.sleep(self._sleep)
        self._f.write(b"X" * 10)


def test_progress_heartbeat(tmp_path, monkeypatch):
    repo = _repo(tmp_path, files=tuple(f"f{i}.py" for i in range(6)))
    eng = BackupEngine(str(repo))
    monkeypatch.setattr(be, "_PROGRESS_INTERVAL", 0.01)
    monkeypatch.setattr(be.tarfile, "open", lambda *a, **k: _WritingFakeTar(a[0]))

    seen = []
    meta = eng.snapshot(reason="test", progress_cb=lambda p: seen.append(p))
    assert meta["files"] == 6
    assert len(seen) >= 1
    assert set(seen[-1].keys()) >= {"files", "bytes", "elapsed"}
    # files count is measurable/advancing across the run
    assert seen[-1]["files"] >= seen[0]["files"]


# ── 3: timeout (bounded; aborts instead of hanging) ──────────────────────────

def test_timeout_aborts(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    eng = BackupEngine(str(repo))
    monkeypatch.setattr(eng, "_members", lambda light: [(f"x{i}", f"./x{i}") for i in range(1000)])
    monkeypatch.setattr(be.tarfile, "open", lambda *a, **k: _FakeTar(lambda t: time.sleep(0.02)))
    monkeypatch.setattr(be, "_PROGRESS_INTERVAL", 0.01)
    t0 = time.monotonic()
    with pytest.raises(BackupError):
        eng.snapshot(reason="test", timeout=0.2)
    assert time.monotonic() - t0 < 8  # bounded, not hung


# ── 4: dead child (worker raises -> precise terminal error) ──────────────────

def test_dead_child(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    eng = BackupEngine(str(repo))
    monkeypatch.setattr(eng, "_members", lambda light: [("x", "./x")])

    def boom(_t):
        raise RuntimeError("archive write exploded")
    monkeypatch.setattr(be.tarfile, "open", lambda *a, **k: _FakeTar(boom))
    with pytest.raises(BackupError, match="backup failed"):
        eng.snapshot(reason="test", timeout=5)
    assert not any(p.name.endswith(".tmp") for p in (repo / ".ai-runtime-backups").iterdir())


# ── 5: orphaned child (won't stop after abort -> orphaned error, bounded) ────

def test_orphaned_child(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    eng = BackupEngine(str(repo))
    monkeypatch.setattr(eng, "_members", lambda light: [("x", "./x")])
    # addfile blocks far longer than timeout+grace and ignores abort
    monkeypatch.setattr(be.tarfile, "open", lambda *a, **k: _FakeTar(lambda t: time.sleep(30)))
    monkeypatch.setattr(be, "_ABORT_GRACE", 0.5)
    monkeypatch.setattr(be, "_PROGRESS_INTERVAL", 0.1)
    t0 = time.monotonic()
    with pytest.raises(BackupError, match="orphaned|did not stop"):
        eng.snapshot(reason="test", timeout=0.3)
    assert time.monotonic() - t0 < 8


# ── 6: no-progress detection (stalled worker still emits progress, then bounded)

def test_no_progress_detection(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    eng = BackupEngine(str(repo))
    monkeypatch.setattr(eng, "_members", lambda light: [("x", "./x")])
    monkeypatch.setattr(be.tarfile, "open", lambda *a, **k: _FakeTar(lambda t: time.sleep(30)))
    monkeypatch.setattr(be, "_ABORT_GRACE", 0.3)
    monkeypatch.setattr(be, "_PROGRESS_INTERVAL", 0.05)
    seen = []
    with pytest.raises(BackupError):
        eng.snapshot(reason="test", timeout=0.3, progress_cb=lambda p: seen.append(p["files"]))
    # progress fired repeatedly but file count never advanced -> no progress
    assert len(seen) >= 2 and set(seen) == {0}


# ── 7: incomplete temporary archive cleanup ──────────────────────────────────

def test_incomplete_temp_cleanup_on_entry(tmp_path):
    repo = _repo(tmp_path)
    bdir = repo / ".ai-runtime-backups"
    bdir.mkdir(exist_ok=True)
    stale = bdir / "backup_20000101_000000_000000.tar.gz.tmp"
    stale.write_bytes(b"partial garbage")
    eng = BackupEngine(str(repo))
    eng.snapshot(reason="test")            # cleans stale temp on entry
    assert not stale.exists()
    assert not any(p.name.endswith(".tmp") for p in bdir.iterdir())


def test_failed_backup_removes_its_temp_and_keeps_last_valid(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    eng = BackupEngine(str(repo))
    good = eng.snapshot(reason="good")     # a valid last backup
    good_arc = repo / ".ai-runtime-backups" / good["archive"]
    monkeypatch.setattr(eng, "_members", lambda light: [("x", "./x")])
    monkeypatch.setattr(be.tarfile, "open", lambda *a, **k: _FakeTar(lambda t: (_ for _ in ()).throw(OSError("disk full"))))
    with pytest.raises(BackupError):
        eng.snapshot(reason="bad", timeout=5)
    assert good_arc.exists()               # last valid backup preserved
    assert not any(p.name.endswith(".tmp") for p in (repo / ".ai-runtime-backups").iterdir())


# ── 8: dirty workspace preservation ──────────────────────────────────────────

def test_dirty_workspace_preserved(tmp_path):
    repo = _repo(tmp_path)
    dirty = repo / "LOCAL_WIP.txt"
    dirty.write_text("uncommitted operator work\n")
    tracked = repo / "a.py"
    before = tracked.read_text()
    BackupEngine(str(repo)).snapshot(reason="test")
    # backup never mutates/resets the workspace
    assert dirty.exists() and dirty.read_text() == "uncommitted operator work\n"
    assert tracked.read_text() == before


# ── 9: service restart during backup -> recover_interrupted re-approval ───────

def test_service_restart_during_backup(tmp_path):
    j = job_store.create_job(project_path=str(tmp_path), goal="g", status="backing_up")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    job_store.update_job(j["id"], heartbeat_at=stale)
    n = job_store.recover_interrupted()
    assert n >= 1
    got = job_store.get_job(j["id"])
    assert got["status"] == "waiting_approval"
    assert "interrupted during 'backing_up'" in got["error"]


# ── 10: read-only task -> lightweight snapshot (git-tracked subset only) ─────

def test_read_only_light_snapshot(tmp_path):
    repo = _repo(tmp_path, files=("src.py", "keep.txt"))
    # an untracked large blob that a FULL backup would include but LIGHT must not
    (repo / "huge_untracked.bin").write_text("x" * 100000)
    eng = BackupEngine(str(repo))
    full = eng.snapshot(reason="full", light=False)
    light = eng.snapshot(reason="light", light=True)
    assert light["light"] is True and full["light"] is False
    light_arc = repo / ".ai-runtime-backups" / light["archive"]
    with tarfile.open(light_arc) as t:
        names = {n.lstrip("./") for n in t.getnames()}
    assert "src.py" in names and "keep.txt" in names
    assert "huge_untracked.bin" not in names          # untracked excluded from light
    assert light["files"] < full["files"]


# ── 11: no duplicate Runtime job created by recovery/reaping ──────────────────

def test_no_duplicate_job_on_reaping(tmp_path):
    before = len(job_store.list_jobs(limit=1000))
    j = job_store.create_job(project_path=str(tmp_path), goal="g", status="backing_up")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    job_store.update_job(j["id"], heartbeat_at=stale)
    job_store.reap_orphaned()
    after = len(job_store.list_jobs(limit=1000))
    assert after == before + 1              # only the one we created; reaping made no new rows


# ── 12: no infinite backing_up (reaper transitions stale -> failed terminal) ──

def test_no_infinite_backing_up(tmp_path):
    j = job_store.create_job(project_path=str(tmp_path), goal="g", status="backing_up")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    job_store.update_job(j["id"], heartbeat_at=stale)
    n = job_store.reap_orphaned()
    assert n >= 1
    got = job_store.get_job(j["id"])
    assert got["status"] == "failed"
    assert "orphaned" in got["error"] and "backing_up" in got["error"]
    assert got["finished_at"] is not None
    # a job with a FRESH heartbeat is never reaped
    j2 = job_store.create_job(project_path=str(tmp_path), goal="g2", status="backing_up")
    job_store.touch_heartbeat(j2["id"])
    job_store.reap_orphaned()
    assert job_store.get_job(j2["id"])["status"] == "backing_up"
