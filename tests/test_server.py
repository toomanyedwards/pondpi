from datetime import datetime, timezone

from pondpi import server


class FakeSignal:
    """Stand-in for any LevelSignal -- current() returns whatever
    result it was constructed with (server.py's _signal_result() just
    calls current() directly now, no owns_read_loop branching left at
    that layer). reset_calls tracks reset() calls, for the cascade
    test."""

    def __init__(self, result=None):
        self._result = result
        self.reset_calls = 0

    def current(self):
        return self._result

    def reset(self):
        self.reset_calls += 1


class FakeSensor:
    """Stand-in LevelSensor for /health tests -- every value is exactly
    whatever it was constructed with, no real polling thread or
    staleness math (server.py no longer does that math itself either --
    it just calls is_healthy()/last_reset_at() and trusts the answer,
    same as this double gives it one directly)."""

    def __init__(self, last_reading_monotonic=None, is_healthy=True, last_reset_at=None):
        self._last_reading_monotonic = last_reading_monotonic
        self._is_healthy = is_healthy
        self._last_reset_at = last_reset_at

    def last_reading_monotonic(self):
        return self._last_reading_monotonic

    def is_healthy(self):
        return self._is_healthy

    def last_reset_at(self):
        return self._last_reset_at


class FakeResetSensor:
    def __init__(self, supports_reset=True):
        self.supports_reset = supports_reset
        self.reset_calls = 0
        self._last_reset_at = None

    def reset(self):
        self.reset_calls += 1
        self._last_reset_at = datetime.now(timezone.utc)

    def last_reset_at(self):
        return self._last_reset_at


def _reset_globals(names):
    """Resets every sensor-keyed global to a fresh, empty-but-consistent
    state for the given sensor names, so each test starts from a known
    baseline regardless of what an earlier test left behind."""
    server._sensors = {}
    server._state = {}
    server._signal_owner = {}
    server._signal_objects = {}
    for name in names:
        server._state[name] = server._new_sensor_state()


def test_health_ok_before_first_reading():
    # No reading yet is expected right after startup -- shouldn't read
    # as degraded on its own, only actual staleness should.
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": FakeSensor()}
    server._state["pond_main"]["signal_names"] = ["rolling_avg", "instantaneous_raw"]
    server._commit_sha = "abc123"
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["started_at"] == server._started_at.isoformat()
    assert isinstance(data["uptime_seconds"], (int, float))
    assert data["uptime_seconds"] >= 0
    assert data["uptime_human"] == f"{int(data['uptime_seconds'])}s"
    assert data["commit_sha"] == "abc123"
    assert data["sensors"]["pond_main"]["last_reading_age_s"] is None
    assert data["sensors"]["pond_main"]["signals"] == ["rolling_avg", "instantaneous_raw"]


def test_health_ok_when_sensor_reports_healthy():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": FakeSensor(last_reading_monotonic=server.time.monotonic(), is_healthy=True)}
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert 0 <= data["sensors"]["pond_main"]["last_reading_age_s"] < 1


def test_health_degraded_when_sensor_reports_unhealthy():
    # The verdict itself comes entirely from is_healthy() -- server.py
    # holds no threshold of its own to recompute it against.
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": FakeSensor(last_reading_monotonic=server.time.monotonic() - 10, is_healthy=False)}
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "degraded"
    assert data["sensors"]["pond_main"]["last_reading_age_s"] > 0


def test_health_last_reset_at_null_before_any_reset():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": FakeSensor()}
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.get_json()["sensors"]["pond_main"]["last_reset_at"] is None


def test_health_reflects_last_reset_at():
    _reset_globals(["pond_main"])
    reset_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    server._sensors = {"pond_main": FakeSensor(last_reset_at=reset_at)}
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.get_json()["sensors"]["pond_main"]["last_reset_at"] == reset_at.isoformat()


