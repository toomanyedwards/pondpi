import threading
import time
from datetime import datetime, timezone

from pondpi import server


class DummyThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


class FakeSignal:
    """Minimal stand-in for a push-fed (not owns_read_loop) signal --
    _signal_result() only ever checks its owns_read_loop attribute for
    this kind, reading the actual cached value out of server._state
    instead."""

    owns_read_loop = False


class FakePollingSignal:
    """Stand-in for a signal that owns its own read loop (like
    RollingAverageSignal) -- current() returns whatever result it was
    constructed with, or was fed via run_loop()."""

    owns_read_loop = True

    def __init__(self, result=None):
        self._result = result

    def current(self):
        return self._result

    def run_loop(self, stop_event, get_raw_value):
        while not stop_event.is_set():
            value = get_raw_value()
            if value is not None:
                self._result = {"value": value, "at": "t"}
            time.sleep(0.001)


class _PassthroughSignal:
    owns_read_loop = False

    def add(self, value):
        return value

    def extra_state(self):
        return {}


class _DoublingSignal:
    owns_read_loop = False

    def add(self, value):
        return value * 2

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
    server._polling_signal_threads = {}
    server._reset_locks = {}
    server._state = {}
    server._signal_owner = {}
    server._signal_objects = {}
    for name in names:
        server._state[name] = server._new_sensor_state()
        server._reset_locks[name] = threading.Lock()


def test_health_ok_when_poller_alive():
    _reset_globals(["pond_main"])
    server._sensors = {"pond_main": object()}
    server._poll_threads = {"pond_main": DummyThread(alive=True)}
    server._state["pond_main"]["signal_names"] = ["rolling_avg", "instantaneous_raw"]
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
    assert data["sensors"]["pond_main"]["signals"] == ["rolling_avg", "instantaneous_raw"]


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


def test_poll_sensor_routes_raw_readings_through_signals():
    _reset_globals(["pond_main"])
    signals = {"instantaneous_raw": _PassthroughSignal()}
    configs = {"instantaneous_raw": {"mode": "raw"}}
    sensor = FakeSensorDriver([{"raw": 100}])
    stop_event = threading.Event()

    thread = threading.Thread(
        target=server.poll_sensor,
        args=("pond_main", sensor, signals, configs, stop_event, 0.001),
    )
    thread.start()
    for _ in range(200):
        with server._state_lock:
            if "instantaneous_raw" in server._state["pond_main"]["signals"]:
                break
        time.sleep(0.005)
    stop_event.set()
    thread.join(timeout=1)

    assert server._state["pond_main"]["signals"]["instantaneous_raw"]["value"] == 100
    assert server._state["pond_main"]["last_reading_monotonic"] is not None


def test_poll_sensor_downstream_signal_receives_upstream_signals_output():
    # "downstream" doubles whatever it's fed. If it wrongly received the
    # raw sensor reading directly instead of "root"'s own (already
    # doubled) output, it'd land on 200 instead of 400.
    _reset_globals(["pond_main"])
    signals = {"root": _DoublingSignal(), "downstream": _DoublingSignal()}
    configs = {"root": {"mode": "raw"}, "downstream": {"mode": "raw", "input": "root"}}
    sensor = FakeSensorDriver([{"raw": 100}])
    stop_event = threading.Event()

    thread = threading.Thread(
        target=server.poll_sensor,
        args=("pond_main", sensor, signals, configs, stop_event, 0.001),
    )
    thread.start()
    for _ in range(200):
        with server._state_lock:
            if "downstream" in server._state["pond_main"]["signals"]:
                break
        time.sleep(0.005)
    stop_event.set()
    thread.join(timeout=1)

    assert server._state["pond_main"]["signals"]["downstream"]["value"] == 400


