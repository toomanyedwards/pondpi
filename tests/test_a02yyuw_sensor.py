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


def _wait_until(predicate, timeout_s=1):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.002)


def _no_op(reading_key, distance_mm):
    pass


# Every A02YYUWSensor now starts its own background read thread the
# moment it's constructed (see LevelSensor.__init__()) -- these tests
# construct with a recording (or no-op) on_reading and observe what
# arrives, rather than calling read() directly and racing that thread.
# poll_interval_s is set small (a few ms) so tests don't wait long, or
# very large (parking the thread asleep after its one initial call) for
# tests that only care about a single deterministic read().


def test_reports_raw_reading_for_valid_frame():
    readings = []
    A02YYUWSensor(
        FakeSerial(_frame(0x01, 0x2C)),
        FakeModeController(),
        FakePowerController(),
        on_reading=lambda k, v: readings.append((k, v)),
        poll_interval_s=0.001,
    )

    _wait_until(lambda: readings)
    assert readings == [("raw", 0x012C)]


def test_reports_nothing_when_no_frame_available():
    readings = []
    A02YYUWSensor(
        FakeSerial(b""),
        FakeModeController(),
        FakePowerController(),
        on_reading=lambda k, v: readings.append((k, v)),
        poll_interval_s=0.001,
    )

    time.sleep(0.05)
    assert readings == []


def test_flushes_buffer_after_prolonged_no_valid_frame():
    ser = FakeSerial(b"")
    A02YYUWSensor(
        ser,
        FakeModeController(),
        FakePowerController(),
        on_reading=_no_op,
        poll_interval_s=0.001,
        stale_threshold_s=0.05,
    )

    _wait_until(lambda: ser.reset_count > 0, timeout_s=0.5)
    assert ser.reset_count > 0


def test_cycles_into_processed_mode():
    ser = read_sensor.SimulatedSerial()
    mode_controller = FakeModeController()
    readings = []
    A02YYUWSensor(
        ser,
        mode_controller,
        FakePowerController(),
        on_reading=lambda k, v: readings.append((k, v)),
        poll_interval_s=0.001,
        mode_cycle_interval_s=0.1,
        processed_mode_duration_s=0.05,
        mode_settle_s=0.01,
    )

    _wait_until(lambda: any(key == "processed" for key, _ in readings), timeout_s=1.0)

    assert any(key == "processed" for key, _ in readings)
    assert sensor_mode.PROCESSED in mode_controller.calls


def test_read_mode_raw_never_switches_to_processed():
    ser = read_sensor.SimulatedSerial()
    mode_controller = FakeModeController()
    readings = []
    A02YYUWSensor(
        ser,
        mode_controller,
        FakePowerController(),
        on_reading=lambda k, v: readings.append((k, v)),
        poll_interval_s=0.001,
        mode_cycle_interval_s=0.05,
        processed_mode_duration_s=0.02,
        mode_settle_s=0.005,
        read_mode=sensor_mode.RAW,
    )

    time.sleep(0.3)

    assert all(key != "processed" for key, _ in readings)
    assert mode_controller.calls == [sensor_mode.RAW]


def test_read_mode_processed_never_switches_to_raw():
    ser = read_sensor.SimulatedSerial()
    mode_controller = FakeModeController()
    readings = []
    A02YYUWSensor(
        ser,
        mode_controller,
        FakePowerController(),
        on_reading=lambda k, v: readings.append((k, v)),
        poll_interval_s=0.001,
        mode_cycle_interval_s=0.05,
        processed_mode_duration_s=0.02,
        mode_settle_s=0.005,
        read_mode=sensor_mode.PROCESSED,
    )

    time.sleep(0.3)

    assert all(key != "raw" for key, _ in readings)
    assert mode_controller.calls == [sensor_mode.PROCESSED]


def test_create_accepts_valid_read_mode():
    sensor = create({"read_mode": "processed"}, simulate=True, on_reading=_no_op)
    assert isinstance(sensor, A02YYUWSensor)


