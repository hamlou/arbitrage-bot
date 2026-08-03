"""
Offline tests for scripts/check_clock_drift.py - no test touches a real NTP
server or the real Cloudflare endpoint. The NTP client, httpx, and time.time
are all mocked so drift math is verified deterministically.

Also verifies that scripts/report_latency.py and scripts/validate_paper_run.py
surface the drift warning near the top of their output when drift is large.
"""
import sys
import types
from unittest.mock import patch

import pytest

import scripts.check_clock_drift as drift

FAKE_NTPLIB = types.ModuleType("ntplib")
FAKE_NTPLIB.NTPException = OSError  # real ntplib exposes this; fake aliases it


class _FakeNTPResponse:
    offset = 0.12  # local clock is 120 ms ahead of the server


class _FakeNTPClient:
    def request(self, *args, **kwargs):
        return _FakeNTPResponse()


class _RaisingNTPClient:
    def request(self, *args, **kwargs):
        raise OSError("udp blocked")


def _install_fake_ntplib(monkeypatch, client_cls):
    FAKE_NTPLIB.NTPClient = client_cls
    monkeypatch.setitem(sys.modules, "ntplib", FAKE_NTPLIB)


# ---------------------------------------------------------------- NTP path


def test_ntp_path_returns_offset_in_ms(monkeypatch):
    _install_fake_ntplib(monkeypatch, _FakeNTPClient)
    ms, source = drift.measure_drift_ms(timeout=1.0)
    assert ms == pytest.approx(120.0)
    assert source == "ntp:pool.ntp.org"


def test_ntp_negative_drift_is_behind(monkeypatch):
    class _SlowNTPClient:
        def request(self, *args, **kwargs):
            return types.SimpleNamespace(offset=-0.005)  # 5 ms behind

    _install_fake_ntplib(monkeypatch, _SlowNTPClient)
    ms, source = drift.measure_drift_ms(timeout=1.0)
    assert ms == pytest.approx(-5.0)


# ----------------------------------------------------------- HTTPS fallback


class _FakeTraceResponse:
    def __init__(self, ts):
        self.text = f"ip=1.2.3.4\nts={ts}\ncolo=DFW\n"

    def raise_for_status(self):
        pass


def test_https_fallback_when_ntplib_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "ntplib", None)
    # Drift = midpoint(t0, t1) - server_ts. Symmetric RTT around the server
    # timestamp means the clocks agree exactly.
    server_ts = 1_720_000_000.500
    fake_now = [1_720_000_000.300, 1_720_000_000.700]  # midpoint == server_ts

    with patch("scripts.check_clock_drift.httpx.get", return_value=_FakeTraceResponse(server_ts)), \
         patch("scripts.check_clock_drift.time.time", side_effect=fake_now):
        ms, source = drift.measure_drift_ms(timeout=1.0)

    assert ms == pytest.approx(0.0)
    assert source == "https:cloudflare.com/cdn-cgi/trace"


def test_https_fallback_when_ntp_fails(monkeypatch):
    _install_fake_ntplib(monkeypatch, _RaisingNTPClient)
    fake_ts = 1_720_000_000.500
    fake_now = [1_720_000_000.000, 1_720_000_000.200]  # 200 ms RTT

    with patch("scripts.check_clock_drift.httpx.get", return_value=_FakeTraceResponse(fake_ts)), \
         patch("scripts.check_clock_drift.time.time", side_effect=fake_now):
        ms, source = drift.measure_drift_ms(timeout=1.0)

    # Reference = 1720000000.600; t1 = 1720000000.200 -> 400 ms behind.
    assert ms == pytest.approx(-400.0)
    assert source == "https:cloudflare.com/cdn-cgi/trace"


def test_https_response_without_ts_line(monkeypatch):
    monkeypatch.setitem(sys.modules, "ntplib", None)

    class _NoTsResponse:
        text = "ip=1.2.3.4\ncolo=DFW\n"  # no ts=

        def raise_for_status(self):
            pass

    with patch("scripts.check_clock_drift.httpx.get", return_value=_NoTsResponse()):
        ms, source = drift.measure_drift_ms(timeout=1.0)
    assert ms is None
    assert source == "https-no-timestamp"


# ------------------------------------------------------------ total failure


def test_both_sources_unreachable(monkeypatch):
    monkeypatch.setitem(sys.modules, "ntplib", None)
    with patch("scripts.check_clock_drift.httpx.get", side_effect=OSError("no network")):
        ms, source = drift.measure_drift_ms(timeout=0.5)
    assert ms is None
    assert "unreachable" in source


# ------------------------------------------------------------ warning line


def test_drift_warning_line_over_threshold():
    warning = drift.drift_warning_line(max_drift_ms=50.0, drift_ms=120.0, source="ntp:test")
    assert warning is not None
    assert "120.0 ms" in warning
    assert "may not be trustworthy" in warning
    assert "50 ms" in warning


