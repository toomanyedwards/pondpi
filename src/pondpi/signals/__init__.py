import importlib
import inspect
import pkgutil
import sys

from pondpi.signals.base import LevelSignal

__all__ = ["LevelSignal", "discover_signal_types"]

_TYPE_SUFFIX = "_signal"


def discover_signal_types(package=None):
    """Returns {type_name: SignalClass} for every module in this package
    whose filename ends in "_signal".

    type_name is the module's filename with that suffix stripped (e.g.
    signals/rolling_median_signal.py -> type "rolling_median"). Each such
    module must define exactly one LevelSignal subclass -- zero or
    multiple is an error, not silently ignored. Files that don't end in
    "_signal" (base.py, the utils/ subpackage, or any future non-signal
    helper module) are ignored automatically, with no hardcoded
    skip-list to maintain.
    This is what lets a new signal be added by just dropping a new
    <name>_signal.py file in this folder, with nothing else to register.
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
            if issubclass(obj, LevelSignal) and obj is not LevelSignal and obj.__module__ == module.__name__
        ]

        if len(found) != 1:
            raise ValueError(f"{module.__name__}: expected exactly one LevelSignal subclass, found {len(found)}")

        type_name = name.removesuffix(_TYPE_SUFFIX)
        registry[type_name] = found[0]

    return registry
