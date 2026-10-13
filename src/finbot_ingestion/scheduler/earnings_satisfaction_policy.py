"""Versioned scheduling heuristic; never evidence of extracted/validated earnings."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .window_policy import EarningsWindow

POLICY_VERSION = "earnings-satisfaction-v2"
SUPPORTED_POLICY_VERSIONS = {"earnings-satisfaction-v1", POLICY_VERSION}
MATCH_REASONS = {"10-Q": "original_10_q", "10-K": "original_10_k", "8-K": "original_8_k_item_2_02"}


def matching_window(expected_date):
    """One New York calendar day, independent of polling/session configuration."""
    zone = ZoneInfo("America/New_York")
    start = datetime.combine(expected_date, time.min, zone)
    end = datetime.combine(expected_date + timedelta(days=1), time.min, zone)
    return EarningsWindow(start, end, end)


@dataclass(frozen=True, slots=True)
class EarningsSatisfactionDecision:
    eligible: bool
    reason: str
    policy_version: str = POLICY_VERSION


class EarningsSatisfactionPolicy:
    version = POLICY_VERSION

    def window(self, event):
        return matching_window(event.expected_date)

    def evaluate(self, event, filing, evidence):
        def no(reason):
            return EarningsSatisfactionDecision(False, reason)
        if filing.company_cik != event.company_cik:
            return no("cik_mismatch")
        if not self.window(event).contains(filing.filed_at):
            return no("outside_window")
        if filing.form_type.endswith("/A"):
            return no("amendment")
        if filing.form_type not in MATCH_REASONS:
            return no("unsupported_form")
        if filing.form_type == "8-K":
            if evidence is None or evidence.status == "absent":
                return no("missing_sec_items")
            if ((evidence.accession_number, evidence.company_cik) != (filing.accession_number, filing.company_cik)
                    or evidence.status != "known"):
                return no("ambiguous_sec_items")
            if "2.02" not in evidence.items:
                return no("missing_item_2_02")
        return EarningsSatisfactionDecision(True, MATCH_REASONS[filing.form_type])

    def select(self, event, observations):
        for observation in sorted(observations, key=lambda o: (o.filing.filed_at, o.filing.accession_number)):
            decision = self.evaluate(event, observation.filing, observation.sec_items)
            if decision.eligible:
                return observation, decision
        return None
