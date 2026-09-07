import threading
import time

from pondpi.signals.base import LevelSignal
from pondpi.signals.utils.rolling_average import RollingAverage


class RollingAverageSignal(LevelSignal):
    """Averages its `input:` signal's output over a rolling window --
    but instead of being pushed a new value on every one of the
    sensor's own reads, this signal owns its own background thread
    that pulls its input's current cached value (via that signal's own
    `current()`) on its own pace.

    `poll_interval_ms` paces the loop itself: each iteration sleeps
    this long between samples, so `window_size * poll_interval_ms` is a
    genuine real-world time span, decoupled from the sensor's own much
    faster poll rate -- no need to retain a sample for every single one
    of those ticks to cover a real minute.

    Its background thread starts the moment it's constructed -- there's
    no public `start()`/loop-control method; `reset()` is the only way
    to make it stop and start a fresh one (see `reset()` below).
    `current()` (inherited from `LevelSignal`) is the thread-safe read
    side, polled by HTTP handlers.
    """

    owns_read_loop = True

    def __init__(self, window_size, poll_interval_ms, get_raw_value):
        super().__init__()
        self._poll_interval_ms = poll_interval_ms
        self._get_raw_value = get_raw_value
        self._rolling_avg = RollingAverage(window_size)
        self._begin_polling()

    def add(self, raw_value):
        return self._rolling_avg.add(raw_value)

    def extra_state(self):
        return {
            "window_size": self._rolling_avg.window_size,
            "samples_in_window": self._rolling_avg.count,
            "poll_interval_ms": self._poll_interval_ms,
        }

    def _reset_state(self):
        self._rolling_avg = RollingAverage(self._rolling_avg.window_size)

    def _poll_loop(self):
        while not self._stop_event.is_set():
            value = self._get_raw_value()
            if value is not None:
                self.feed(value)
            time.sleep(self._poll_interval_ms / 1000)

    def _begin_polling(self):
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def reset(self):
        """Stops the current thread and waits for it to actually exit
        *before* clearing the accumulated window (via the base class's
        `reset()` -> `_reset_state()`) and starting a fresh one -- so
        there's never a moment where the old thread could still call
        `_get_raw_value()`/`feed()` against state we're in the middle
        of resetting."""
        self._stop_event.set()
        self._thread.join(timeout=self._poll_interval_ms / 1000 + 1)
        super().reset()
        self._begin_polling()
