from pondpi.signals.base import LevelSignal


class ChainSignal(LevelSignal):
    """Runs a value through a sequence of other signals in order,
    feeding each stage's output as the next stage's input.

    `steps` is a list of (label, LevelSignal instance) pairs, resolved
    by signal_config.py from the YAML config. Each step always gets its
    own independent instance -- referencing another signal by name
    (`ref:`) reuses its type/params to build a fresh instance, never the
    literal same object, so state is never shared across signals. See
    config/sensors.yaml for examples (each sensor's own `signals:` list
    uses this same schema).
    """

    def __init__(self, steps):
        self._steps = steps

    def add(self, raw_value):
        value = raw_value
        for _, signal in self._steps:
            value = signal.add(value)
        return value

    def extra_state(self):
        return {"steps": [{"signal": label, **signal.extra_state()} for label, signal in self._steps]}
