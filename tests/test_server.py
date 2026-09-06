import threading
import time
from datetime import datetime, timezone

from pondpi import server


class DummyThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


class _PassthroughProcessor:
    def add(self, value):
        return value

    def extra_state(self):
        return {}


class FakeSensorDriver:
    """A LevelSensor test double: `read()` returns each entry of a
    pre-scripted sequence in turn, then an empty dict forever once
    exhausted (matching a real driver's "nothing new this call")."""

    def __init__(self, readings_sequence):
        self._readings_sequence = list(readings_sequence)
        self._index = 0

    def read(self):
        if self._index < len(self._readings_sequence):
            result = self._readings_sequence[self._index]
            self._index += 1
            return result
        return {}


class FakeResetSensor:
    def __init__(self, supports_reset=True):
        self.supports_reset = supports_reset
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1


def _reset_globals(names):
    """Resets every sensor-keyed global to a fresh, empty-but-consistent
    state for the given sensor names, so each test starts from a known
    baseline regardless of what an earlier test left behind."""
    server._sensors = {}
    server._poll_threads = {}
    server._reset_locks = {}
    server._state = {}
    for name in names:
        server._state[name] = server._new_sensor_state()
        server._reset_locks[name] = threading.Lock()


def test_health_ok_when_poller_alive():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": object()}
    server._poll_threads = {"pond_main": DummyThread(alive=True)}
    server._state["pond_main"]["processor_names"] = ["rolling_avg", "instantaneous_raw"]
    server._commit_sha = "abc123"
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["default_sensor"] == "pond_main"
    assert data["started_at"] == server._started_at.isoformat()
    assert isinstance(data["uptime_seconds"], (int, float))
    assert data["uptime_seconds"] >= 0
    assert data["uptime_human"] == f"{int(data['uptime_seconds'])}s"
    assert data["commit_sha"] == "abc123"
    assert data["sensors"]["pond_main"]["poller_alive"] is True
    assert data["sensors"]["pond_main"]["last_reading_age_s"] is None
    assert data["sensors"]["pond_main"]["processors"] == ["rolling_avg", "instantaneous_raw"]


def test_health_degraded_when_poller_dead():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": object()}
    server._poll_threads = {"pond_main": DummyThread(alive=False)}
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "degraded"
    assert data["sensors"]["pond_main"]["poller_alive"] is False


def test_health_degraded_when_poller_never_started():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": object()}
    server._poll_threads = {}
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 503


def test_health_ok_when_reading_recent():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": object()}
    server._poll_threads = {"pond_main": DummyThread(alive=True)}
    server._state["pond_main"]["last_reading_monotonic"] = time.monotonic()
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert 0 <= data["sensors"]["pond_main"]["last_reading_age_s"] < server.STALE_READING_THRESHOLD_S


def test_health_degraded_when_reading_stale():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": object()}
    server._poll_threads = {"pond_main": DummyThread(alive=True)}
    server._state["pond_main"]["last_reading_monotonic"] = time.monotonic() - (server.STALE_READING_THRESHOLD_S + 1)
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "degraded"
    # poller thread is alive -- it's specifically the stale reading that
    # should drive degraded status here, not thread liveness.
    assert data["sensors"]["pond_main"]["poller_alive"] is True
    assert data["sensors"]["pond_main"]["last_reading_age_s"] > server.STALE_READING_THRESHOLD_S


def test_health_last_reset_at_null_before_any_reset():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": object()}
    server._poll_threads = {"pond_main": DummyThread(alive=True)}
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.get_json()["sensors"]["pond_main"]["last_reset_at"] is None


def test_health_reflects_last_reset_at():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": object()}
    server._poll_threads = {"pond_main": DummyThread(alive=True)}
    reset_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    server._state["pond_main"]["last_reset_at"] = reset_at
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.get_json()["sensors"]["pond_main"]["last_reset_at"] == reset_at.isoformat()


def test_health_reports_multiple_sensors_independently():
    _reset_globals(["pond_main", "rain_barrel"])
    server._sensors = {"pond_main": object(), "rain_barrel": object()}
    server._poll_threads = {
        "pond_main": DummyThread(alive=True),
        "rain_barrel": DummyThread(alive=False),
    }
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/health")

    # One sensor degraded is enough to make the overall status degraded.
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "degraded"
    assert data["sensors"]["pond_main"]["poller_alive"] is True
    assert data["sensors"]["rain_barrel"]["poller_alive"] is False