def test_poll_sensor_skips_signals_that_own_their_own_read_loop():
    _reset_globals(["pond_main"])
    polling_signal = FakePollingSignal()
    signals = {"instantaneous_raw": _PassthroughSignal(), "avg": polling_signal}
    configs = {"instantaneous_raw": {"mode": "raw"}, "avg": {"mode": "raw", "input": "instantaneous_raw"}}
    sensor = FakeSensorDriver([{"raw": 100}])
    stop_event = threading.Event()

    thread = threading.Thread(
        target=server.poll_sensor,
        args=("pond_main", sensor, signals, configs, stop_event, 0.001),
    )
    thread.start()
    for _ in range(200):
        with server._state_lock:
            if "instantaneous_raw" in server._state["pond_main"]["signals"]:
                break
        time.sleep(0.005)
    stop_event.set()
    thread.join(timeout=1)

    assert "instantaneous_raw" in server._state["pond_main"]["signals"]
    assert "avg" not in server._state["pond_main"]["signals"]


def test_poll_sensor_caches_processed_signals_independently_of_raw():
    _reset_globals(["pond_main"])
    signals = {"proc_sig": _PassthroughSignal()}
    configs = {"proc_sig": {"mode": "processed"}}
    sensor = FakeSensorDriver([{"processed": 123}])
    stop_event = threading.Event()

    thread = threading.Thread(
        target=server.poll_sensor,
        args=("pond_main", sensor, signals, configs, stop_event, 0.001),
    )
    thread.start()
    for _ in range(200):
        with server._state_lock:
            if "proc_sig" in server._state["pond_main"]["signals"]:
                break
        time.sleep(0.005)
    stop_event.set()
    thread.join(timeout=1)

    assert server._state["pond_main"]["signals"]["proc_sig"]["value"] == 123
    # A "processed" reading must never touch last_reading_monotonic --
    # /health's staleness check is specifically about the raw pipeline.
    assert server._state["pond_main"]["last_reading_monotonic"] is None


def test_poll_sensor_routes_each_reading_to_signals_rooted_at_its_own_mode():
    # A raw-rooted and a processed-rooted signal on the same sensor:
    # each should only be fed (and only get a fresh "at" timestamp)
    # when its own reading key shows up, independent of the other.
    _reset_globals(["pond_main"])
    signals = {"raw_sig": _PassthroughSignal(), "proc_sig": _PassthroughSignal()}
    configs = {"raw_sig": {"mode": "raw"}, "proc_sig": {"mode": "processed"}}
    sensor = FakeSensorDriver([{"raw": 100}, {"processed": 50}])
    stop_event = threading.Event()

    thread = threading.Thread(
        target=server.poll_sensor,
        args=("pond_main", sensor, signals, configs, stop_event, 0.001),
    )
    thread.start()
    for _ in range(200):
        with server._state_lock:
            sigs = server._state["pond_main"]["signals"]
            if "raw_sig" in sigs and "proc_sig" in sigs:
                break
        time.sleep(0.005)
    stop_event.set()
    thread.join(timeout=1)

    sigs = server._state["pond_main"]["signals"]
    assert sigs["raw_sig"]["value"] == 100
    assert sigs["proc_sig"]["value"] == 50
    assert sigs["raw_sig"]["at"]
    assert sigs["proc_sig"]["at"]
    assert server._state["pond_main"]["last_reading_monotonic"] is not None


def test_poll_sensor_keeps_multiple_sensors_state_independent():
    _reset_globals(["pond_main", "rain_barrel"])
    sensor_main = FakeSensorDriver([{"raw": 100}])
    sensor_barrel = FakeSensorDriver([{"raw": 200}])
    stop_event = threading.Event()

    thread_main = threading.Thread(
        target=server.poll_sensor,
        args=("pond_main", sensor_main, {"raw": _PassthroughSignal()}, {"raw": {"mode": "raw"}}, stop_event, 0.001),
    )
    thread_barrel = threading.Thread(
        target=server.poll_sensor,
        args=("rain_barrel", sensor_barrel, {"raw": _PassthroughSignal()}, {"raw": {"mode": "raw"}}, stop_event, 0.001),
    )
    thread_main.start()
    thread_barrel.start()
    for _ in range(200):
        with server._state_lock:
            if (
                "raw" in server._state["pond_main"]["signals"]
                and "raw" in server._state["rain_barrel"]["signals"]
            ):
                break
        time.sleep(0.005)
    stop_event.set()
    thread_main.join(timeout=1)
    thread_barrel.join(timeout=1)

    assert server._state["pond_main"]["signals"]["raw"]["value"] == 100
    assert server._state["rain_barrel"]["signals"]["raw"]["value"] == 200


