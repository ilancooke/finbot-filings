"""Versioned scheduling heuristic; never evidence of extracted/validated earnings."""

from dataclasses import dataclass

POLICY_VERSION = "earnings-satisfaction-v1"
MATCH_REASONS = {"10-Q": "original_10_q", "10-K": "original_10_k", "8-K": "original_8_k_item_2_02"}


@dataclass(frozen=True, slots=True)
class EarningsSatisfactionDecision:
    eligible: bool
    reason: str
    policy_version: str = POLICY_VERSION


class EarningsSatisfactionPolicy:
    version = POLICY_VERSION

    def evaluate(self, event, window, filing, evidence):
        def no(reason):
            return EarningsSatisfactionDecision(False, reason)
        if filing.company_cik != event.company_cik:
            return no("cik_mismatch")
        if not window.contains(filing.filed_at):
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

    def select(self, event, window, observations):
        for observation in sorted(observations, key=lambda o: (o.filing.filed_at, o.filing.accession_number)):
            decision = self.evaluate(event, window, observation.filing, observation.sec_items)
            if decision.eligible:
                return observation, decision
        return None
