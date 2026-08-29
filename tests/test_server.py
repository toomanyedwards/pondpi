import server


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