def test_drift_warning_line_at_threshold_is_not_warned():
    # Spec: warn when drift is GREATER than 50 ms - exactly 50 ms is not a warning.
    assert drift.drift_warning_line(max_drift_ms=50.0, drift_ms=50.0, source="ntp:test") is None
    assert drift.drift_warning_line(max_drift_ms=50.0, drift_ms=-50.0, source="ntp:test") is None


def test_drift_warning_line_under_threshold():
    assert drift.drift_warning_line(max_drift_ms=50.0, drift_ms=5.0, source="ntp:test") is None


def test_drift_warning_line_unmeasurable(monkeypatch):
    # None drift_ms means "measure live" - mock that the measurement failed.
    monkeypatch.setattr(drift, "measure_drift_ms", lambda timeout=3.0: (None, "ntp-unreachable (x)"))
    assert drift.drift_warning_line(max_drift_ms=50.0) is None


# ---------------------------------------------------------------- main()


def test_main_prints_drift_and_warning(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check_clock_drift.py"])
    _install_fake_ntplib(monkeypatch, _FakeNTPClient)
    rc = drift.main()  # NTP offset 0.12 -> +120 ms -> warning fires
    out = capsys.readouterr().out
    assert rc == 0
    assert "+120.0 ms" in out
    assert "WARNING" in out


def test_main_prints_drift_without_warning(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check_clock_drift.py"])

    class _TinyDriftNTPClient:
        def request(self, *args, **kwargs):
            return types.SimpleNamespace(offset=0.001)  # 1 ms - no warning

    _install_fake_ntplib(monkeypatch, _TinyDriftNTPClient)
    rc = drift.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "+1.0 ms" in out
    assert "WARNING" not in out


def test_main_exit_1_when_unreachable(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check_clock_drift.py"])
    monkeypatch.setitem(sys.modules, "ntplib", None)
    with patch("scripts.check_clock_drift.httpx.get", side_effect=OSError("no network")):
        rc = drift.main()
    err = capsys.readouterr().err
    assert rc == 1
    assert "Could not measure clock drift" in err


# ------------------------------------------------- report-script integration


def test_report_latency_prints_warning_when_drift_large(monkeypatch, capsys):
    import asyncio

    import scripts.report_latency as report_latency
    from scripts.check_clock_drift import drift_warning_line

    fake_event = {"tick_to_signal_ms": 5.0, "tick_to_order_ms": 10.0, "fired": True}

    async def fake_get_latency_events(self):
        return [fake_event]

    async def fake_connect(self):
        return None

    async def fake_close(self):
        return None

    monkeypatch.setattr(report_latency.Database, "connect", fake_connect)
    monkeypatch.setattr(report_latency.Database, "close", fake_close)
    monkeypatch.setattr(report_latency.Database, "get_latency_events", fake_get_latency_events)
    monkeypatch.setattr(
        report_latency, "drift_warning_line",
        lambda **kw: drift_warning_line(max_drift_ms=50.0, drift_ms=999.0, source="ntp:test"),
    )

    asyncio.run(report_latency.report(2.7))
    out = capsys.readouterr().out
    assert "LATENCY REPORT" in out
    assert "WARNING: system clock is 999.0 ms" in out


def test_report_latency_no_warning_when_drift_small(monkeypatch, capsys):
    import asyncio

    import scripts.report_latency as report_latency

    async def fake_get_latency_events(self):
        return [{"tick_to_signal_ms": 5.0, "tick_to_order_ms": None, "fired": True}]

    async def fake_connect(self):
        return None

    async def fake_close(self):
        return None

    monkeypatch.setattr(report_latency.Database, "connect", fake_connect)
    monkeypatch.setattr(report_latency.Database, "close", fake_close)
    monkeypatch.setattr(report_latency.Database, "get_latency_events", fake_get_latency_events)
    monkeypatch.setattr(report_latency, "drift_warning_line", lambda **kw: None)

    asyncio.run(report_latency.report(2.7))
    out = capsys.readouterr().out
    assert "LATENCY REPORT" in out
    assert "WARNING" not in out


def test_validate_paper_run_prints_warning_when_drift_large(monkeypatch, capsys):
    import scripts.validate_paper_run as validate
    from scripts.check_clock_drift import drift_warning_line

    result = validate.ValidationResult(
        total_trades=5,
        win_rate=0.8,
        expectancy_usd=0.5,
        max_drawdown_pct=0.1,
        days_elapsed=9.0,
        passed=True,
        failures=[],
    )
    monkeypatch.setattr(
        validate, "drift_warning_line",
        lambda **kw: drift_warning_line(max_drift_ms=50.0, drift_ms=777.0, source="ntp:test"),
    )
    validate._print_report(result)
    out = capsys.readouterr().out
    assert "PAPER TRADING VALIDATION REPORT" in out
    assert "WARNING: system clock is 777.0 ms" in out
