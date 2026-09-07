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


# Every A02YYUWSensor now starts its own background read thread the
# moment it's constructed (see Sensor.__init__()) -- these tests
# construct it and poll last_reading()/last_reading_monotonic() for
# what arrives, rather than calling read() directly and racing that
# thread. poll_interval_s is set small (a few ms) so tests don't wait
# long, or very large (parking the thread asleep after its one initial
# call) for tests that only care about a single deterministic read().


def test_reports_raw_reading_for_valid_frame():
    sensor = A02YYUWSensor(
        FakeSerial(_frame(0x01, 0x2C)),
        FakeModeController(),
        FakePowerController(),
        poll_interval_s=0.001,
    )

    _wait_until(lambda: sensor.last_reading("raw") is not None)
    assert sensor.last_reading("raw")["value"] == 0x012C


def test_read_returns_none_before_any_reading():
    sensor = A02YYUWSensor(FakeSerial(b""), FakeModeController(), FakePowerController(), poll_interval_s=1000)
    assert sensor.read() is None


def test_check_health_false_before_any_reading():
    sensor = A02YYUWSensor(FakeSerial(b""), FakeModeController(), FakePowerController(), poll_interval_s=1000)
    assert sensor.check_health() is False
    assert sensor.is_healthy() is False


def test_check_health_false_when_only_raw_has_arrived():
    # A fixed-mode fake frame source only ever populates "raw" (this
    # driver's cycling logic still starts in raw mode regardless of the
    # frame data itself) -- "processed" never arrives, so check_health()
    # must stay False even though raw is fine.
    sensor = A02YYUWSensor(
        FakeSerial(_frame(0x01, 0x2C)),
        FakeModeController(),
        FakePowerController(),
        poll_interval_s=0.001,
    )

    _wait_until(lambda: sensor.last_reading("raw") is not None)

    assert sensor.last_reading("processed") is None
    assert sensor.check_health() is False


def test_check_health_true_once_both_readings_arrive():
    sensor = A02YYUWSensor(
        read_sensor.SimulatedSerial(),
        FakeModeController(),
        FakePowerController(),
        poll_interval_s=0.001,
        mode_cycle_interval_s=0.1,
        processed_mode_duration_s=0.05,
        mode_settle_s=0.01,
    )

    _wait_until(lambda: sensor.last_reading("processed") is not None, timeout_s=1.0)

    assert sensor.check_health() is True
    assert sensor.is_healthy() is True


def test_check_health_false_when_last_reading_is_older_than_health_threshold():
    # Both readings have arrived (satisfying the presence check), but
    # last_reading_monotonic() is stale beyond health_stale_threshold_s
    # -- e.g. the background thread has since died -- so check_health()
    # must catch that even though a pure presence check wouldn't.
    sensor = A02YYUWSensor(
        read_sensor.SimulatedSerial(),
        FakeModeController(),
        FakePowerController(),
        poll_interval_s=0.001,
        mode_cycle_interval_s=0.1,
        processed_mode_duration_s=0.05,
        mode_settle_s=0.01,
        health_stale_threshold_s=0.05,
    )
    _wait_until(lambda: sensor.last_reading("processed") is not None, timeout_s=1.0)
    sensor._stop_event.set()  # freeze the poll loop -- no further readings will arrive
    sensor._thread.join(timeout=1)

    with sensor._lock:
        sensor._last_reading_monotonic = time.monotonic() - 1000

    assert sensor.check_health() is False


def test_check_health_true_within_health_threshold_even_with_a_custom_value():
    sensor = A02YYUWSensor(
        read_sensor.SimulatedSerial(),
        FakeModeController(),
        FakePowerController(),
        poll_interval_s=0.001,
        mode_cycle_interval_s=0.1,
        processed_mode_duration_s=0.05,
        mode_settle_s=0.01,
        health_stale_threshold_s=10,
    )
    _wait_until(lambda: sensor.last_reading("processed") is not None, timeout_s=1.0)
    sensor._stop_event.set()  # freeze the poll loop -- no further readings will arrive
    sensor._thread.join(timeout=1)

    with sensor._lock:
        sensor._last_reading_monotonic = time.monotonic() - 5  # stale, but within this sensor's own 10s threshold

    assert sensor.check_health() is True


