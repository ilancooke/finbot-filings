"""No durable reads per tick; completion-based schedules and no catch-up bursts."""

from dataclasses import dataclass

from finbot_ingestion.domain.satisfaction import EventIdentity


@dataclass(slots=True)
class PollState:
    company: object
    due: float
    active: bool = False
    failures: int = 0


class PollingScheduler:
    def __init__(self, config, clock, windows, queue):
        self.config, self.clock, self.windows, self.queue = config, clock, windows, queue
        self.states, self.events, self.satisfied = {}, (), set()
        self.events_by_cik = {}

    def reload(self, companies, events, satisfied):
        now = self.clock.monotonic()
        self.states = {c.cik: PollState(c, self.states[c.cik].due, self.states[c.cik].active,
            self.states[c.cik].failures) if c.cik in self.states else PollState(c,
            now + (int(c.cik) % 1000) / 1000 * self.config.safety_poll_seconds)
            for c in companies if c.enabled}
        self.events, self.satisfied = tuple(events), set(satisfied)
        self.events_by_cik = {}
        for event in self.events:
            self.events_by_cik.setdefault(event.company_cik, []).append(event)

    def active(self, cik):
        now = self.clock.now()
        return any(e.company_cik == cik and EventIdentity.from_event(e) not in self.satisfied
                   and self.windows.window(e).contains(now) for e in self.events_by_cik.get(cik, ()))

    def tick(self):
        now = self.clock.monotonic()
        for cik, state in sorted(self.states.items(), key=lambda pair: (pair[1].due, pair[0])):
            active = self.active(cik)
            if active and not state.active:
                state.due = min(state.due, now)
            state.active = active
            if state.due <= now:
                self.queue.offer(cik, state.company)

    def completed(self, cik, *, retry_delay=None):
        state = self.states.get(cik)
        if state is None:
            return
        state.active = self.active(cik)
        interval = self.config.active_poll_seconds if state.active else self.config.safety_poll_seconds
        state.due = self.clock.monotonic() + (max(interval, retry_delay) if retry_delay is not None else interval)
        state.failures = state.failures + 1 if retry_delay is not None else 0

    @property
    def active_count(self):
        return sum(self.active(cik) for cik in self.states)
