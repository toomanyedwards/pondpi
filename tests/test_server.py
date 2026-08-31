from pondpi import server


class DummyThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


def test_health_ok_when_poller_alive():
    server._poll_thread = DummyThread(alive=True)
    server._state["processor_names"] = ["rolling_avg", "instantaneous_raw"]
    server._state["commit_sha"] = "abc123"
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["poller_alive"] is True
    assert data["started_at"] == server._started_at.isoformat()
    assert isinstance(data["uptime_seconds"], (int, float))
    assert data["uptime_seconds"] >= 0
    assert data["uptime_human"] == f"{int(data['uptime_seconds'])}s"
    assert data["processors"] == ["rolling_avg", "instantaneous_raw"]
    assert data["commit_sha"] == "abc123"


def test_health_degraded_when_poller_dead():
    server._poll_thread = DummyThread(alive=False)
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 503
    data = resp.get_json()
    assert data["status"] == "degraded"
    assert data["poller_alive"] is False


def test_health_degraded_when_poller_never_started():
    server._poll_thread = None
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 503


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
        "instantaneous_distance_cm": 10.1,
        "rolling_avg_distance_cm": 85.0,
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