def test_read_defaults_to_raw_mode_when_no_options_given():
    sensor = A02YYUWSensor(
        FakeSerial(_frame(0x01, 0x2C)),
        FakeModeController(),
        FakePowerController(),
        poll_interval_s=0.001,
    )

    _wait_until(lambda: sensor.last_reading("raw") is not None)
    assert sensor.read()["value"] == 0x012C


def test_read_returns_the_reading_for_the_mode_options_selects():
    sensor = A02YYUWSensor(
        read_sensor.SimulatedSerial(),
        FakeModeController(),
        FakePowerController(),
        poll_interval_s=0.001,
        mode_cycle_interval_s=0.1,
        processed_mode_duration_s=0.05,
        mode_settle_s=0.01,
    )

    _wait_until(lambda: sensor.last_reading("processed") is not None, timeout_s=1.0)

    raw_reading = sensor.read({"read_mode": "raw"})
    processed_reading = sensor.read({"read_mode": "processed"})
    assert raw_reading == sensor.last_reading("raw")
    assert processed_reading == sensor.last_reading("processed")
    assert raw_reading != processed_reading


def test_read_does_not_block_while_read_hardware_holds_the_lock():
    # read() is now a plain cache lookup -- it must never contend with
    # _hardware_lock, unlike the old read() that did the UART work
    # itself and could block behind a concurrent reset_hardware().
    class SlowSerial(FakeSerial):
        def read(self, n):
            time.sleep(0.2)
            return super().read(n)

    sensor = A02YYUWSensor(SlowSerial(_frame(0x01, 0x2C)), FakeModeController(), FakePowerController(), poll_interval_s=1000)

    hardware_thread = threading.Thread(target=sensor._read_hardware)
    hardware_thread.start()
    time.sleep(0.02)  # let _read_hardware() acquire the lock and start its slow "hardware" read

    start = time.monotonic()
    sensor.read()
    elapsed = time.monotonic() - start

    hardware_thread.join()
    assert elapsed < 0.1


def test_reports_nothing_when_no_frame_available():
    sensor = A02YYUWSensor(
        FakeSerial(b""),
        FakeModeController(),
        FakePowerController(),
        poll_interval_s=0.001,
    )

    time.sleep(0.05)
    assert sensor.last_reading("raw") is None
    assert sensor.last_reading("processed") is None


def test_flushes_buffer_after_prolonged_no_valid_frame():
    ser = FakeSerial(b"")
    A02YYUWSensor(
        ser,
        FakeModeController(),
        FakePowerController(),
        poll_interval_s=0.001,
        stale_threshold_s=0.05,
    )

    _wait_until(lambda: ser.reset_count > 0, timeout_s=0.5)
    assert ser.reset_count > 0


def test_cycles_into_processed_mode():
    ser = read_sensor.SimulatedSerial()
    mode_controller = FakeModeController()
    sensor = A02YYUWSensor(
        ser,
        mode_controller,
        FakePowerController(),
        poll_interval_s=0.001,
        mode_cycle_interval_s=0.1,
        processed_mode_duration_s=0.05,
        mode_settle_s=0.01,
    )

    _wait_until(lambda: sensor.last_reading("processed") is not None, timeout_s=1.0)

    assert sensor.last_reading("processed") is not None
    assert sensor_mode.PROCESSED in mode_controller.calls


def test_read_mode_raw_never_switches_to_processed():
    ser = read_sensor.SimulatedSerial()
    mode_controller = FakeModeController()
    sensor = A02YYUWSensor(
        ser,
        mode_controller,
        FakePowerController(),
        poll_interval_s=0.001,
        mode_cycle_interval_s=0.05,
        processed_mode_duration_s=0.02,
        mode_settle_s=0.005,
        read_mode=sensor_mode.RAW,
    )

    time.sleep(0.3)

    assert sensor.last_reading("processed") is None
    assert mode_controller.calls == [sensor_mode.RAW]


