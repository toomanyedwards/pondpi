import threading
import time
from datetime import datetime, timezone

from pondpi.signals.base import LevelSignal
from pondpi.signals.utils.rolling_average import RollingAverage


class PollingRollingAverageSignal(LevelSignal):
    """Averages its `input:` signal's output over a rolling window, like
    RollingAverageSignal -- but instead of being pushed a new value on
    every poll_sensor() tick, this signal owns its own background
    thread that pulls its input's current cached value on its own pace.

    `poll_interval_s` paces the loop itself: each iteration sleeps this
    long between samples, so `window_size * poll_interval_s` is a
    genuine real-world time span, decoupled from the sensor's own much
    faster poll rate (`--polling-interval-ms`) -- no need to retain a
    sample for every single one of those ticks to cover a real minute,
    the way a plain input:-fed RollingAverageSignal would.

    Call `run_loop()` in a dedicated thread (see server.py's `main()`);
    `current()` is the thread-safe read side, polled by HTTP handlers.
    """

    owns_read_loop = True

    def __init__(self, window_size, poll_interval_s):
        self._poll_interval_s = poll_interval_s
        self._rolling_avg = RollingAverage(window_size)
        self._lock = threading.Lock()
        self._value = None
        self._at = None
        self._last_reading_monotonic = None

    def run_loop(self, stop_event, get_raw_value):
        """`get_raw_value` is a zero-arg callable returning this
        signal's `input:` signal's current cached value (in mm), or
        None if it isn't ready yet -- supplied by server.py, which owns
        the shared per-sensor state this reads from."""
        while not stop_event.is_set():
            value = get_raw_value()

            if value is not None:
                with self._lock:
                    self._value = self._rolling_avg.add(value)
                    self._at = datetime.now(timezone.utc).isoformat()
                    self._last_reading_monotonic = time.monotonic()

            time.sleep(self._poll_interval_s)

    def current(self):
        """Thread-safe snapshot of this signal's current output, or
        None before its first successful reading."""
        with self._lock:
            if self._value is None:
                return None
            return {
                "value": self._value,
                "at": self._at,
                "window_size": self._rolling_avg.window_size,
                "samples_in_window": self._rolling_avg.count,
                "poll_interval_s": self._poll_interval_s,
            }

    def last_reading_monotonic(self):
        """time.monotonic() timestamp of this signal's last successful
        reading, or None before its first -- kept separate from
        `current()` since it's an internal timing detail, not part of
        the public signal output."""
        with self._lock:
            return self._last_reading_monotonic
