import time

import pytest

from pondpi.sensors import sensor_mode
from pondpi.sensors.a02yyuw_sensor import A02YYUWSensor, create


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

    def close(self):
        pass


def make_frame(data_h, data_l, checksum=None):
    if checksum is None:
        checksum = (0xFF + data_h + data_l) & 0xFF
    return bytes([0xFF, data_h, data_l, checksum])


class FakeModeController:
    def __init__(self):
        self.calls = []

    def set_mode(self, mode):
        self.calls.append(mode)

    def close(self):
        pass


class FakePowerController:
    def __init__(self):
        self.reset_calls = []

    def reset(self, off_duration_s=None):
        self.reset_calls.append(off_duration_s)

    def close(self):
        pass


def test_read_returns_raw_reading_for_valid_frame():
    fake_serial = FakeSerial(make_frame(0x00, 0x64))  # 100mm
    sensor = A02YYUWSensor(fake_serial, FakeModeController(), FakePowerController())

    assert sensor.read() == {"raw": 100}


def test_read_returns_empty_dict_when_no_frame_available():
    sensor = A02YYUWSensor(FakeSerial(b""), FakeModeController(), FakePowerController())

    assert sensor.read() == {}


def test_read_flushes_buffer_after_prolonged_no_valid_frame():
    # Garbage that never forms a valid frame -- read_frame() will keep
    # returning None forever without a watchdog forcing a resync.
    fake_serial = FakeSerial(bytes([0x01, 0x02, 0x03, 0x04]) * 50)
    sensor = A02YYUWSensor(fake_serial, FakeModeController(), FakePowerController(), stale_threshold_s=0.02)

    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline and fake_serial.reset_count == 0:
        sensor.read()

    assert fake_serial.reset_count > 0


def test_read_cycles_into_processed_mode():
    # A long repeating stream of the same valid frame (100mm) -- long
    # enough to cover several mode-cycle iterations at this test's tiny
    # cycle interval.
    fake_serial = FakeSerial(make_frame(0x00, 0x64) * 5000)
    mode_controller = FakeModeController()
    sensor = A02YYUWSensor(
        fake_serial,
        mode_controller,
        FakePowerController(),
        mode_cycle_interval_s=0.1,
        processed_mode_duration_s=0.05,
        mode_settle_s=0.01,
    )

    # Paced like a real poll loop -- a tight busy-loop would drain the
    # (finite) fake frame buffer almost instantly, well before real time
    # ever crosses a mode-cycle boundary.
    deadline = time.monotonic() + 1.0
    saw_processed = False
    while time.monotonic() < deadline and not saw_processed:
        if sensor.read().get("processed") == 100:
            saw_processed = True
        time.sleep(0.001)

    assert saw_processed
    # Starts in raw (so a freshly-constructed driver always defaults to
    # raw, regardless of wall-clock time), and reaches processed at least
    # once.
    assert mode_controller.calls[0] == sensor_mode.RAW
    assert sensor_mode.PROCESSED in mode_controller.calls


def test_read_mode_raw_never_switches_to_processed():
    fake_serial = FakeSerial(make_frame(0x00, 0x64) * 5000)
    mode_controller = FakeModeController()
    sensor = A02YYUWSensor(
        fake_serial,
        mode_controller,
        FakePowerController(),
        mode_cycle_interval_s=0.05,
        processed_mode_duration_s=0.02,
        mode_settle_s=0.01,
        read_mode=sensor_mode.RAW,
    )

    deadline = time.monotonic() + 0.3
    readings = []
    while time.monotonic() < deadline:
        readings.append(sensor.read())
        time.sleep(0.001)

    assert any(r.get("raw") == 100 for r in readings)
    assert not any("processed" in r for r in readings)
    # Only ever set once, at construction -- no mid-run switching.
    assert mode_controller.calls == [sensor_mode.RAW]


def test_read_mode_processed_never_switches_to_raw():
    fake_serial = FakeSerial(make_frame(0x00, 0x64) * 5000)
    mode_controller = FakeModeController()
    sensor = A02YYUWSensor(
        fake_serial,
        mode_controller,
        FakePowerController(),
        mode_cycle_interval_s=0.05,
        processed_mode_duration_s=0.02,
        mode_settle_s=0.01,
        read_mode=sensor_mode.PROCESSED,
    )

    deadline = time.monotonic() + 0.3
    readings = []
    while time.monotonic() < deadline:
        readings.append(sensor.read())
        time.sleep(0.001)

    assert any(r.get("processed") == 100 for r in readings)
    assert not any("raw" in r for r in readings)
    assert mode_controller.calls == [sensor_mode.PROCESSED]


def test_create_accepts_valid_read_mode():
    sensor = create({"read_mode": "processed"}, simulate=True)

    assert isinstance(sensor, A02YYUWSensor)
    readings = [sensor.read() for _ in range(10)]
    assert any("processed" in r for r in readings)
    assert not any("raw" in r for r in readings)


def test_create_rejects_invalid_read_mode():
    with pytest.raises(ValueError, match="invalid read_mode 'smoothed'"):
        create({"read_mode": "smoothed"}, simulate=True)


def test_reset_delegates_to_power_controller():
    power_controller = FakePowerController()
    sensor = A02YYUWSensor(FakeSerial(b""), FakeModeController(), power_controller)

    sensor.reset()

    assert len(power_controller.reset_calls) == 1


def test_supports_reset_is_true():
    sensor = A02YYUWSensor(FakeSerial(b""), FakeModeController(), FakePowerController())

    assert sensor.supports_reset is True


def test_create_under_simulate_builds_a_working_sensor():
    sensor = create({}, simulate=True)

    assert isinstance(sensor, A02YYUWSensor)
    # A real (simulated) reading should come back within a few calls --
    # confirms create() wired up a genuinely functional SimulatedSerial,
    # not just an object of the right type.
    readings = [sensor.read() for _ in range(10)]
    assert any("raw" in r for r in readings)


def test_close_closes_serial_mode_and_power_controllers():
    class TrackingSerial(FakeSerial):
        def __init__(self):
            super().__init__(b"")
            self.closed = False

        def close(self):
            self.closed = True

    fake_serial = TrackingSerial()
    mode_controller = FakeModeController()
    power_controller = FakePowerController()
    sensor = A02YYUWSensor(fake_serial, mode_controller, power_controller)

    closed_calls = []
    mode_controller.close = lambda: closed_calls.append("mode")
    power_controller.close = lambda: closed_calls.append("power")

    sensor.close()

    assert fake_serial.closed is True
    assert "mode" in closed_calls
    assert "power" in closed_calls