def test_health_reports_multiple_sensors_independently():
    _reset_globals(["pond_main", "rain_barrel"])
    server._sensors = {
        "pond_main": FakeSensor(is_healthy=True),
        "rain_barrel": FakeSensor(is_healthy=False),
    }
    client = server.app.test_client()

    resp = client.get("/health")

    # One sensor stale is enough to make the overall status degraded.
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "degraded"
    assert data["sensors"]["pond_main"]["status"] == "ok"
    assert data["sensors"]["rain_barrel"]["status"] == "degraded"


def test_reset_powercycles_a_single_configured_sensor_and_records_last_reset_at():
    _reset_globals(["pond_main"])
    fake_sensor = FakeResetSensor()
    server._sensors = {"pond_main": fake_sensor}
    client = server.app.test_client()

    resp = client.post("/reset")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sensors"]["pond_main"]["status"] == "reset"
    assert fake_sensor.reset_calls == 1
    assert fake_sensor.last_reset_at() is not None
    assert data["sensors"]["pond_main"]["reset_at"] == fake_sensor.last_reset_at().isoformat()


def test_reset_powercycles_every_supporting_sensor():
    _reset_globals(["pond_main", "rain_barrel"])
    fake_main = FakeResetSensor()
    fake_barrel = FakeResetSensor()
    server._sensors = {"pond_main": fake_main, "rain_barrel": fake_barrel}
    client = server.app.test_client()

    resp = client.post("/reset")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sensors"]["pond_main"]["status"] == "reset"
    assert data["sensors"]["rain_barrel"]["status"] == "reset"
    assert fake_main.reset_calls == 1
    assert fake_barrel.reset_calls == 1
    assert fake_main.last_reset_at() is not None
    assert fake_barrel.last_reset_at() is not None


def test_reset_reports_not_supported_without_failing_other_sensors():
    _reset_globals(["pond_main", "rain_barrel"])
    fake_main = FakeResetSensor()
    unsupported_barrel = FakeResetSensor(supports_reset=False)
    server._sensors = {"pond_main": fake_main, "rain_barrel": unsupported_barrel}
    client = server.app.test_client()

    resp = client.post("/reset")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sensors"]["pond_main"]["status"] == "reset"
    assert data["sensors"]["rain_barrel"] == {"status": "not_supported"}
    assert fake_main.reset_calls == 1
    assert unsupported_barrel.reset_calls == 0
    assert unsupported_barrel.last_reset_at() is None


def test_reset_cascades_to_every_signal_rooted_at_that_sensor():
    # A sensor reset happens because something looked wrong -- so
    # accumulated signal state (a rolling window, an average) built
    # from readings around that time is suspect too. Resetting the
    # sensor resets every signal in its group, giving a clean slate
    # end-to-end.
    _reset_globals(["pond_main"])
    fake_sensor = FakeResetSensor()
    server._sensors = {"pond_main": fake_sensor}
    server._state["pond_main"]["signal_names"] = ["instantaneous_raw", "rolling_avg"]
    fake_raw = FakeSignal()
    fake_avg = FakeSignal()
    server._signal_objects = {"instantaneous_raw": fake_raw, "rolling_avg": fake_avg}
    client = server.app.test_client()

    resp = client.post("/sensors/pond_main/reset")

    assert resp.status_code == 200
    assert fake_sensor.reset_calls == 1
    assert fake_raw.reset_calls == 1
    assert fake_avg.reset_calls == 1


def test_sensor_reset_returns_501_when_sensor_does_not_support_reset():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": FakeResetSensor(supports_reset=False)}
    client = server.app.test_client()

    resp = client.post("/sensors/pond_main/reset")

    assert resp.status_code == 501


def test_sensor_reset_returns_404_for_unknown_sensor():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": FakeResetSensor()}
    client = server.app.test_client()

    resp = client.post("/sensors/nonexistent/reset")

    assert resp.status_code == 404


