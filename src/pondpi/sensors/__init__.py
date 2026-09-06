import importlib
import inspect
import pkgutil
import sys

from pondpi.sensors.base import LevelSensor

__all__ = ["LevelSensor", "discover_sensor_types"]

_TYPE_SUFFIX = "_sensor"


def discover_sensor_types(package=None):
    """Returns {type_name: create_function} for every module in this
    package whose filename ends in "_sensor" (mirrors
    signals.discover_signal_types() -- see that docstring for the
    general approach).

    type_name is the module's filename with that suffix stripped (e.g.
    sensors/a02yyuw_sensor.py -> type "a02yyuw"). Each such module must
    define exactly one LevelSensor subclass, and a module-level
    `create(params, simulate)` function that builds and returns an
    instance of it. Unlike signals (whose constructors take simple
    scalar params directly), most sensor drivers need real hardware
    objects -- a serial connection, GPIO controllers -- assembled
    around those params, and build entirely different (simulated)
    objects under `--simulate`; `create()` is where a driver does that
    assembly, so callers never need to know a given type's own
    construction details.
    """
    if package is None:
        package = sys.modules[__name__]

    registry = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        name = module_info.name
        if not name.endswith(_TYPE_SUFFIX):
            continue

        module = importlib.import_module(f"{package.__name__}.{name}")
        found = [
            obj
            for _, obj in inspect.getmembers(module, inspect.isclass)
            if issubclass(obj, LevelSensor) and obj is not LevelSensor and obj.__module__ == module.__name__
        ]

        if len(found) != 1:
            raise ValueError(f"{module.__name__}: expected exactly one LevelSensor subclass, found {len(found)}")

        create = getattr(module, "create", None)
        if create is None or not callable(create):
            raise ValueError(f"{module.__name__}: must define a module-level create(params, simulate) function")

        type_name = name.removesuffix(_TYPE_SUFFIX)
        registry[type_name] = create

    return registry
