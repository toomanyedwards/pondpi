import threading
import time

import pytest

from pondpi.sensors.etape_sensor import EtapeSensor, ads1263, create


class FakeADC:
    def __init__(self, voltage=1.0):
        self.voltage = voltage
        self.channels_read = []
        self.closed = False
        self._raise = None

    def read_voltage(self, channel):
        self.channels_read.append(channel)
        if self._raise is not None:
            raise self._raise
        return self.voltage

    def close(self):
        self.closed = True


def _wait_until(predicate, timeout_s=1):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.002)


def test_reports_raw_reading_from_calibrated_voltage():
    # depth_cm = 10 * 1.0 + 0.5 = 10.5cm -> 105.0mm
    sensor = EtapeSensor(FakeADC(voltage=1.0), 9, 10.0, 0.5, poll_interval_s=0.001)

    _wait_until(lambda: sensor.last_reading("raw") is not None)
    assert sensor.last_reading("raw")["value"] == pytest.approx(105.0)


def _never_ready_adc():
    # Unlike FakeADC (which always has a value ready), this never
    # produces a reading -- used for "before any reading has arrived"
    # tests, since (unlike A02YYUWSensor's fake serial) a plain FakeADC
    # would otherwise get its first successful reading on the poll
    # loop's very first iteration, before poll_interval_s=1000 ever
    # gets a chance to park it.
    adc = FakeADC()
    adc._raise = OSError("no reading yet")
    return adc


def test_read_returns_none_before_any_reading():
    sensor = EtapeSensor(_never_ready_adc(), 9, 10.0, 0.0, poll_interval_s=1000)
    time.sleep(0.02)
    assert sensor.read() is None


def test_read_is_a_cache_lookup_for_raw():
    sensor = EtapeSensor(FakeADC(voltage=2.0), 9, 5.0, 1.0, poll_interval_s=0.001)
    _wait_until(lambda: sensor.last_reading("raw") is not None)
    assert sensor.read() == sensor.last_reading("raw")


def test_polls_the_configured_channel():
    adc = FakeADC()
    EtapeSensor(adc, 7, 10.0, 0.0, poll_interval_s=0.001)
    _wait_until(lambda: len(adc.channels_read) > 0)
    assert adc.channels_read[0] == 7


def test_check_health_false_before_any_reading():
    sensor = EtapeSensor(_never_ready_adc(), 9, 10.0, 0.0, poll_interval_s=1000)
    time.sleep(0.02)
    assert sensor.check_health() is False
    assert sensor.is_healthy() is False


def test_check_health_true_once_a_reading_arrives():
    sensor = EtapeSensor(FakeADC(), 9, 10.0, 0.0, poll_interval_s=0.001)
    _wait_until(lambda: sensor.last_reading("raw") is not None)
    assert sensor.check_health() is True


def test_check_health_false_when_stale():
    sensor = EtapeSensor(FakeADC(), 9, 10.0, 0.0, poll_interval_s=0.001, health_stale_threshold_s=0.05)
    _wait_until(lambda: sensor.last_reading("raw") is not None)
    sensor._stop_event.set()
    sensor._thread.join(timeout=1)

    with sensor._lock:
        sensor._last_reading_monotonic = time.monotonic() - 1000

    assert sensor.check_health() is False


def test_negative_calibration_intercept_can_produce_negative_distance():
    # The calibration is a pure linear transform -- this driver has no
    # opinion about sign, that's entirely on whoever calibrates a given
    # install (see EtapeSensor's own docstring).
    sensor = EtapeSensor(FakeADC(voltage=0.0), 9, 1.0, -50.0, poll_interval_s=0.001)
    _wait_until(lambda: sensor.last_reading("raw") is not None)
    assert sensor.last_reading("raw")["value"] == pytest.approx(-500.0)


def test_poll_loop_skips_a_sample_on_transient_read_error():
    adc = FakeADC()
    adc._raise = OSError("checksum mismatch")
    sensor = EtapeSensor(adc, 9, 10.0, 0.0, poll_interval_s=0.001)

    time.sleep(0.02)
    assert sensor.last_reading("raw") is None
    assert sensor.check_health() is False

    # Recovers once the ADC starts returning real values again.
    adc._raise = None
    _wait_until(lambda: sensor.last_reading("raw") is not None)
    assert sensor.last_reading("raw") is not None


def test_supports_reset_is_false():
    sensor = EtapeSensor(FakeADC(), 9, 10.0, 0.0, poll_interval_s=1000)
    assert sensor.supports_reset is False


def test_close_stops_the_poll_thread_and_closes_the_adc():
    adc = FakeADC()
    sensor = EtapeSensor(adc, 9, 10.0, 0.0, poll_interval_s=0.005)
    _wait_until(lambda: sensor.last_reading("raw") is not None)

    sensor.close()

    assert adc.closed is True
    assert not sensor._thread.is_alive()


def test_read_does_not_block_while_poll_loop_holds_the_lock():
    release = threading.Event()

    class SlowADC(FakeADC):
        def read_voltage(self, channel):
            release.wait(timeout=5)
            return super().read_voltage(channel)

    sensor = EtapeSensor(SlowADC(), 9, 10.0, 0.0, poll_interval_s=1000)

    start = time.monotonic()
    result = sensor.read()
    elapsed = time.monotonic() - start

    release.set()
    assert result is None
    assert elapsed < 0.1


# -- create() ---------------------------------------------------------


def test_create_under_simulate_builds_a_working_sensor():
    sensor = create({"adc_channel": 9, "calibration_slope_cm_per_v": 9.867, "calibration_intercept_cm": -0.530}, simulate=True)
    assert isinstance(sensor, EtapeSensor)
    assert isinstance(sensor._adc, ads1263.SimulatedADS1263)
    sensor.close()


def test_create_requires_adc_channel():
    with pytest.raises(ValueError, match="adc_channel"):
        create({"calibration_slope_cm_per_v": 1.0, "calibration_intercept_cm": 0.0}, simulate=True)


@pytest.mark.parametrize("channel", [-1, 10, 3.5, "9", True])
def test_create_rejects_invalid_adc_channel(channel):
    with pytest.raises(ValueError, match="adc_channel"):
        create(
            {"adc_channel": channel, "calibration_slope_cm_per_v": 1.0, "calibration_intercept_cm": 0.0},
            simulate=True,
        )


def test_create_requires_calibration_slope_and_intercept():
    with pytest.raises(ValueError, match="calibration_slope_cm_per_v"):
        create({"adc_channel": 9}, simulate=True)


def test_create_passes_calibration_through_to_the_sensor():
    sensor = create(
        {"adc_channel": 4, "calibration_slope_cm_per_v": 2.0, "calibration_intercept_cm": 1.0, "poll_interval_ms": 5},
        simulate=True,
    )
    try:
        _wait_until(lambda: sensor.last_reading("raw") is not None)
        # SimulatedADS1263's voltage is always >= 0 (see ads1263.py), so
        # a positive slope/intercept guarantees a positive distance --
        # this just proves the configured coefficients were actually
        # wired through, not the exact synthetic value.
        assert sensor.last_reading("raw")["value"] > 0
    finally:
        sensor.close()


def test_create_uses_the_configured_adc_channel():
    sensor = create(
        {"adc_channel": 6, "calibration_slope_cm_per_v": 1.0, "calibration_intercept_cm": 0.0, "poll_interval_ms": 1000},
        simulate=True,
    )
    try:
        assert sensor._adc_channel == 6
    finally:
        sensor.close()
