import threading
import time

from pondpi import server


class DummyThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


class FakeSerial:
    def __init__(self, data=b""):
        self._buf = data
        self.reset_count = 0

    @property
    def in_waiting(self):
        return len(self._buf)

    def read(self, n):
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk

    def reset_input_buffer(self):
        self.reset_count += 1
        self._buf = b""


def make_frame(data_h, data_l, checksum=None):
    if checksum is None:
        checksum = (0xFF + data_h + data_l) & 0xFF
    return bytes([0xFF, data_h, data_l, checksum])


class _PassthroughProcessor:
    def add(self, value):
        return value

    def extra_state(self):
        return {}


def test_health_ok_when_poller_alive():
    server._poll_thread = DummyThread(alive=True)
    server._state["processor_names"] = ["rolling_avg", "instantaneous_raw"]
    server._state["commit_sha"] = "abc123"
    server._state["last_reading_monotonic"] = None
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["poller_alive"] is True
    assert data["last_reading_age_s"] is None
    assert data["started_at"] == server._started_at.isoformat()
    assert isinstance(data["uptime_seconds"], (int, float))
    assert data["uptime_seconds"] >= 0
    assert data["uptime_human"] == f"{int(data['uptime_seconds'])}s"
    assert data["processors"] == ["rolling_avg", "instantaneous_raw"]
    assert data["commit_sha"] == "abc123"


def test_health_degraded_when_poller_dead():
    server._poll_thread = DummyThread(alive=False)
    server._state["last_reading_monotonic"] = None
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "degraded"
    assert data["poller_alive"] is False


def test_health_degraded_when_poller_never_started():
    server._poll_thread = None
    server._state["last_reading_monotonic"] = None
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 503


def test_health_ok_when_reading_recent():
    server._poll_thread = DummyThread(alive=True)
    server._state["last_reading_monotonic"] = time.monotonic()
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert 0 <= data["last_reading_age_s"] < server.STALE_READING_THRESHOLD_S


def test_health_degraded_when_reading_stale():
    server._poll_thread = DummyThread(alive=True)
    server._state["last_reading_monotonic"] = time.monotonic() - (server.STALE_READING_THRESHOLD_S + 1)
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "degraded"
    # poller thread is alive -- it's specifically the stale reading that
    # should drive degraded status here, not thread liveness.
    assert data["poller_alive"] is True
    assert data["last_reading_age_s"] > server.STALE_READING_THRESHOLD_S


def test_poll_sensor_updates_state_on_valid_frame():
    fake = FakeSerial(make_frame(0x00, 0x64))  # 100mm
    processors = {"instantaneous_raw": _PassthroughProcessor()}
    stop_event = threading.Event()

    # Driving the loop body directly would require exporting internals, so
    # instead run the real loop in a thread and stop it once state updates.
    thread = threading.Thread(
        target=server.poll_sensor,
        args=(fake, processors, "instantaneous_raw", stop_event, 0.001),
    )
    thread.start()
    for _ in range(200):
        with server._state_lock:
            if server._state["instantaneous_mm"] == 100:
                break
        time.sleep(0.005)
    stop_event.set()
    thread.join(timeout=1)

    assert server._state["instantaneous_mm"] == 100
    assert server._state["last_reading_monotonic"] is not None


def test_poll_sensor_flushes_buffer_after_prolonged_no_valid_frame():
    # Garbage that never forms a valid frame -- read_frame() will keep
    # returning None forever without a watchdog forcing a resync.
    fake = FakeSerial(bytes([0x01, 0x02, 0x03, 0x04]) * 50)
    stop_event = threading.Event()

    thread = threading.Thread(
        target=server.poll_sensor,
        args=(fake, {}, "primary", stop_event, 0.001),
        kwargs={"stale_threshold_s": 0.02},
    )
    thread.start()
    time.sleep(0.2)
    stop_event.set()
    thread.join(timeout=1)

    assert fake.reset_count > 0


def test_level_returns_503_before_first_reading():
    server._state.update(
        instantaneous_mm=None,
        rolling_avg_mm=None,
        processors={},
    )
    client = server.app.test_client()

    resp = client.get("/level")

    assert resp.status_code == 503


def test_level_returns_current_reading():
    server._state.update(
        instantaneous_mm=101.0,
        rolling_avg_mm=850.0,
        polling_interval_ms=10,
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
    client = server.app.test_client()

    resp = client.get("/level")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {
        "measure_name": "level",
        "units": "cm",
        "polling_interval_ms": 10,
        "primary_signal": {"value": 85.0, "name": "rolling_avg"},
        # rolling_median5 is emit: false -- absent from `signals`.
        "signals": {
            "rolling_avg": 85.0,
            "instantaneous_raw": 10.1,
        },
    }


def test_diag_returns_503_before_first_reading():
    server._state.update(
        instantaneous_mm=None,
        processors={},
    )
    client = server.app.test_client()

    resp = client.get("/diag")

    assert resp.status_code == 503


def test_diag_returns_config_and_output_for_every_processor():
    server._state.update(
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
