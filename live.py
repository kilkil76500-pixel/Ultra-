"""
engine/live.py — DEPRECATED: live goal notification system removed.

The live monitor and goal alert system has been removed. The bot now focuses
exclusively on pre-match statistical prediction and analysis.

This stub module is kept so any external import does not crash at startup.
All public methods are no-ops.
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


class _NullMonitor:
    """No-op drop-in for the old LiveMonitor — does nothing."""

    def register_chat(self, chat_id: int) -> None:  # noqa: ARG002
        pass

    def unregister_chat(self, chat_id: int) -> None:  # noqa: ARG002
        pass

    def start(self, bot, loop) -> None:  # noqa: ARG002
        pass

    def stop(self) -> None:
        pass

    @property
    def subscriber_count(self) -> int:
        return 0

    def is_running(self) -> bool:
        return False


monitor = _NullMonitor()
