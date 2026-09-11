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

    `_poll_loop()` (below) samples on this fixed timer regardless of
    whether the source has anything new -- deliberately, unlike every
    other signal type's dirty-checked `read()`, since a genuine
    real-world time average needs one sample per tick, not one per
    upstream change. But it still only feeds a sample into the window
    (`add()`) when the source's own `at` has actually advanced since
    last tick (tracked via the base class's `_last_source_at` -- unused
    by this type until now, since it never went through `Signal.read()`'s
    own dirty-check machinery) -- a source that's gone briefly stale
    (e.g. `A02YYUWSensor`'s "raw" reading freezing for ~1.5s during each
    mode-cycle's dip into "processed", see [Sensor
    notes](#sensor-notes)/README) just gets its last value re-written
    with a fresh timestamp instead of being counted twice. Skipping that
    check entirely used to mean a handful of duplicate-weighted samples
    slipped into the window every cycle, producing a small but real
    periodic sawtooth in the average -- the exact mechanism the
    README's "Response time sets a polling floor" note already
    describes, just triggered by mode-cycling instead of by polling
    faster than the sensor's own response time.

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

    def _poll_loop(self, stop_event):
        """Loops until `stop_event` -- specifically the one this thread
        was started with, passed in as an argument rather than read off
        `self._stop_event` -- is set. Same reasoning as
        `A02YYUWSensor._poll_loop()`'s identical pattern: `reset()`'s
        `self._thread.join(timeout=...)` is a bound, not a guarantee, so
        `_begin_polling()` can end up replacing `self._stop_event` with
        a fresh `Event` while the old thread is still alive; a loop that
        read `self._stop_event` dynamically would then never see the
        stop signal it was actually given, leaking that thread forever.
        Capturing the exact `Event` this thread was handed avoids that.

        Samples `self._source_signal` once per tick regardless of
        whether it's changed (see the class docstring for why), but only
        feeds a genuinely new reading into the window -- a stale repeat
        (same `at` as last tick) just gets its already-computed value
        re-written with a fresh timestamp, not re-added."""
        while not stop_event.is_set():
            source = self._source_signal.read(self._source_options)
            if source is not None:
                if source["at"] != self._last_source_at:
                    self._last_source_at = source["at"]
                    value = self.add(source["value"])
                else:
                    value = self._value
                self._write(value, datetime.now(timezone.utc).isoformat())
            time.sleep(self._poll_interval_ms / 1000)

    def _begin_polling(self):
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._poll_loop, args=(self._stop_event,), daemon=True)
        self._thread.start()

    def reset(self):
        """Signals the current thread to stop and waits (up to
        `poll_interval_ms + 1s`) for it to actually exit *before*
        clearing the accumulated window (via the base class's `reset()`
        -> `_reset_state()`, which also clears `_last_source_at`) and
        starting a fresh one. The join timeout is a bound, not a
        guarantee -- see `_poll_loop()`'s own docstring for why a timed-
        out old thread still can't leak: it's guaranteed to notice its
        own captured `stop_event` and exit shortly after, even past the
        timeout, so any overlap with the new thread is brief and
        self-resolving."""
        self._stop_event.set()
        self._thread.join(timeout=self._poll_interval_ms / 1000 + 1)
        super().reset()
        self._begin_polling()