def test_reset_powercycles_default_sensor_and_records_last_reset_at():
    _reset_globals(["pond_main"])
    fake_sensor = FakeResetSensor()
    server._sensors = {"pond_main": fake_sensor}
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.post("/reset")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "reset"
    assert data["sensor"] == "pond_main"
    assert fake_sensor.reset_calls == 1
    assert server._state["pond_main"]["last_reset_at"] is not None
    assert data["reset_at"] == server._state["pond_main"]["last_reset_at"].isoformat()


def test_reset_returns_501_when_sensor_does_not_support_reset():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": FakeResetSensor(supports_reset=False)}
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.post("/reset")

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
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.post("/sensors/rain_barrel/reset")

    assert resp.status_code == 200
    assert resp.get_json()["sensor"] == "rain_barrel"
    assert fake_barrel.reset_calls == 1
    assert fake_main.reset_calls == 0


def test_poll_sensor_routes_raw_readings_through_processors():
    _reset_globals(["pond_main"])
    processors = {"instantaneous_raw": _PassthroughProcessor()}
    sensor = FakeSensorDriver([{"raw": 100}])
    stop_event = threading.Event()

    thread = threading.Thread(
        target=server.poll_sensor,
        args=("pond_main", sensor, processors, "instantaneous_raw", stop_event, 0.001),
    )
    thread.start()
    for _ in range(200):
        with server._state_lock:
            if server._state["pond_main"]["instantaneous_mm"] == 100:
                break
        time.sleep(0.005)
    stop_event.set()
    thread.join(timeout=1)

    assert server._state["pond_main"]["instantaneous_mm"] == 100
    assert server._state["pond_main"]["rolling_avg_mm"] == 100
    assert server._state["pond_main"]["last_reading_monotonic"] is not None


def test_poll_sensor_caches_processed_readings_as_is():
    # No processors configured for "processed" -- unlike "raw", it's
    # cached directly rather than run through a pipeline (see
    # poll_sensor()'s docstring).
    _reset_globals(["pond_main"])
    sensor = FakeSensorDriver([{"processed": 123}])
    stop_event = threading.Event()

    thread = threading.Thread(
        target=server.poll_sensor,
        args=("pond_main", sensor, {}, "primary", stop_event, 0.001),
    )
    thread.start()
    for _ in range(200):
        with server._state_lock:
            if server._state["pond_main"]["processed_mm"] == 123:
                break
        time.sleep(0.005)
    stop_event.set()
    thread.join(timeout=1)

    assert server._state["pond_main"]["processed_mm"] == 123


def test_poll_sensor_keeps_multiple_sensors_state_independent():
    _reset_globals(["pond_main", "rain_barrel"])
    sensor_main = FakeSensorDriver([{"raw": 100}])
    sensor_barrel = FakeSensorDriver([{"raw": 200}])
    stop_event = threading.Event()

    thread_main = threading.Thread(
        target=server.poll_sensor,
        args=("pond_main", sensor_main, {"raw": _PassthroughProcessor()}, "raw", stop_event, 0.001),
    )
    thread_barrel = threading.Thread(
        target=server.poll_sensor,
        args=("rain_barrel", sensor_barrel, {"raw": _PassthroughProcessor()}, "raw", stop_event, 0.001),
    )
    thread_main.start()
    thread_barrel.start()
    for _ in range(200):
        with server._state_lock:
            if (
                server._state["pond_main"]["instantaneous_mm"] == 100
                and server._state["rain_barrel"]["instantaneous_mm"] == 200
            ):
                break
        time.sleep(0.005)
    stop_event.set()
    thread_main.join(timeout=1)
    thread_barrel.join(timeout=1)

    assert server._state["pond_main"]["instantaneous_mm"] == 100
    assert server._state["rain_barrel"]["instantaneous_mm"] == 200


def test_level_returns_503_before_first_reading():
    _reset_globals(["pond_main"])
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/level")

    assert resp.status_code == 503


