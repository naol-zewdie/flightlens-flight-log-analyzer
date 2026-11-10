"""The check plugin interface and registry.

A check is one Python file in ``flightlens/checks/`` that defines a class
following :class:`Check` and decorates it with :func:`register`. Files are
discovered automatically; nothing else needs to change to add a check.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from flightlens.model import CheckConfig, Finding, Flight


@runtime_checkable
class Check(Protocol):
    id: str
    """Stable identifier, e.g. ``vibration.level``."""
    title: str
    version: str
    """Bump when the method or defaults change, so reports stay traceable."""
    required_signals: Sequence[str]
    defaults: Mapping[str, float]

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]: ...


def alternative_signals(check: Check) -> Sequence[Sequence[str]]:
    """Optional other signal sets a check can run on instead of ``required_signals``.

    For example, vibration runs on per-axis ArduPilot ``vibe.x/y/z`` or on
    PX4's single ``vibe.metric``. Declare it as a class attribute.
    """
    alts: Sequence[Sequence[str]] = getattr(check, "alternative_signals", ())
    return alts


_REGISTRY: dict[str, Check] = {}
_discovered = False


def register[T](cls: type[T]) -> type[T]:
    """Class decorator that adds a check to the registry."""
    instance = cls()
    if not isinstance(instance, Check):
        raise TypeError(f"{cls.__name__} does not implement the Check interface")
    if instance.id in _REGISTRY:
        raise ValueError(f"duplicate check id {instance.id!r}")
    _REGISTRY[instance.id] = instance
    return cls


def _discover() -> None:
    global _discovered
    if _discovered:
        return
    import flightlens.checks as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("_") or mod.name == "base":
            continue
        importlib.import_module(f"{pkg.__name__}.{mod.name}")
    _discovered = True


def all_checks() -> list[Check]:
    """Every registered check, ordered by id."""
    _discover()
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]