def test_sensor_reset_targets_named_sensor_independently():
    _reset_globals(["pond_main", "rain_barrel"])
    fake_main = FakeResetSensor()
    fake_barrel = FakeResetSensor()
    server._sensors = {"pond_main": fake_main, "rain_barrel": fake_barrel}
    client = server.app.test_client()

    resp = client.post("/sensors/rain_barrel/reset")

    assert resp.status_code == 200
    assert resp.get_json()["sensor"] == "rain_barrel"
    assert fake_barrel.reset_calls == 1
    assert fake_main.reset_calls == 0


_ROLLING_MEDIAN5_CONFIG = {
    "type": "rolling_median",
    "params": {"window_size": 5},
    "emit": False,
    "input": "instantaneous_raw",
    "unit": "cm",
}
_ROLLING_AVG_CONFIG = {
    "type": "rolling_average",
    "params": {"window_size": 200},
    "emit": True,
    "input": "rolling_median5",
    "unit": "cm",
}


def _populate_pond_main_diag_state():
    """Shared setup for both the bare and named diag tests: one sensor
    with two signals, both already holding a cached current() result."""
    server._signal_objects = {
        "rolling_median5": FakeSignal(
            {"value": 50.0, "at": "2026-01-01T00:00:00+00:00", "window_size": 5, "samples_in_window": 5}
        ),
        "rolling_avg": FakeSignal(
            {"value": 85.0, "at": "2026-01-01T00:00:00+00:00", "window_size": 200, "samples_in_window": 200}
        ),
    }
    server._state["pond_main"].update(
        signal_names=["rolling_median5", "rolling_avg"],
        configs={"rolling_median5": _ROLLING_MEDIAN5_CONFIG, "rolling_avg": _ROLLING_AVG_CONFIG},
    )


_EXPECTED_POND_MAIN_DIAG_SIGNALS = {
    "rolling_median5": {
        "config": _ROLLING_MEDIAN5_CONFIG,
        "output": {
            "value": 50.0,
            "unit": "cm",
            "at": "2026-01-01T00:00:00+00:00",
            "window_size": 5,
            "samples_in_window": 5,
        },
    },
    "rolling_avg": {
        "config": _ROLLING_AVG_CONFIG,
        "output": {
            "value": 85.0,
            "unit": "cm",
            "at": "2026-01-01T00:00:00+00:00",
            "window_size": 200,
            "samples_in_window": 200,
        },
    },
}


def test_diag_aggregates_every_sensor_at_once():
    _reset_globals(["pond_main", "rain_barrel"])
    server._sensors = {"pond_main": object(), "rain_barrel": object()}
    _populate_pond_main_diag_state()
    client = server.app.test_client()

    resp = client.get("/diag")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {
        "sensors": {
            "pond_main": _EXPECTED_POND_MAIN_DIAG_SIGNALS,
            # No readings yet for rain_barrel -- shown as empty rather
            # than failing the whole request, same as bare POST /reset
            # reports per-sensor status instead of an all-or-nothing error.
            "rain_barrel": {},
        },
    }


def test_sensor_diag_returns_404_for_unknown_sensor():
    _reset_globals(["pond_main"])
    client = server.app.test_client()

    resp = client.get("/sensors/nonexistent/diag")

    assert resp.status_code == 404


def test_sensor_diag_returns_503_before_first_reading():
    _reset_globals(["pond_main"])
    client = server.app.test_client()

    resp = client.get("/sensors/pond_main/diag")

    assert resp.status_code == 503


def test_sensor_diag_returns_config_and_output_for_every_signal():
    _reset_globals(["pond_main"])
    _populate_pond_main_diag_state()
    client = server.app.test_client()

    resp = client.get("/sensors/pond_main/diag")

    assert resp.status_code == 200
    assert resp.get_json() == {"signals": _EXPECTED_POND_MAIN_DIAG_SIGNALS}


