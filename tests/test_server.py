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
    assert resp.get_json() == {"status": "ok", "poller_alive": True}


def test_health_degraded_when_poller_dead():
    server._poll_thread = DummyThread(alive=False)
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 503
    assert resp.get_json() == {"status": "degraded", "poller_alive": False}


def test_health_degraded_when_poller_never_started():
    server._poll_thread = None
    client = server.app.test_client()

    resp = client.get("/health")

    assert resp.status_code == 503


def test_level_returns_503_before_first_reading():
    server._state.update(distance_mm=None, distance_cm=None, reading_count=0, window_size=25)
    client = server.app.test_client()

    resp = client.get("/level")

    assert resp.status_code == 503


def test_level_returns_current_reading():
    server._state.update(distance_mm=850.0, distance_cm=85.0, reading_count=25, window_size=25)
    client = server.app.test_client()

    resp = client.get("/level")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {
        "distance_mm": 850,
        "distance_cm": 85.0,
        "reading_count": 25,
        "window_size": 25,
    }
