import threading
import time
from datetime import datetime, timezone

from pondpi.sensors.base import Sensor

from . import ads1263

# The physical water level moves far slower than this ADC can sample
# (each conversion at ads1263.DEFAULT_DATA_RATE_SPS takes ~10ms) -- this
# just sets how often the background thread below bothers asking, not a
# hardware constraint.
DEFAULT_POLL_INTERVAL_S = 0.5

# How long last_reading_monotonic() may go without a fresh reading
# before check_health() reports unhealthy -- comfortably above
# DEFAULT_POLL_INTERVAL_S so it only trips if the background thread has
# genuinely stopped (died, or the ADC/SPI link went stale), not because
# of one slow poll.
HEALTH_STALE_THRESHOLD_S = 3.0


class EtapeSensor(Sensor):
    """Driver for a Milone eTape liquid-level sensor's 0-3.3V
    Resistance-to-Voltage Module, read via one single-ended channel of a
    Waveshare High-Precision AD HAT (ADS1263 ADC, over SPI -- see this
    package's `ads1263.py`). See README's "Sensor notes" section for the
    physical wiring and how a calibration like the one this class
    applies is derived.

    Unlike `A02YYUWSensor`, this hardware has no software-controllable
    power supply -- the module's Vin is wired directly to the Pi's own
    3.3V rail, not a GPIO -- so `supports_reset` stays False (the
    inherited default) and `reset_hardware()` is never implemented or
    called.

    Reports a single named reading, `"raw"` -- this sensor has no
    onboard smoothing the way the A02YYUW's own "processed" hardware
    mode does; any smoothing happens downstream, in the signal graph,
    same as every other sensor's own raw feed.

    `adc_channel` and the linear calibration (`calibration_slope_cm_per_v`,
    `calibration_intercept_cm`, applied as
    `depth_cm = slope * voltage_v + intercept`) are both required
    construction params -- see `create()` below -- since neither has a
    sensible project-wide default the way the A02YYUW's fixed UART pins
    do: which ADC channel this sensor is wired to, and its calibration,
    are properties of one specific physical eTape unit's own
    installation, not of this driver.

    The calibration coefficients are responsible for orienting the
    result into this project's canonical "distance from the sensor's
    mount point down to the water surface, increasing as the water
    level falls" convention (see `sensors/base.py`'s `Sensor`
    docstring) -- this driver applies them as a pure linear transform
    and has no opinion of its own about which physical direction is
    which; whoever calibrates a given install picks the sign.
    """

    def __init__(
        self,
        adc,
        adc_channel,
        calibration_slope_cm_per_v,
        calibration_intercept_cm,
        poll_interval_s=DEFAULT_POLL_INTERVAL_S,
        health_stale_threshold_s=HEALTH_STALE_THRESHOLD_S,
    ):
        self._adc = adc
        self._adc_channel = adc_channel
        self._slope = calibration_slope_cm_per_v
        self._intercept = calibration_intercept_cm
        self._poll_interval_s = poll_interval_s
        self._health_stale_threshold_s = health_stale_threshold_s
        self._last_readings = {}

        super().__init__()
        # Must be last: this starts a background thread that immediately
        # begins calling self._adc.read_voltage(), so every attribute
        # above must already be set.
        self._begin_polling()

    def read(self, options=None):
        """A thin cache lookup, same shape as `A02YYUWSensor.read()` --
        always instant, never touches the ADC itself. `options` is
        accepted for interface consistency with `Sensor.read()` but
        unused: this driver only ever reports one reading key."""
        return self.last_reading("raw")

    def last_reading(self, key):
        with self._lock:
            return self._last_readings.get(key)

    def check_health(self):
        last = self.last_reading_monotonic()
        if last is None:
            return False
        return (time.monotonic() - last) <= self._health_stale_threshold_s

    def close(self):
        self._stop_event.set()
        self._thread.join(timeout=self._poll_interval_s + 1)
        self._adc.close()

    def _poll_loop(self, stop_event):
        while not stop_event.is_set():
            try:
                voltage_v = self._adc.read_voltage(self._adc_channel)
            except (OSError, TimeoutError):
                # A transient SPI/DRDY hiccup -- skip this sample rather
                # than crashing the poll thread; check_health() catches
                # a *sustained* failure via last_reading_monotonic()
                # going stale, same as A02YYUWSensor's own staleness
                # check does for its UART.
                time.sleep(self._poll_interval_s)
                continue

            depth_cm = self._slope * voltage_v + self._intercept
            distance_mm = depth_cm * 10.0

            with self._lock:
                self._last_readings["raw"] = {"value": distance_mm, "at": datetime.now(timezone.utc).isoformat()}
            self._record_reading()
            time.sleep(self._poll_interval_s)

    def _begin_polling(self):
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._poll_loop, args=(self._stop_event,), daemon=True)
        self._thread.start()


def create(params, simulate):
    """Builds an EtapeSensor from a sensor config entry's `params` dict.

    Recognized params:
      adc_channel (required, integer 0-9) -- which single-ended channel
      of the Waveshare ADS1263 ADC HAT this eTape's Resistance-to-Voltage
      module's Vout is wired to.
      calibration_slope_cm_per_v, calibration_intercept_cm (both
      required) -- this specific physical eTape unit's own linear
      voltage-to-depth fit (`depth_cm = slope * voltage_v + intercept`),
      bench-calibrated per-unit; see README's "Sensor notes" section.
      reference_voltage (default ads1263.DEFAULT_REFERENCE_VOLTAGE) --
      the ADC's own AVDD reference rail voltage (see ads1263.py); only
      needs overriding if a board is remeasured differently than the
      one the default was measured against.
      poll_interval_ms (default 500) -- how often this driver's own
      background thread reads the ADC.
    """
    if "adc_channel" not in params:
        raise ValueError("etape: missing required 'adc_channel'")
    adc_channel = params["adc_channel"]
    if not isinstance(adc_channel, int) or isinstance(adc_channel, bool) or not 0 <= adc_channel <= 9:
        raise ValueError(f"etape: invalid adc_channel {adc_channel!r} (expected an integer 0-9)")

    missing = {"calibration_slope_cm_per_v", "calibration_intercept_cm"} - set(params)
    if missing:
        raise ValueError(f"etape: missing required {sorted(missing)}")
    slope = params["calibration_slope_cm_per_v"]
    intercept = params["calibration_intercept_cm"]

    poll_interval_s = params.get("poll_interval_ms", DEFAULT_POLL_INTERVAL_S * 1000) / 1000
    reference_voltage = params.get("reference_voltage", ads1263.DEFAULT_REFERENCE_VOLTAGE)

    if simulate:
        adc = ads1263.SimulatedADS1263()
    else:
        import spidev
        from gpiozero import DigitalInputDevice, DigitalOutputDevice

        spi = spidev.SpiDev()
        spi.open(0, 0)
        reset_pin = DigitalOutputDevice(ads1263.RST_PIN, initial_value=True)
        cs_pin = DigitalOutputDevice(ads1263.CS_PIN, initial_value=True)
        drdy_pin = DigitalInputDevice(ads1263.DRDY_PIN, pull_up=True)
        adc = ads1263.ADS1263(spi, reset_pin, cs_pin, drdy_pin, reference_voltage=reference_voltage)

    return EtapeSensor(adc, adc_channel, slope, intercept, poll_interval_s=poll_interval_s)