def test_sensors_list_returns_names():
    _reset_globals(["pond_main", "rain_barrel"])
    server._sensors = {"pond_main": object(), "rain_barrel": object()}
    client = server.app.test_client()

    resp = client.get("/sensors")

    assert resp.status_code == 200
    assert set(resp.get_json()["sensors"]) == {"pond_main", "rain_barrel"}


def test_signals_list_returns_every_configured_signal_name():
    _reset_globals(["pond_main", "rain_barrel"])
    server._signal_owner = {"instantaneous_raw": "pond_main", "rolling_avg": "pond_main", "barrel_raw": "rain_barrel"}
    client = server.app.test_client()

    resp = client.get("/signals")

    assert resp.status_code == 200
    assert set(resp.get_json()["signals"]) == {"instantaneous_raw", "rolling_avg", "barrel_raw"}


def test_signal_detail_returns_value_and_extra_state():
    _reset_globals(["pond_main"])
    server._signal_owner = {"rolling_avg": "pond_main"}
    server._signal_objects = {
        "rolling_avg": FakeSignal(
            {"value": 85.0, "at": "2026-01-01T00:00:00+00:00", "window_size": 400, "samples_in_window": 400}
        )
    }
    server._state["pond_main"].update(configs={"rolling_avg": {"unit": "cm"}})
    client = server.app.test_client()

    resp = client.get("/signals/rolling_avg")

    assert resp.status_code == 200
    assert resp.get_json() == {
        "name": "rolling_avg",
        "sensor": "pond_main",
        "unit": "cm",
        "value": 85.0,
        "at": "2026-01-01T00:00:00+00:00",
        "window_size": 400,
        "samples_in_window": 400,
    }


def test_signal_detail_returns_404_for_unknown_signal():
    _reset_globals(["pond_main"])
    client = server.app.test_client()

    resp = client.get("/signals/nonexistent")

    assert resp.status_code == 404


def test_signal_detail_returns_503_before_first_reading():
    _reset_globals(["pond_main"])
    server._signal_owner = {"rolling_avg": "pond_main"}
    server._signal_objects = {"rolling_avg": FakeSignal(None)}
    client = server.app.test_client()

    resp = client.get("/signals/rolling_avg")

    assert resp.status_code == 503


def test_signal_diag_returns_config_and_output():
    _reset_globals(["pond_main"])
    server._signal_owner = {"rolling_avg": "pond_main"}
    server._signal_objects = {
        "rolling_avg": FakeSignal(
            {"value": 85.0, "at": "2026-01-01T00:00:00+00:00", "window_size": 400, "samples_in_window": 400}
        )
    }
    server._state["pond_main"].update(
        configs={
            "rolling_avg": {
                "type": "rolling_average",
                "params": {"window_size": 400},
                "emit": True,
                "input": "instantaneous_raw",
                "unit": "cm",
            }
        },
    )
    client = server.app.test_client()

    resp = client.get("/signals/rolling_avg/diag")

    assert resp.status_code == 200
    assert resp.get_json() == {
        "name": "rolling_avg",
        "sensor": "pond_main",
        "config": {
            "type": "rolling_average",
            "params": {"window_size": 400},
            "emit": True,
            "input": "instantaneous_raw",
            "unit": "cm",
        },
        "output": {
            "value": 85.0,
            "unit": "cm",
            "at": "2026-01-01T00:00:00+00:00",
            "window_size": 400,
            "samples_in_window": 400,
        },
    }


def test_signal_diag_returns_404_for_unknown_signal():
    _reset_globals(["pond_main"])
    client = server.app.test_client()

    resp = client.get("/signals/nonexistent/diag")

    assert resp.status_code == 404


def test_signal_diag_returns_503_before_first_reading():
    _reset_globals(["pond_main"])
    server._signal_owner = {"rolling_avg": "pond_main"}
    server._signal_objects = {"rolling_avg": FakeSignal(None)}
    client = server.app.test_client()

    resp = client.get("/signals/rolling_avg/diag")

    assert resp.status_code == 503
