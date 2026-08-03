"""
Tests for scripts/launch_paper_run.py — the Windows Task Scheduler-based
persistent launcher for the continuous paper run.

No real scheduled task is created, run, or deleted: every schtasks call is
mocked. These tests pin the exact failure mode hit in production — an
unquoted /tr command line makes Task Scheduler split on the space in the repo
path ("polymarket-arb-bot (1)") and fail with ERROR_FILE_NOT_FOUND — so the
quoted form is locked in.
"""
import subprocess
from unittest.mock import patch

import scripts.launch_paper_run as launcher


def test_create_and_start_quotes_task_command(tmp_path):
    """The /tr path must be quoted — the repo path contains a space."""
    calls = []

    def fake_run(args, check=True, capture_output=True, text=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch("scripts.launch_paper_run._run", side_effect=fake_run):
        launcher.create_and_start("TestTask")

    create_args = [c for c in calls if c[1] == "/create"]
    assert len(create_args) == 1
    tr_idx = create_args[0].index("/tr")
    task_cmd = create_args[0][tr_idx + 1]
    # Quoted, and pointing at the batch launcher inside this repo.
    assert task_cmd.startswith('"') and task_cmd.endswith('"')
    assert "start_paper_bot.cmd" in task_cmd

    run_args = [c for c in calls if c[1] == "/run"]
    assert len(run_args) == 1
    assert run_args[0][-1] == "TestTask"


def test_stop_ends_and_deletes_task():
    calls = []

    def fake_run(args, check=True, capture_output=True, text=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch("scripts.launch_paper_run._run", side_effect=fake_run):
        launcher.stop("TestTask")

    verbs = [c[1] for c in calls]
    assert "/end" in verbs
    assert "/delete" in verbs


def test_stop_survives_missing_task(capsys):
    """Ending/deleting a task that doesn't exist must not raise."""
    def fake_run(args, check=True, capture_output=True, text=True):
        raise subprocess.CalledProcessError(1, args)

    with patch("scripts.launch_paper_run._run", side_effect=fake_run):
        launcher.stop("NoSuchTask")

    out = capsys.readouterr().out
    assert "not running" in out or "not exist" in out


def test_main_flag_stop():
    """--stop routes to stop(), not create_and_start()."""
    with patch("scripts.launch_paper_run.stop") as mock_stop, \
         patch("scripts.launch_paper_run.create_and_start") as mock_create, \
         patch("sys.argv", ["launch_paper_run.py", "--stop", "--task-name", "X"]):
        rc = launcher.main()
    assert rc == 0
    mock_stop.assert_called_once_with("X")
    mock_create.assert_not_called()