def test_read_mode_processed_never_switches_to_raw():
    ser = read_sensor.SimulatedSerial()
    mode_controller = FakeModeController()
    sensor = A02YYUWSensor(
        ser,
        mode_controller,
        FakePowerController(),
        poll_interval_s=0.001,
        mode_cycle_interval_s=0.05,
        processed_mode_duration_s=0.02,
        mode_settle_s=0.005,
        read_mode=sensor_mode.PROCESSED,
    )

    time.sleep(0.3)

    assert sensor.last_reading("raw") is None
    assert mode_controller.calls == [sensor_mode.PROCESSED]


def test_create_accepts_valid_read_mode():
    sensor = create({"read_mode": "processed"}, simulate=True)
    assert isinstance(sensor, A02YYUWSensor)


def test_create_rejects_invalid_read_mode():
    with pytest.raises(ValueError, match="invalid read_mode"):
        create({"read_mode": "bogus"}, simulate=True)


def test_reset_delegates_to_power_controller():
    power_controller = FakePowerController()
    sensor = A02YYUWSensor(FakeSerial(), FakeModeController(), power_controller, poll_interval_s=0.01)

    sensor.reset()
    assert power_controller.reset_calls == 1


def test_reset_restarts_polling_with_a_fresh_thread():
    power_controller = FakePowerController()
    sensor = A02YYUWSensor(
        read_sensor.SimulatedSerial(),
        FakeModeController(),
        power_controller,
        poll_interval_s=0.005,
    )
    _wait_until(lambda: sensor.last_reading("raw") is not None)
    assert sensor.last_reset_at() is None

    sensor.reset()
    assert power_controller.reset_calls == 1
    # last_reset_at() is inherited unchanged from Sensor -- proving
    # it's actually wired up through this concrete driver, not just the
    # abstract base (see test_sensors.py for the base-class behavior itself).
    assert sensor.last_reset_at() is not None

    # A fresh reading arrives after reset() -- proves the read thread
    # actually restarted, not just that the hardware was power-cycled.
    _wait_until(lambda: sensor.last_reading("raw") is not None)
    assert sensor.last_reading("raw") is not None


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
        poll_interval_s=1000,
    )

    reset_thread = threading.Thread(target=sensor.reset_hardware)
    reset_thread.start()
    time.sleep(0.01)  # let reset_hardware() acquire the lock and start its "hardware" delay first

    # This _read_hardware() call is invoked while reset_hardware() is
    # still mid-flight (well within its 0.05s critical section) -- if
    # the two aren't serialized, it would return immediately; if they
    # are, it can't complete until reset_hardware() has released the
    # lock. (Not sensor.read() -- that's now a plain cache lookup that
    # never touches _hardware_lock at all.)
    sensor._read_hardware()
    read_end = time.monotonic()

    reset_thread.join()
    assert read_end >= reset_end_time[0]


def test_supports_reset_is_true():
    sensor = A02YYUWSensor(FakeSerial(), FakeModeController(), FakePowerController(), poll_interval_s=1000)
    assert sensor.supports_reset is True


def test_create_under_simulate_builds_a_working_sensor():
    sensor = create({}, simulate=True)
    assert isinstance(sensor, A02YYUWSensor)


def test_close_closes_serial_mode_and_power_controllers():
    ser = FakeSerial()
    mode_controller = FakeModeController()
    power_controller = FakePowerController()
    sensor = A02YYUWSensor(ser, mode_controller, power_controller, poll_interval_s=1000)

    closed = []
    ser.close = lambda: closed.append("ser")
    mode_controller.close = lambda: closed.append("mode")
    power_controller.close = lambda: closed.append("power")

    sensor.close()
    assert closed == ["ser", "mode", "power"]
