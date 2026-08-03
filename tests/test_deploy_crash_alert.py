"""
Tests for the deploy/ systemd artifacts:

1. deploy/polymarket-bot.service must carry the restart/backoff directives
   (Restart=on-failure, RestartSec, StartLimit* cap) and wire the crash-alert
   ExecStopPost hook.
2. deploy/send_crash_alert.py must alert ONLY on abnormal exits (crash,
   signal, start-limit-hit) and stay silent on a clean systemctl stop —
   driven via env vars, with the Telegram alerter mocked so no test touches
   the network.
"""
import importlib.util
import logging
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"
SERVICE_FILE = DEPLOY_DIR / "polymarket-bot.service"
HOOK_FILE = DEPLOY_DIR / "send_crash_alert.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("send_crash_alert", HOOK_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hook():
    return _load_hook()


# -- the service file ----------------------------------------------------------


def test_service_file_has_restart_with_backoff():
    text = SERVICE_FILE.read_text()
    assert "Restart=on-failure" in text
    assert "RestartSec=" in text
    assert "StartLimitIntervalSec=" in text
    assert "StartLimitBurst=" in text


def test_service_file_runs_main_py_and_wires_crash_hook():
    text = SERVICE_FILE.read_text()
    # Runs the bot, not some wrapper.
    assert "ExecStart=" in text and "main.py" in text
    # Crash alert hook present.
    assert "ExecStopPost=" in text and "send_crash_alert.py" in text


# -- the crash-alert hook logic ------------------------------------------------


@pytest.mark.parametrize("service_result", ["exit-code", "signal", "core-dump", "timeout", "watchdog", "start-limit-hit"])
def test_should_alert_true_on_abnormal_exits(hook, service_result):
    assert hook.should_alert(service_result) is True


def test_should_alert_false_on_clean_stop(hook):
    assert hook.should_alert("success") is False


def test_should_alert_false_when_env_empty(hook):
    """An empty SERVICE_RESULT means this wasn't invoked by systemd (or
    predates the env vars) — stay silent rather than page on a false
    positive from a bare manual run."""
    assert hook.should_alert("") is False


def test_main_sends_critical_alert_on_crash(hook, monkeypatch):
    sent = []

    class FakeAlerter:
        async def send_alert(self, message, level):
            sent.append((message, level))

    monkeypatch.setattr(hook, "build_alerter", lambda *a, **k: FakeAlerter())
    monkeypatch.setenv("SERVICE_RESULT", "exit-code")
    monkeypatch.setenv("EXIT_CODE", "exited")
    monkeypatch.setenv("EXIT_STATUS", "1")

    assert hook.main() == 0
    assert len(sent) == 1
    message, level = sent[0]
    assert level == hook.AlertLevel.CRITICAL
    assert "exit-code" in message and "exited" in message and "exit_status=1" in message


def test_main_silent_when_not_invoked_by_systemd(hook, monkeypatch):
    """Bare invocation with no SERVICE_RESULT at all must not send an alert
    — only a genuine systemd-reported abnormal stop should page."""
    sent = []

    class FakeAlerter:
        async def send_alert(self, message, level):
            sent.append((message, level))

    monkeypatch.setattr(hook, "build_alerter", lambda *a, **k: FakeAlerter())
    monkeypatch.delenv("SERVICE_RESULT", raising=False)
    monkeypatch.delenv("EXIT_CODE", raising=False)
    monkeypatch.delenv("EXIT_STATUS", raising=False)

    assert hook.main() == 0
    assert sent == []


def test_main_silent_on_clean_stop(hook, monkeypatch):
    sent = []

    class FakeAlerter:
        async def send_alert(self, message, level):
            sent.append((message, level))

    monkeypatch.setattr(hook, "build_alerter", lambda *a, **k: FakeAlerter())
    monkeypatch.setenv("SERVICE_RESULT", "success")
    monkeypatch.setenv("EXIT_CODE", "ok")
    monkeypatch.setenv("EXIT_STATUS", "0")

    assert hook.main() == 0
    assert sent == []  # clean stop must not page anyone


def test_main_never_raises_when_alerter_fails(hook, monkeypatch, caplog):
    async def broken_send_alert(self, message, level):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(hook, "build_alerter", lambda *a, **k: type("A", (), {"send_alert": broken_send_alert})())
    monkeypatch.setenv("SERVICE_RESULT", "signal")
    monkeypatch.setenv("EXIT_CODE", "killed")
    monkeypatch.setenv("EXIT_STATUS", "KILL")

    with caplog.at_level(logging.ERROR, logger="send_crash_alert"):
        assert hook.main() == 0  # hook must never block systemd's restart
    assert any("Crash alert hook failed" in r.getMessage() for r in caplog.records)