def test_create_rejects_invalid_read_mode():
    with pytest.raises(ValueError, match="invalid read_mode"):
        create({"read_mode": "bogus"}, simulate=True, on_reading=_no_op)


def test_reset_delegates_to_power_controller():
    power_controller = FakePowerController()
    sensor = A02YYUWSensor(
        FakeSerial(), FakeModeController(), power_controller, on_reading=_no_op, poll_interval_s=0.01
    )

    sensor.reset()
    assert power_controller.reset_calls == 1


def test_reset_restarts_polling_with_a_fresh_thread():
    power_controller = FakePowerController()
    readings = []
    sensor = A02YYUWSensor(
        read_sensor.SimulatedSerial(),
        FakeModeController(),
        power_controller,
        on_reading=lambda k, v: readings.append((k, v)),
        poll_interval_s=0.005,
    )
    _wait_until(lambda: readings)
    assert sensor.last_reset_at() is None

    sensor.reset()
    assert power_controller.reset_calls == 1
    # last_reset_at() is inherited unchanged from LevelSensor -- proving
    # it's actually wired up through this concrete driver, not just the
    # abstract base (see test_sensors.py for the base-class behavior itself).
    assert sensor.last_reset_at() is not None

    # A fresh reading arrives after reset() -- proves the read thread
    # actually restarted, not just that the hardware was power-cycled.
    readings.clear()
    _wait_until(lambda: readings)
    assert readings


def test_read_and_reset_hardware_are_serialized_against_each_other():
    # A slow reset_hardware() (real hardware holds the power pin low
    # for RESET_OFF_DURATION_S) must never overlap with a concurrent
    # read() on another thread -- power-cycling mid-read is what wedged
    # the driver on real hardware (see sensors/base.py's docstring).
    # poll_interval_s is set very high so this sensor's own
    # auto-started thread does its one initial read() then parks
    # itself asleep, letting this test drive read()/reset_hardware()
    # directly and deterministically from threads it controls.
    reset_end_time = []

    class SlowPowerController(FakePowerController):
        def reset(self):
            time.sleep(0.05)
            super().reset()
            reset_end_time.append(time.monotonic())

    sensor = A02YYUWSensor(
        FakeSerial(_frame(0x01, 0x2C)),
        FakeModeController(),
        SlowPowerController(),
        on_reading=_no_op,
        poll_interval_s=1000,
    )

    reset_thread = threading.Thread(target=sensor.reset_hardware)
    reset_thread.start()
    time.sleep(0.01)  # let reset_hardware() acquire the lock and start its "hardware" delay first

    # This read() call is invoked while reset_hardware() is still
    # mid-flight (well within its 0.05s critical section) -- if the two
    # aren't serialized, it would return immediately; if they are, it
    # can't complete until reset_hardware() has released the lock.
    sensor.read()
    read_end = time.monotonic()

    reset_thread.join()
    assert read_end >= reset_end_time[0]


def test_supports_reset_is_true():
    sensor = A02YYUWSensor(
        FakeSerial(), FakeModeController(), FakePowerController(), on_reading=_no_op, poll_interval_s=1000
    )
    assert sensor.supports_reset is True


def test_create_under_simulate_builds_a_working_sensor():
    sensor = create({}, simulate=True, on_reading=_no_op)
    assert isinstance(sensor, A02YYUWSensor)


def test_close_closes_serial_mode_and_power_controllers():
    ser = FakeSerial()
    mode_controller = FakeModeController()
    power_controller = FakePowerController()
    sensor = A02YYUWSensor(ser, mode_controller, power_controller, on_reading=_no_op, poll_interval_s=1000)

    closed = []
    ser.close = lambda: closed.append("ser")
    mode_controller.close = lambda: closed.append("mode")
    power_controller.close = lambda: closed.append("power")

    sensor.close()
    assert closed == ["ser", "mode", "power"]
