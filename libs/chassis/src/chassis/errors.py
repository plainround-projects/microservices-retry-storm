"""Errors raised by the chassis."""

from __future__ import annotations


class ConfigError(Exception):
    """The environment does not describe a valid configuration; the message lists every problem."""
