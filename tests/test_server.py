import server


class DummyThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


def test_health_ok_when_poller_alive():
    server._poll_thread = DummyThread(alive=True)
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["poller_alive"] is True
    assert data["started_at"] == server._started_at.isoformat()
    assert isinstance(data["uptime_seconds"], (int, float))
    assert data["uptime_seconds"] >= 0


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
        instantaneous_mm=None, rolling_avg_mm=None, samples_in_rolling_window=0, rolling_window_size=25
    )
    client = server.app.test_client()

    resp = client.get("/level")

    assert resp.status_code == 503


def test_level_returns_current_reading():
    server._state.update(
        instantaneous_mm=101.0, rolling_avg_mm=850.0, samples_in_rolling_window=25, rolling_window_size=100
    )
    client = server.app.test_client()

    resp = client.get("/level")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {
        "instantaneous_distance_cm": 10.1,
        "rolling_avg_distance_cm": 85.0,
        "rolling_window_size": 100,
        "samples_in_rolling_window": 25,
        "polling_interval_ms": 10,
    }