def test_diag_returns_503_before_first_reading():
    _reset_globals(["pond_main"])
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/diag")

    assert resp.status_code == 503


def test_diag_returns_config_and_output_for_every_signal():
    _reset_globals(["pond_main"])
    server._signal_objects = {
        "rolling_median5": FakeSignal(),
        "rolling_avg": FakePollingSignal(
            {"value": 850.0, "at": "2026-01-01T00:00:00+00:00", "window_size": 200, "samples_in_window": 200}
        ),
    }
    server._state["pond_main"].update(
        primary_name="rolling_avg",
        signal_names=["rolling_median5", "rolling_avg"],
        configs={
            "rolling_median5": {
                "type": "rolling_median",
                "params": {"window_size": 5},
                "primary": False,
                "emit": False,
                "input": "instantaneous_raw",
                "unit": "cm",
            },
            "rolling_avg": {
                "type": "rolling_average",
                "params": {"window_size": 200},
                "primary": True,
                "emit": True,
                "input": "rolling_median5",
                "unit": "cm",
            },
        },
        signals={
            "rolling_median5": {"value": 500.0, "at": "2026-01-01T00:00:00+00:00", "window_size": 5, "samples_in_window": 5},
        },
    )
    server._default_sensor_name = "pond_main"
    client = server.app.test_client()

    resp = client.get("/diag")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {
        "signals": {
            "rolling_median5": {
                "config": {
                    "type": "rolling_median",
                    "params": {"window_size": 5},
                    "primary": False,
                    "emit": False,
                    "input": "instantaneous_raw",
                    "unit": "cm",
                },
                "output": {
                    "value": 50.0,
                    "unit": "cm",
                    "at": "2026-01-01T00:00:00+00:00",
                    "window_size": 5,
                    "samples_in_window": 5,
                },
            },
            "rolling_avg": {
                "config": {
                    "type": "rolling_average",
                    "params": {"window_size": 200},
                    "primary": True,
                    "emit": True,
                    "input": "rolling_median5",
                    "unit": "cm",
                },
                "output": {
                    "value": 85.0,
                    "unit": "cm",
                    "at": "2026-01-01T00:00:00+00:00",
                    "window_size": 200,
                    "samples_in_window": 200,
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
        "rolling_avg": FakePollingSignal(
            {"value": 850.0, "at": "2026-01-01T00:00:00+00:00", "window_size": 400, "samples_in_window": 400}
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
    server._signal_objects = {"rolling_avg": FakePollingSignal(None)}
    client = server.app.test_client()

    resp = client.get("/signals/rolling_avg")

    assert resp.status_code == 503


def test_signal_diag_returns_config_and_output():
    _reset_globals(["pond_main"])
    server._signal_owner = {"rolling_avg": "pond_main"}
    server._signal_objects = {
        "rolling_avg": FakePollingSignal(
            {"value": 850.0, "at": "2026-01-01T00:00:00+00:00", "window_size": 400, "samples_in_window": 400}
        )
    }
    server._state["pond_main"].update(
        configs={
            "rolling_avg": {
                "type": "rolling_average",
                "params": {"window_size": 400},
                "primary": True,
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
            "primary": True,
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
    server._signal_objects = {"rolling_avg": FakePollingSignal(None)}
    client = server.app.test_client()

    resp = client.get("/signals/rolling_avg/diag")

    assert resp.status_code == 503
