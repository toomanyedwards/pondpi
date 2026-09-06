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
