import importlib
import inspect
import pkgutil
import sys

from pondpi.signal_processors.base import LevelSignalProcessor

__all__ = ["LevelSignalProcessor", "discover_signal_processor_types"]


def discover_signal_processor_types(package=None):
    """Returns {type_name: SignalProcessorClass} for every module in this
    package except base.py.

    type_name is the module's filename (e.g. signal_processors/median.py
    -> type "median"). Each module must define exactly one
    LevelSignalProcessor subclass -- zero or multiple is an error, not
    silently ignored. This is what lets a new signal processor be added
    by just dropping a new file in this folder, with nothing else to
    register.
    """
    if package is None:
        package = sys.modules[__name__]

    registry = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        name = module_info.name
        if name == "base":
            continue

        module = importlib.import_module(f"{package.__name__}.{name}")
        found = [
            obj
            for _, obj in inspect.getmembers(module, inspect.isclass)
            if issubclass(obj, LevelSignalProcessor)
            and obj is not LevelSignalProcessor
            and obj.__module__ == module.__name__
        ]

        if len(found) != 1:
            raise ValueError(
                f"{module.__name__}: expected exactly one LevelSignalProcessor subclass, found {len(found)}"
            )

        registry[name] = found[0]

    return registry
