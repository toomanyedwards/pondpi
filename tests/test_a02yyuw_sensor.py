import threading
import time

import pytest

from pondpi.sensors.a02yyuw_sensor import (
    A02YYUWSensor,
    create,
    read_sensor,
    sensor_mode,
)


class FakeSerial:
    def __init__(self, data=b""):
        self._data = data
        self.reset_count = 0

    @property
    def in_waiting(self):
        return len(self._data)

    def read(self, n):
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

    def reset_input_buffer(self):
        self.reset_count += 1
        self._data = b""

    def close(self):
        pass


class FakeModeController:
    def __init__(self):
        self.calls = []

    def set_mode(self, mode):
        self.calls.append(mode)

    def close(self):
        pass


class FakePowerController:
    def __init__(self):
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def close(self):
        pass


def _frame(data_h, data_l):
    checksum = read_sensor.calculate_checksum(data_h, data_l)
    return bytes([0xFF, data_h, data_l, checksum])


def test_read_returns_raw_reading_for_valid_frame():
    sensor = A02YYUWSensor(FakeSerial(_frame(0x01, 0x2C)), FakeModeController(), FakePowerController())
    assert sensor.read() == {"raw": 0x012C}


def test_read_returns_empty_dict_when_no_frame_available():
    sensor = A02YYUWSensor(FakeSerial(b""), FakeModeController(), FakePowerController())
    assert sensor.read() == {}


def test_read_flushes_buffer_after_prolonged_no_valid_frame():
    ser = FakeSerial(b"")
    sensor = A02YYUWSensor(ser, FakeModeController(), FakePowerController(), stale_threshold_s=0.05)

    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline and ser.reset_count == 0:
        sensor.read()

    assert ser.reset_count > 0


def test_read_cycles_into_processed_mode():
    ser = read_sensor.SimulatedSerial()
    mode_controller = FakeModeController()
    sensor = A02YYUWSensor(
        ser,
        mode_controller,
        FakePowerController(),
        mode_cycle_interval_s=0.1,
        processed_mode_duration_s=0.05,
        mode_settle_s=0.01,
    )

    saw_processed = False
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not saw_processed:
        if "processed" in sensor.read():
            saw_processed = True

    assert saw_processed
    assert sensor_mode.PROCESSED in mode_controller.calls


def test_read_mode_raw_never_switches_to_processed():
    ser = read_sensor.SimulatedSerial()
    mode_controller = FakeModeController()
    sensor = A02YYUWSensor(
        ser,
        mode_controller,
        FakePowerController(),
        mode_cycle_interval_s=0.05,
        processed_mode_duration_s=0.02,
        mode_settle_s=0.005,
        read_mode=sensor_mode.RAW,
    )

    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        assert "processed" not in sensor.read()

    assert mode_controller.calls == [sensor_mode.RAW]


def test_read_mode_processed_never_switches_to_raw():
    ser = read_sensor.SimulatedSerial()
    mode_controller = FakeModeController()
    sensor = A02YYUWSensor(
        ser,
        mode_controller,
        FakePowerController(),
        mode_cycle_interval_s=0.05,
        processed_mode_duration_s=0.02,
        mode_settle_s=0.005,
        read_mode=sensor_mode.PROCESSED,
    )

    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        assert "raw" not in sensor.read()

    assert mode_controller.calls == [sensor_mode.PROCESSED]


def test_create_accepts_valid_read_mode():
    sensor = create({"read_mode": "processed"}, simulate=True)
    assert isinstance(sensor, A02YYUWSensor)


def test_create_rejects_invalid_read_mode():
    with pytest.raises(ValueError, match="invalid read_mode"):
        create({"read_mode": "bogus"}, simulate=True)


def test_reset_delegates_to_power_controller():
    power_controller = FakePowerController()
    sensor = A02YYUWSensor(FakeSerial(), FakeModeController(), power_controller)
    sensor.reset()
    assert power_controller.reset_calls == 1


def test_read_and_reset_are_serialized_against_each_other():
    # A slow reset() (real hardware holds the power pin low for
    # RESET_OFF_DURATION_S) must never overlap with a concurrent
    # read() on another thread -- power-cycling mid-read is what wedged
    # the driver on real hardware (see sensors/base.py's docstring).
    reset_end_time = []

    class SlowPowerController(FakePowerController):
        def reset(self):
            time.sleep(0.05)
            super().reset()
            reset_end_time.append(time.monotonic())

    sensor = A02YYUWSensor(FakeSerial(_frame(0x01, 0x2C)), FakeModeController(), SlowPowerController())

    reset_thread = threading.Thread(target=sensor.reset)
    reset_thread.start()
    time.sleep(0.01)  # let reset() acquire the lock and start its "hardware" delay first

    # This read() call is invoked while reset() is still mid-flight
    # (well within its 0.05s critical section) -- if the two aren't
    # serialized, it would return immediately; if they are, it can't
    # complete until reset() has released the lock.
    sensor.read()
    read_end = time.monotonic()

    reset_thread.join()
    assert read_end >= reset_end_time[0]


def test_supports_reset_is_true():
    sensor = A02YYUWSensor(FakeSerial(), FakeModeController(), FakePowerController())
    assert sensor.supports_reset is True


def test_create_under_simulate_builds_a_working_sensor():
    sensor = create({}, simulate=True)
    assert isinstance(sensor, A02YYUWSensor)


def test_close_closes_serial_mode_and_power_controllers():
    ser = FakeSerial()
    mode_controller = FakeModeController()
    power_controller = FakePowerController()
    sensor = A02YYUWSensor(ser, mode_controller, power_controller)

    closed = []
    ser.close = lambda: closed.append("ser")
    mode_controller.close = lambda: closed.append("mode")
    power_controller.close = lambda: closed.append("power")

    sensor.close()
    assert closed == ["ser", "mode", "power"]


def test_poll_loop_routes_each_reading_to_on_reading():
    # poll_loop()/start() are inherited unchanged from LevelSensor
    # (see sensors/base.py) -- exercised here through the concrete
    # A02YYUWSensor, same as every other behavioral test in this file.
    sensor = A02YYUWSensor(FakeSerial(_frame(0x01, 0x2C)), FakeModeController(), FakePowerController())
    stop_event = threading.Event()
    readings = []

    thread = threading.Thread(
        target=sensor.poll_loop,
        args=(stop_event, 0.001, lambda key, value: readings.append((key, value))),
    )
    thread.start()

    deadline = time.monotonic() + 1
    while not readings and time.monotonic() < deadline:
        time.sleep(0.005)
    stop_event.set()
    thread.join(timeout=1)

    assert readings == [("raw", 0x012C)]


def test_start_spawns_its_own_thread():
    sensor = A02YYUWSensor(FakeSerial(_frame(0x01, 0x2C)), FakeModeController(), FakePowerController())
    stop_event = threading.Event()
    readings = []

    sensor.start(stop_event, 0.001, lambda key, value: readings.append((key, value)))

    deadline = time.monotonic() + 1
    while not readings and time.monotonic() < deadline:
        time.sleep(0.005)
    stop_event.set()

    assert readings == [("raw", 0x012C)]
