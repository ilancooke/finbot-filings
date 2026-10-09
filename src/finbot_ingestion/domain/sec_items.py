"""Accession-bound scheduling evidence, independent of document interpretation."""

from dataclasses import dataclass
import re

from .identity import normalize_accession_number, normalize_cik
from .filing import Filing


@dataclass(frozen=True, slots=True)
class SECItemMetadata:
    accession_number: str
    company_cik: str
    status: str = "absent"
    items: tuple[str, ...] = ()
    source: str = "submissions.recent.items"

    def __post_init__(self):
        object.__setattr__(self, "accession_number", normalize_accession_number(self.accession_number))
        object.__setattr__(self, "company_cik", normalize_cik(self.company_cik))
        if self.status not in ("known", "absent", "ambiguous"):
            raise ValueError("invalid SEC item evidence status")
        if (not isinstance(self.items, tuple) or len(self.items) > 100 or
                any(not isinstance(item, str) or not re.fullmatch(r"[1-9]\.[0-9]{2}", item) for item in self.items)
                or tuple(sorted(set(self.items))) != self.items):
            raise ValueError("SEC items must be sorted unique exact codes")
        if self.status != "known" and self.items:
            raise ValueError("unknown evidence cannot carry affirmative items")
        if self.source != "submissions.recent.items":
            raise ValueError("unsupported SEC evidence source")


@dataclass(frozen=True, slots=True)
class FilingObservation:
    filing: Filing
    sec_items: SECItemMetadata

    def __post_init__(self):
        if not isinstance(self.filing, Filing) or not isinstance(self.sec_items, SECItemMetadata):
            raise ValueError("typed filing and item evidence required")
        if (self.filing.accession_number, self.filing.company_cik) != (self.sec_items.accession_number, self.sec_items.company_cik):
            raise ValueError("item evidence belongs to another filing")
