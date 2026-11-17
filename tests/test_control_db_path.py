"""AGENT_CONTROL_DB is validated in one place (2026-11-17: a blank value made sqlite
create a file literally named '  ' in the working directory during a suite run)."""
from __future__ import annotations

import os

import pytest

from core import control_db


@pytest.mark.parametrize("bad", ["", "  ", "relative.db", " /abs/with-space.db",
                                 "/abs/trailing.db\n"])
def test_blank_relative_or_padded_values_are_refused(monkeypatch, bad):
    monkeypatch.setenv("AGENT_CONTROL_DB", bad)
    with pytest.raises(ValueError, match="absolute path"):
        control_db.control_db_path()


def test_unset_is_the_live_default_and_a_real_path_is_kept(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CONTROL_DB", raising=False)
    assert control_db.control_db_path() == control_db.DEFAULT_CONTROL_DB
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    assert control_db.control_db_path() == str(tmp_path / "ac.db")


def test_no_reader_opens_a_blank_path_in_the_working_directory(monkeypatch, tmp_path):
    from core import agent_control as ac
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_CONTROL_DB", "  ")
    with pytest.raises(ValueError):
        ac._db()
    assert os.listdir(tmp_path) == []


def test_every_core_reader_goes_through_the_validator():
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "core"
    raw = re.compile(r'getenv\(\s*"AGENT_CONTROL_DB"')
    # agent_privilege validates the value itself (blank/relative -> control_db_unset)
    exempt = {"control_db.py", "agent_privilege.py"}
    offenders = [str(f.relative_to(root)) for f in root.rglob("*.py")
                 if f.name not in exempt and raw.search(f.read_text())]
    assert offenders == [], offenders


def test_the_privilege_pin_never_lands_beside_the_cwd(monkeypatch, tmp_path):
    from core import agent_privilege as priv
    monkeypatch.chdir(tmp_path)
    for bad in ("  ", "rel.db"):
        monkeypatch.setenv("AGENT_CONTROL_DB", bad)
        with pytest.raises(priv.AgentPrivilegeError):
            priv._pin_path()
    assert os.listdir(tmp_path) == []
