import threading
import time
from datetime import datetime, timezone

from pondpi.signals.base import Signal
from pondpi.signals.utils.rolling_average import RollingAverage


class RollingAverageSignal(Signal):
    """Averages its `source:` signal's output over a rolling window --
    but instead of recomputing on demand every time something calls
    `read()`, this signal owns its own background thread that pulls its
    source's current value (via that signal's own `read()`) on its own
    pace and writes the result directly. `read()` (overridden below) is
    a pure getter of whatever this thread last wrote -- there's no
    capability flag marking this type as special; it's simply the one
    type whose own `read()` skips the base class's usual pull-and-
    compute logic.

    `poll_interval_ms` paces the loop itself: each iteration sleeps
    this long between samples, so `window_size * poll_interval_ms` is a
    genuine real-world time span, decoupled from the sensor's own much
    faster poll rate -- no need to retain a sample for every single one
    of those ticks to cover a real minute.

    Its background thread starts the moment it's constructed -- there's
    no public `start()`/loop-control method; `reset()` is the only way
    to make it stop and start a fresh one (see `reset()` below).
    """

    def __init__(self, window_size, poll_interval_ms, source_signal):
        super().__init__(source_signal)
        self._poll_interval_ms = poll_interval_ms
        self._rolling_avg = RollingAverage(window_size)
        self._begin_polling()

    def read(self, options=None):
        """A pure getter -- this signal's own background thread
        (`_poll_loop()`, below) writes the cache directly on its own
        schedule, so `read()` here doesn't pull or compute anything
        itself (contrast `Signal.read()`, the default every other type
        uses). `options` is accepted only for interface compatibility
        with `Signal.read()` -- unused, since there's nothing to pull
        here."""
        return self._snapshot()

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
            source = self._source_signal.read(self._source_options)
            if source is not None:
                self._write(self.add(source["value"]), datetime.now(timezone.utc).isoformat())
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
        `source_signal.read()`/`_write()` against state we're in the
        middle of resetting."""
        self._stop_event.set()
        self._thread.join(timeout=self._poll_interval_ms / 1000 + 1)
        super().reset()
        self._begin_polling()
