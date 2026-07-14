"""job_executor._run_tests: `&&`-chained validation commands (issue #10 —
planner-produced `test -s file && echo OK` was silently failing because the
old code passed the literal string "&&" as an argv token to `test`)."""
import tempfile
import os

from core import job_executor


def test_single_command_still_works(tmp_path):
    out = job_executor._run_tests(str(tmp_path), ["python3 -c 'print(1)'"])
    assert out["ok"] is True and out["results"][0]["passed"] is True


def test_chained_command_both_steps_pass(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("hi")
    out = job_executor._run_tests(str(tmp_path), [f"test -s {f.name} && echo VALIDATION_OK"])
    assert out["ok"] is True
    assert "VALIDATION_OK" in out["results"][0]["output"]


def test_chained_command_short_circuits_on_first_failure(tmp_path):
    out = job_executor._run_tests(str(tmp_path), ["test -s missing_file.md && echo SHOULD_NOT_RUN"])
    assert out["ok"] is False
    assert "SHOULD_NOT_RUN" not in out["results"][0]["output"]


def test_never_invokes_a_real_shell(tmp_path):
    """`&&` must be handled by chaining argv-level subprocess calls, not by
    shell=True — prove a shell metacharacter in a later step is inert."""
    f = tmp_path / "y.md"
    f.write_text("hi")
    marker = tmp_path / "should_not_exist.txt"
    out = job_executor._run_tests(str(tmp_path), [f"test -s {f.name} && echo hi > {marker.name}"])
    # `>` is not shell-interpreted (shell=False, argv tokens) — echo just prints
    # "hi > should_not_exist.txt" as literal words, no redirection happens.
    assert out["ok"] is True
    assert not marker.exists()


def test_multiple_test_commands_each_independently_chained(tmp_path):
    f = tmp_path / "z.md"
    f.write_text("hi")
    out = job_executor._run_tests(str(tmp_path), [
        f"test -s {f.name} && echo first_ok",
        "test -s nope.md && echo second_should_fail",
    ])
    assert out["ok"] is False
    assert out["results"][0]["passed"] is True
    assert out["results"][1]["passed"] is False
