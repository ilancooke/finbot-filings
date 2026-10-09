"""Collection completeness does not grant authority to cancel omitted events."""

from dataclasses import dataclass

from finbot_ingestion.domain import ExpectedEarningsEvent


def key(event):
    return event.expected_date, event.company_cik


@dataclass(frozen=True, slots=True)
class CalendarReplacement:
    previous: ExpectedEarningsEvent
    incoming: ExpectedEarningsEvent


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    cancellations: tuple[ExpectedEarningsEvent, ...] = ()
    replacements: tuple[CalendarReplacement, ...] = ()
    preserved_count: int = 0
    ambiguous_count: int = 0


class AuthoritativeSnapshotPolicy:
    def plan(self, previous, incoming):
        keys = {key(event) for event in incoming}
        return ReconciliationPlan(cancellations=tuple(event for event in previous if key(event) not in keys))


class ReplacementOnlyPolicy:
    def plan(self, previous, incoming):
        old_groups, new_groups = {}, {}
        incoming_keys = {key(event) for event in incoming}
        for values, groups in ((previous, old_groups), (incoming, new_groups)):
            for event in values:
                if event.replacement_hint is not None:
                    identity = event.provider, event.company_cik, event.replacement_hint
                    groups.setdefault(identity, []).append(event)
        replacements, ambiguous = [], 0
        for identity, new in new_groups.items():
            if len(new) != 1:
                ambiguous += 1
                continue
            # A prior interrupted apply may already have persisted the target row.
            # Only missing source dates are candidates to supersede; a target row
            # already represented by this snapshot must not make repair ambiguous.
            old = [event for event in old_groups.get(identity, []) if key(event) not in incoming_keys]
            if not old:
                continue
            if len(old) != 1:
                ambiguous += 1
            elif old[0].expected_date != new[0].expected_date:
                if key(old[0]) in incoming_keys:
                    ambiguous += 1
                else:
                    replacements.append(CalendarReplacement(old[0], new[0]))
        replaced_keys = {key(pair.previous) for pair in replacements}
        preserved = sum(key(event) not in incoming_keys and key(event) not in replaced_keys for event in previous)
        return ReconciliationPlan(replacements=tuple(replacements), preserved_count=preserved,
                                  ambiguous_count=ambiguous)
