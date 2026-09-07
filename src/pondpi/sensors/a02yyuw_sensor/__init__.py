import threading
import time

import serial

from pondpi.sensors.base import LevelSensor

from . import read_sensor, sensor_mode, sensor_power

# Comfortably above the sensor's ~100ms response time -- if this long
# passes with no valid frame, read_frame() has likely lost byte
# alignment with the sensor's stream and isn't going to resync on its
# own. A plain input-buffer flush is enough to force a fresh resync.
STALE_READING_THRESHOLD_S = 3.0

# This driver spends most of its time with the sensor in "raw" mode (so
# downstream signals -- tuned assuming a near-continuous feed -- keep
# behaving as they always have) and only briefly dips into
# "processed" mode once per cycle to keep that reading fresh too. E.g.
# with the defaults below: 1s of "processed" out of every 10s, so "raw"
# still sees samples ~90% of the time.
MODE_CYCLE_INTERVAL_S = 10.0
PROCESSED_MODE_DURATION_S = 1.0

# After switching the mode-select pin, frames read within this window
# are discarded rather than reported -- the sensor's documented response
# time is 100-300ms, and this gives comfortable margin above that so a
# stale reading from the previous mode is never mistaken for the new
# one.
MODE_SETTLE_S = 0.4

# How often this driver's own background thread calls read() -- see
# LevelSensor.__init__(). Comfortably above the sensor's ~100ms response
# time, so every poll is an independent look at the water surface rather
# than re-reading the same frame multiple times in a row.
DEFAULT_POLL_INTERVAL_S = 0.15


class A02YYUWSensor(LevelSensor):
    """Driver for the A02YYUW waterproof ultrasonic sensor (UART, 9600
    baud). See README's "Sensor notes" section for why the sensor has
    two hardware output modes and why this driver alternates between
    them on a single physical unit rather than reading both at once.

    Reports named signals from `read()`:
    - "raw": the sensor's real-time hardware mode.
    - "processed": the sensor's own internally-smoothed hardware mode.

    By default alternates between both (mostly raw, briefly dipping
    into processed once per `mode_cycle_interval_s`) so both stay
    fresh. Pass `read_mode=sensor_mode.RAW` or `sensor_mode.PROCESSED`
    to pin it permanently in one mode instead -- no cycling, and
    `read()` then only ever reports that one key.

    `read()` does at most one serial read per call and never blocks
    waiting for a frame -- called repeatedly from this driver's own
    background thread (started automatically at construction; see
    `LevelSensor.__init__()`).

    `read()` and `reset_hardware()` are safe to call concurrently from
    different threads (this driver's own background thread calls
    `read()` continuously while `POST /reset` calls `reset()`, which
    calls `reset_hardware()`, from a request-handling thread) --
    internally serialized via `_hardware_lock`, since power-cycling
    mid-read could otherwise wedge the UART. Callers never need to know
    about this; it's this driver's own responsibility to be safe under
    that usage pattern.
    """

    supports_reset = True

    def __init__(
        self,
        ser,
        mode_controller,
        power_controller,
        on_reading,
        poll_interval_s=DEFAULT_POLL_INTERVAL_S,
        stale_threshold_s=STALE_READING_THRESHOLD_S,
        mode_cycle_interval_s=MODE_CYCLE_INTERVAL_S,
        processed_mode_duration_s=PROCESSED_MODE_DURATION_S,
        mode_settle_s=MODE_SETTLE_S,
        read_mode=None,
    ):
        self._ser = ser
        self._mode_controller = mode_controller
        self._power_controller = power_controller
        self._stale_threshold_s = stale_threshold_s
        self._mode_cycle_interval_s = mode_cycle_interval_s
        self._processed_mode_duration_s = processed_mode_duration_s
        self._mode_settle_s = mode_settle_s
        self._read_mode = read_mode
        self._hardware_lock = threading.Lock()

        self._last_valid_monotonic = time.monotonic()
        self._current_mode = self._read_mode if self._read_mode is not None else sensor_mode.RAW
        self._mode_controller.set_mode(self._current_mode)
        # Backdated, not just time.monotonic(): there's no prior mode's
        # stale readings to guard against on a fresh start, so the very
        # first frames shouldn't be discarded as "settling" the way
        # frames right after a real mid-run mode switch are.
        self._last_mode_switch_monotonic = time.monotonic() - mode_settle_s
        self._cycle_start_monotonic = time.monotonic()

        # Must be last: this starts a background thread that immediately
        # begins calling self.read(), so every attribute above must
        # already be set.
        super().__init__(on_reading, poll_interval_s)

    def read(self):
        with self._hardware_lock:
            now = time.monotonic()

            if self._read_mode is not None:
                desired_mode = self._read_mode
            else:
                # Which mode we *should* be in right now, as a function of
                # time elapsed since this driver was constructed (not
                # wall-clock time -- that would make a freshly-started
                # driver's initial mode depend on what moment it happened to
                # start at). Always begins in "raw".
                phase = (now - self._cycle_start_monotonic) % self._mode_cycle_interval_s
                desired_mode = (
                    sensor_mode.PROCESSED
                    if phase >= (self._mode_cycle_interval_s - self._processed_mode_duration_s)
                    else sensor_mode.RAW
                )
            if desired_mode != self._current_mode:
                self._current_mode = desired_mode
                self._mode_controller.set_mode(self._current_mode)
                self._last_mode_switch_monotonic = now

            distance_mm = read_sensor.read_frame(self._ser)
            settling = (now - self._last_mode_switch_monotonic) < self._mode_settle_s

            if distance_mm is not None and read_sensor.is_valid_reading(distance_mm):
                self._last_valid_monotonic = time.monotonic()

                if settling:
                    return {}

                key = "raw" if self._current_mode == sensor_mode.RAW else "processed"
                return {key: distance_mm}

            if time.monotonic() - self._last_valid_monotonic > self._stale_threshold_s:
                # No valid frame in a while -- read_frame()'s incremental
                # header-hunting resync can get permanently wedged if the
                # byte stream is knocked out of alignment (e.g. by a wiring
                # disturbance) in just the wrong way. A plain buffer flush
                # is enough to force a fresh resync, so do that rather than
                # wait forever. Reset the timer so we don't flush every loop
                # iteration while genuinely disconnected.
                self._ser.reset_input_buffer()
                self._last_valid_monotonic = time.monotonic()

            return {}

    def reset_hardware(self):
        with self._hardware_lock:
            self._power_controller.reset()

    def close(self):
        self._ser.close()
        self._mode_controller.close()
        self._power_controller.close()


