"""Submissions item normalization and conflicting filing evidence."""

from pathlib import Path
import json

import pytest

from finbot_ingestion.sec.submissions import parse_company_submissions_with_evidence

from tests.support.scheduling import NOW, COMPANY, ACCESSION


@pytest.mark.parametrize("raw,status,items", [("2.02", "known", ("2.02",)),
    ("9.01, 2.02,2.02", "known", ("2.02", "9.01")), (None, "absent", ()), ("", "absent", ()),
    ("2.020", "ambiguous", ()), ("Item 2.02", "ambiguous", ()), (["2.02"], "ambiguous", ()),
    ("2.02;9.01", "ambiguous", ()), ("2.02,", "ambiguous", ()), ("2.02,9.٠١", "ambiguous", ())])
def test_item_source_normalization_and_duplicates(raw, status, items):
    payload = json.loads((Path(__file__).parents[1] / "fixtures/submissions_mixed.json").read_text())
    payload["filings"]["recent"]["items"] = [raw] * 8
    result = parse_company_submissions_with_evidence(COMPANY, payload, discovered_at=NOW)
    assert len(result) == 6
    assert all(o.sec_items.status == status and o.sec_items.items == items for o in result)
    payload["filings"]["recent"]["items"] = ["2.02"]
    assert all(o.sec_items.status == "ambiguous" for o in parse_company_submissions_with_evidence(
        COMPANY, payload, discovered_at=NOW))


def test_conflicting_duplicate_item_metadata_is_not_a_match():
    payload = json.loads((Path(__file__).parents[1] / "fixtures/submissions_mixed.json").read_text())
    payload["filings"]["recent"]["items"] = ["2.02"] * 8
    payload["filings"]["recent"]["items"][6] = "9.01"
    result = parse_company_submissions_with_evidence(COMPANY, payload, discovered_at=NOW)
    assert next(o for o in result if o.filing.accession_number == ACCESSION).sec_items.status == "ambiguous"
