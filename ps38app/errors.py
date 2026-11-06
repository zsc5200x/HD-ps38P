"""Shared exceptions that must cross the collector/provider boundary."""

from __future__ import annotations


class LoginRejected(RuntimeError):
    """Credentials/captcha/second factor were rejected; do not auto-retry blindly."""
