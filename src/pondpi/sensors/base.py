import threading
import time


class LevelSensor:
    """Base class every sensor driver implements, one file per sensor
    type (e.g. `a02yyuw_sensor.py`), mirroring how `signals/` is
    structured.

    `read()` returns canonical readings -- distance from the sensor's
    mount point down to the water surface, in millimeters -- regardless
    of the underlying sensing technology. Different sensor hardware
    measures fundamentally different physical quantities (an ultrasonic
    sensor's raw distance vs. a resistive sensor's submerged length,
    with opposite sign conventions), so each driver is responsible for
    converting its own native reading into this shared unit before
    returning it; nothing downstream (signals, the HTTP API) needs to
    know which sensing technology produced a given value.

    A driver reports one or more named signals -- e.g. `A02YYUWSensor`
    reports "raw" and "processed", corresponding to the sensor's two
    hardware modes, while a simpler sensor might only ever report one.

    `supports_reset` is a capability flag: override it to True (and
    implement `reset()`) only if the underlying hardware can actually be
    power-cycled or otherwise reset in software. Callers must check it
    before calling `reset()`.

    `read()` and `reset()` must be safe to call concurrently from
    different threads -- this driver's own `poll_loop()` calls `read()`
    continuously from its background polling thread while `POST /reset`
    calls `reset()` from a request-handling thread, with no
    synchronization at that layer. It's each driver's own responsibility
    to serialize its hardware access internally (e.g. a lock around
    whatever touches the physical connection) if a concurrent reset
    could otherwise corrupt or wedge an in-flight read.

    `start()`/`poll_loop()` are concrete, not abstract: `read()`'s
    "non-blocking, call repeatedly" contract is identical for every
    driver type, so the polling loop itself has nothing driver-specific
    in it and every subclass gets it for free. Override only if some
    future driver type genuinely needs something other than a plain
    polling thread.
    """

    supports_reset = False

    def read(self):
        """Returns a dict of {signal_name: distance_mm} for whichever
        signals produced a fresh valid reading since the last call, or
        an empty dict if nothing new is available this call. Must not
        block waiting for a frame -- callers are expected to call this
        repeatedly from their own polling loop."""
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError

    def close(self):
        pass

    def start(self, stop_event, poll_interval_s, on_reading):
        """Spawns this driver's own background thread running
        `poll_loop()` (see server.py's `main()`, which calls this once
        per configured sensor and otherwise leaves that thread's
        lifecycle to the driver itself, same pattern as
        `LevelSignal.start()`)."""
        threading.Thread(target=self.poll_loop, args=(stop_event, poll_interval_s, on_reading), daemon=True).start()

    def poll_loop(self, stop_event, poll_interval_s, on_reading):
        """Repeatedly calls `read()` and passes each (reading_key,
        distance_mm) pair it returns to `on_reading(reading_key,
        distance_mm)` -- e.g. server.py's `_route_reading()`, which owns
        deciding what a reading actually feeds, this loop just supplies
        it with each one as it arrives."""
        while not stop_event.is_set():
            for reading_key, distance_mm in self.read().items():
                on_reading(reading_key, distance_mm)
            time.sleep(poll_interval_s)
