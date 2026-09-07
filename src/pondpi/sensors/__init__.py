import importlib
import inspect
import pkgutil
import sys

from pondpi.sensors.base import Sensor

__all__ = ["Sensor", "discover_sensor_types"]

_TYPE_SUFFIX = "_sensor"


def discover_sensor_types(package=None):
    """Returns {type_name: create_function} for every module or
    subpackage in this package whose name ends in "_sensor" (mirrors
    signals.discover_signal_types() -- see that docstring for the
    general approach).

    A driver is usually a single <type>_sensor.py file, but can
    instead be a <type>_sensor/ directory package for a sensor whose
    driver is naturally split across several source files (protocol
    parsing, GPIO helpers, ...) that belong grouped together rather
    than scattered as top-level pondpi modules -- see
    sensors/a02yyuw_sensor/ for an example. pkgutil.iter_modules()
    already enumerates packages alongside plain modules, so both forms
    are found the same way; type_name is the module/package's name
    with that suffix stripped (e.g. sensors/a02yyuw_sensor/ -> type
    "a02yyuw").

    Each such module must define exactly one Sensor subclass --
    directly, for a flat module, or in one of its own submodules and
    re-exported from its __init__.py, for a directory package -- and a
    module-level `create(params, simulate)` function that builds and
    returns an instance of it. Unlike signals (whose constructors take
    simple scalar params directly), most sensor drivers need real
    hardware objects -- a serial connection, GPIO controllers --
    assembled around those params, and build entirely different
    (simulated) objects under `--simulate`; `create()` is where a driver
    does that assembly, so callers never need to know a given type's own
    construction details. Construction alone is enough to start the
    driver's own background read thread, if it has one -- see
    `Sensor.__init__()`.
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
            if issubclass(obj, Sensor)
            and obj is not Sensor
            and (obj.__module__ == module.__name__ or obj.__module__.startswith(f"{module.__name__}."))
        ]

        if len(found) != 1:
            raise ValueError(f"{module.__name__}: expected exactly one Sensor subclass, found {len(found)}")

        create = getattr(module, "create", None)
        if create is None or not callable(create):
            raise ValueError(f"{module.__name__}: must define a module-level create(params, simulate) function")

        type_name = name.removesuffix(_TYPE_SUFFIX)
        registry[type_name] = create

    return registry