def test_level_returns_current_reading():
    _reset_globals(["pond_main"])
    server._state["pond_main"].update(
        instantaneous_mm=101.0,
        rolling_avg_mm=850.0,
        primary_name="rolling_avg",
        emit_flags={"rolling_median5": False, "rolling_avg": True, "instantaneous_raw": True},
        processors={
            "rolling_median5": {"value": 500.0},
            "rolling_avg": {
                "value": 850.0,
                "steps": [{"processor": "rolling_median5", "window_size": 5, "samples_in_window": 5}],
            },
            "instantaneous_raw": {"value": 101.0},
        },
    )
    server._polling_interval_ms = 10
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/level")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {
        "measure_name": "level",
        "units": "cm",
        "mode": "raw",
        "polling_interval_ms": 10,
        "primary_signal": {"value": 85.0, "name": "rolling_avg"},
        # rolling_median5 is emit: false -- absent from `signals`.
        "signals": {
            "rolling_avg": 85.0,
            "instantaneous_raw": 10.1,
        },
    }


def test_level_processed_mode_returns_503_before_first_processed_reading():
    _reset_globals(["pond_main"])
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/level?mode=processed")

    assert resp.status_code == 503


def test_level_processed_mode_returns_current_reading():
    _reset_globals(["pond_main"])
    server._state["pond_main"]["processed_mm"] = 123.0
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/level?mode=processed")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {
        "measure_name": "level",
        "units": "cm",
        "mode": "processed",
        "distance_cm": 12.3,
    }


def test_level_unrecognized_mode_falls_back_to_raw():
    _reset_globals(["pond_main"])
    server._state["pond_main"].update(
        instantaneous_mm=101.0,
        rolling_avg_mm=850.0,
        primary_name="rolling_avg",
        emit_flags={"rolling_avg": True},
        processors={"rolling_avg": {"value": 850.0}},
    )
    server._polling_interval_ms = 10
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/level?mode=bogus")

    assert resp.status_code == 200
    assert resp.get_json()["mode"] == "raw"


def test_sensor_level_returns_404_for_unknown_sensor():
    _reset_globals(["pond_main"])
    client = server.app.test_client()

    resp = client.get("/sensors/nonexistent/level")

    assert resp.status_code == 404


def test_sensor_level_targets_named_sensor_independently_of_default():
    _reset_globals(["pond_main", "rain_barrel"])
    server._state["rain_barrel"].update(
        instantaneous_mm=200.0,
        rolling_avg_mm=200.0,
        primary_name="raw",
        emit_flags={"raw": True},
        processors={"raw": {"value": 200.0}},
    )
    server._polling_interval_ms = 150
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/sensors/rain_barrel/level")

    assert resp.status_code == 200
    assert resp.get_json()["primary_signal"]["value"] == 20.0


def test_diag_returns_503_before_first_reading():
    _reset_globals(["pond_main"])
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/diag")

    assert resp.status_code == 503


def test_diag_returns_config_and_output_for_every_processor():
    _reset_globals(["pond_main"])
    server._state["pond_main"].update(
        instantaneous_mm=101.0,
        configs={
            "rolling_median5": {"type": "rolling_median", "params": {"window_size": 5}, "primary": False, "emit": False},
            "rolling_avg": {"type": "chain", "params": {"steps": [{"ref": "rolling_median5"}]}, "primary": True, "emit": True},
        },
        processors={
            "rolling_median5": {"value": 500.0},
            "rolling_avg": {
                "value": 850.0,
                "steps": [{"processor": "rolling_median5", "window_size": 5, "samples_in_window": 5}],
            },
        },
    )
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/diag")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {
        "processors": {
            "rolling_median5": {
                "config": {"type": "rolling_median", "params": {"window_size": 5}, "primary": False, "emit": False},
                "output": {"distance_cm": 50.0},
            },
            "rolling_avg": {
                "config": {"type": "chain", "params": {"steps": [{"ref": "rolling_median5"}]}, "primary": True, "emit": True},
                "output": {
                    "distance_cm": 85.0,
                    "steps": [{"processor": "rolling_median5", "window_size": 5, "samples_in_window": 5}],
                },
            },
        },
    }


def test_sensor_diag_returns_404_for_unknown_sensor():
    _reset_globals(["pond_main"])
    client = server.app.test_client()

    resp = client.get("/sensors/nonexistent/diag")

    assert resp.status_code == 404


def test_sensors_list_returns_names_and_default():
    _reset_globals(["pond_main", "rain_barrel"])
    server._sensors = {"pond_main": object(), "rain_barrel": object()}
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/sensors")

    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data["sensors"]) == {"pond_main", "rain_barrel"}
    assert data["default"] == "pond_main"