def create(params, simulate, on_reading):
    """Builds an A02YYUWSensor from a sensor config entry's `params`
    dict -- see discover_sensor_types() for why driver types need a
    factory function rather than being constructed directly.

    Recognized params (all optional):
      serial_port (default "/dev/serial0"), mode_select_pin (default 25),
      power_pin (default 24) -- hardware wiring, ignored entirely under
      `simulate`.
      read_mode ("raw" or "processed"; omitted alternates between both,
      see A02YYUWSensor) -- governs this driver's own mode-cycling
      logic rather than hardware wiring, so it still applies under
      `simulate` too.
      poll_interval_ms (default 150) -- how often this driver's own
      background thread calls read().
    """
    read_mode = params.get("read_mode")
    if read_mode is not None and read_mode not in (sensor_mode.RAW, sensor_mode.PROCESSED):
        raise ValueError(
            f"a02yyuw: invalid read_mode '{read_mode}' "
            f"(expected '{sensor_mode.RAW}', '{sensor_mode.PROCESSED}', or omitted for the default alternating cycle)"
        )

    if simulate:
        ser = read_sensor.SimulatedSerial()
        mode_controller = sensor_mode.NullModeController()
        power_controller = sensor_power.NullPowerController()
    else:
        ser = serial.Serial(params.get("serial_port", "/dev/serial0"), baudrate=9600, timeout=1)
        mode_controller = sensor_mode.GpioModeController(params.get("mode_select_pin", 25))
        power_controller = sensor_power.GpioPowerController(params.get("power_pin", 24))

    poll_interval_s = params.get("poll_interval_ms", DEFAULT_POLL_INTERVAL_S * 1000) / 1000

    return A02YYUWSensor(
        ser, mode_controller, power_controller, on_reading, poll_interval_s=poll_interval_s, read_mode=read_mode
    )
