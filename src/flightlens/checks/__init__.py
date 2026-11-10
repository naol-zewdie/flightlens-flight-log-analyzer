"""Built-in checks. Each module registers itself with :func:`register`."""

from flightlens.checks.base import Check, all_checks, register

__all__ = ["Check", "all_checks", "register"]
