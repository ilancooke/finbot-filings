"""Explicit earnings-calendar synchronization; no background runtime on import."""

from .config import CalendarConfig
from .contracts import CalendarHealth, CalendarSnapshot, CalendarSyncRun, CalendarSyncState, CalendarSyncSuccess
from .providers import PlaceholderCalendarProvider

__all__ = ["CalendarConfig", "CalendarHealth", "CalendarSnapshot", "CalendarSyncRun",
           "CalendarSyncState", "CalendarSyncSuccess", "PlaceholderCalendarProvider"]
