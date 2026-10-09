"""Unconfigured provider: never pretend an unavailable calendar is empty."""

from ..provider import CalendarProviderNotConfigured


class PlaceholderCalendarProvider:
    name = "placeholder"

    async def fetch_events(self, start_date, end_date, companies):
        raise CalendarProviderNotConfigured("Select and configure an earnings-calendar provider")
