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

# The real ADS1263 hardware path's shared state -- see
# `_get_or_build_shared_adc()` below for why this exists. Only ever
# populated under `simulate=False`; `create()`'s simulated path never
# touches these.
_shared_adc = None
_shared_adc_reference_voltage = None
_shared_adc_lock = threading.Lock()


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
        self._last_voltage_v = None

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

    def extra_diag(self):
        """Overrides `Sensor.extra_diag()` (empty by default) to surface
        the ADC's last raw voltage reading, unrounded -- unlike
        `read()`'s `value` (which server.py's `_signal_output()` always
        rounds to 1 decimal place for display), `GET /diag`/`GET
        /sensors/<name>/diag` pass `extra_diag()`'s own dict straight
        through with no rounding at all. That precision matters
        specifically for calibrating a fresh physical unit: a
        0.1cm-rounded depth reading (computed from whatever calibration
        happens to be configured, possibly still a wrong placeholder --
        see README) can't reliably be inverted back into a precise
        voltage to fit a new line against; reading this field directly
        sidesteps needing a correct calibration to already be in place
        just to calibrate at all."""
        with self._lock:
            return {"last_voltage_v": self._last_voltage_v}

    def close(self):
        """Stops this sensor's own poll thread, then closes `self._adc`
        -- safe even when another `EtapeSensor` shares the same real
        `ADS1263` (see `_get_or_build_shared_adc()`), since `ADS1263.
        close()` is itself idempotent and only actually tears down the
        SPI/GPIO handles on the first call, no matter how many sensors
        call this."""
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
                self._last_voltage_v = voltage_v
            self._record_reading()
            time.sleep(self._poll_interval_s)

    def _begin_polling(self):
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._poll_loop, args=(self._stop_event,), daemon=True)
        self._thread.start()


def _build_real_adc(reference_voltage):
    """Constructs one real `ads1263.ADS1263` against the Waveshare HAT's
    fixed SPI bus and RST/CS/DRDY pins. Only ever called (at most once
    per process) from `_get_or_build_shared_adc()` below -- never call
    this directly from `create()`."""
    import spidev
    from gpiozero import DigitalInputDevice, DigitalOutputDevice

    spi = spidev.SpiDev()
    spi.open(0, 0)
    reset_pin = DigitalOutputDevice(ads1263.RST_PIN, initial_value=True)
    cs_pin = DigitalOutputDevice(ads1263.CS_PIN, initial_value=True)
    drdy_pin = DigitalInputDevice(ads1263.DRDY_PIN, pull_up=True)
    return ads1263.ADS1263(spi, reset_pin, cs_pin, drdy_pin, reference_voltage=reference_voltage)


def _get_or_build_shared_adc(reference_voltage, build_adc=_build_real_adc):
    """Every real (non-simulated) `etape` sensor config entry reads a
    different single-ended channel of the *same* physical ADS1263 HAT --
    there's only one per Pi, wired to one SPI bus and one set of
    RST/CS/DRDY pins (see `ads1263.py`'s own docstring: "Owns the whole
    chip ... only one instance should exist per physical HAT"). Before
    this function existed, `create()` built a fresh `ADS1263` (its own
    SPI handle + GPIO pin objects) on every call, so a second `etape`
    entry always failed with `gpiozero.exc.GPIOPinInUse` fighting the
    first over the same pins. This lazily builds ONE shared instance the
    first time any real `etape` entry needs it, and hands that same
    object to every entry after -- module-level state deliberately,
    since it mirrors the actual physical hardware: there is exactly one
    ADC chip, at most once per process.

    `build_adc` is injectable (defaults to `_build_real_adc`, which
    touches real SPI/GPIO) purely so this caching/validation logic can
    be unit-tested without real hardware -- same spirit as `ads1263.py`
    taking `spi`/pin objects as constructor args instead of opening them
    itself. Every `etape` entry sharing this HAT must agree on
    `reference_voltage` (it's a property of the physical board, not of
    one sensor's install) -- a conflicting value raises rather than
    silently using whichever entry happened to load first.

    Simulated sensors (`create(..., simulate=True)`) never call this --
    each gets its own independent `SimulatedADS1263()`, since there's no
    real GPIO to contend over and existing tests rely on that
    independence."""
    global _shared_adc, _shared_adc_reference_voltage
    with _shared_adc_lock:
        if _shared_adc is None:
            _shared_adc = build_adc(reference_voltage)
            _shared_adc_reference_voltage = reference_voltage
        elif reference_voltage != _shared_adc_reference_voltage:
            raise ValueError(
                f"etape: reference_voltage {reference_voltage!r} conflicts with "
                f"{_shared_adc_reference_voltage!r} already in use by another etape "
                "sensor on this ADC HAT -- every etape entry shares one physical "
                "chip, so they must all agree on reference_voltage (or leave it "
                "unset on every entry to use the default)"
            )
        return _shared_adc


def create(params, simulate):
    """Builds an EtapeSensor from a sensor config entry's `params` dict.

    Recognized params:
      adc_channel (required, integer 0-9) -- which single-ended channel
      of the Waveshare ADS1263 ADC HAT this eTape's Resistance-to-Voltage
      module's Vout is wired to. Multiple `etape` entries (different
      physical eTapes wired to different channels of the same HAT) are
      supported -- see `_get_or_build_shared_adc()` for how they share
      one underlying ADC handle.
      calibration_slope_cm_per_v, calibration_intercept_cm (both
      required) -- this specific physical eTape unit's own linear
      voltage-to-depth fit (`depth_cm = slope * voltage_v + intercept`),
      bench-calibrated per-unit; see README's "Sensor notes" section.
      reference_voltage (default ads1263.DEFAULT_REFERENCE_VOLTAGE) --
      the ADC's own AVDD reference rail voltage (see ads1263.py); only
      needs overriding if a board is remeasured differently than the
      one the default was measured against -- and must be the same
      across every entry sharing one HAT.
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
        adc = _get_or_build_shared_adc(reference_voltage)

    return EtapeSensor(adc, adc_channel, slope, intercept, poll_interval_s=poll_interval_s)
