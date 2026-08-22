from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from finbot_filings.xbrl.extract import (
    XBRLExtractionError,
    extract_xbrl_filings,
    inspect_xbrl_filings,
    inventory_xbrl_source,
    write_extracted_facts,
)
from finbot_filings.xbrl.instance import XBRLParseError, parse_instance_document
from finbot_filings.xbrl.query import show_xbrl_facts

ACCESSION = "0000320193-25-000079"


def _metadata() -> dict[str, object]:
    return {
        "ticker": "AAPL",
        "cik": 320193,
        "form": "10-K",
        "accession_number": ACCESSION,
        "filing_date": "2025-10-31",
        "report_date": "2025-09-27",
    }


def _instance() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl
  xmlns:xbrli="http://www.xbrl.org/2003/instance"
  xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
  xmlns:us-gaap="http://fasb.org/us-gaap/2025"
  xmlns:dei="http://xbrl.sec.gov/dei/2025"
  xmlns:aapl="http://www.apple.com/20250927">
  <xbrli:context id="instant">
    <xbrli:entity>
      <xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier>
    </xbrli:entity>
    <xbrli:period><xbrli:instant>2025-09-27</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="duration-a">
    <xbrli:entity>
      <xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier>
      <xbrli:segment>
        <xbrldi:explicitMember dimension="us-gaap:ProductOrServiceAxis">aapl:IPhoneMember</xbrldi:explicitMember>
      </xbrli:segment>
    </xbrli:entity>
    <xbrli:period>
      <xbrli:startDate>2024-09-29</xbrli:startDate>
      <xbrli:endDate>2025-09-27</xbrli:endDate>
    </xbrli:period>
  </xbrli:context>
  <xbrli:context id="duration-b">
    <xbrli:entity>
      <xbrli:identifier scheme="http://www.sec.gov/CIK">0000320193</xbrli:identifier>
      <xbrli:segment>
        <xbrldi:explicitMember dimension="us-gaap:ProductOrServiceAxis">aapl:IPhoneMember</xbrldi:explicitMember>
      </xbrli:segment>
    </xbrli:entity>
    <xbrli:period>
      <xbrli:startDate>2024-09-29</xbrli:startDate>
      <xbrli:endDate>2025-09-27</xbrli:endDate>
    </xbrli:period>
  </xbrli:context>
  <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
  <us-gaap:Assets contextRef="instant" unitRef="USD" decimals="-6">364980000000</us-gaap:Assets>
  <aapl:VendorNonTradeReceivables contextRef="instant" unitRef="USD" decimals="-6">20000000000</aapl:VendorNonTradeReceivables>
  <us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax contextRef="duration-a" unitRef="USD" decimals="-6">416161000000</us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax>
  <us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax contextRef="duration-b" unitRef="USD" decimals="-6">416161000000</us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax>
  <dei:EntityRegistrantName contextRef="instant" xml:lang="en-US">Apple Inc.</dei:EntityRegistrantName>
  <us-gaap:Goodwill contextRef="instant" unitRef="USD" xsi:nil="true"/>
</xbrli:xbrl>
"""


def _zip_bytes() -> bytes:
    value = io.BytesIO()
    with zipfile.ZipFile(value, "w") as archive:
        archive.writestr("aapl-20250927.xsd", b"<schema/>")
    return value.getvalue()


def _write_raw_source(root: Path) -> Path:
    directory = root / "AAPL" / "10-K" / ACCESSION
    xbrl_directory = directory / "xbrl"
    xbrl_directory.mkdir(parents=True)
    package = _zip_bytes()
    instance = _instance()
    (directory / "metadata.json").write_text(
        json.dumps(_metadata()), encoding="utf-8"
    )
    (xbrl_directory / "package.zip").write_bytes(package)
    (xbrl_directory / "instance.xml").write_bytes(instance)
    (xbrl_directory / "metadata.json").write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(package).hexdigest(),
                "instance_sha256": hashlib.sha256(instance).hexdigest(),
                "instance_source_filename": "aapl-20250927_htm.xml",
                "instance_source_url": "https://www.sec.gov/example_htm.xml",
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_parse_instance_discovers_standard_and_extension_concepts() -> None:
    result = parse_instance_document(
        _instance(),
        filing_metadata=_metadata(),
        instance_filename="aapl-20250927_htm.xml",
    )

    assert len(result.facts) == 6
    assert result.context_count == 3
    assert result.unit_count == 1
    assets = next(fact for fact in result.facts if fact["concept_name"] == "Assets")
    assert assets["value_text"] == "364980000000"
    assert assets["value_kind"] == "numeric"
    assert assets["period_instant"] == date(2025, 9, 27)
    assert assets["unit_display"] == "USD"
    extension = next(
        fact
        for fact in result.facts
        if fact["concept_name"] == "VendorNonTradeReceivables"
    )
    assert extension["concept_namespace"] == "http://www.apple.com/20250927"


def test_parse_instance_resolves_dimensions_and_equivalent_duplicates() -> None:
    result = parse_instance_document(
        _instance(), filing_metadata=_metadata(), instance_filename="instance.xml"
    )
    revenue = [
        fact
        for fact in result.facts
        if fact["concept_name"]
        == "RevenueFromContractWithCustomerExcludingAssessedTax"
    ]

    assert result.duplicate_group_count == 1
    assert {fact["duplicate_group_size"] for fact in revenue} == {2}
    assert revenue[0]["duplicate_group_id"] == revenue[1]["duplicate_group_id"]
    assert revenue[0]["period_start"] == date(2024, 9, 29)
    dimensions = json.loads(revenue[0]["dimensions_json"])
    assert dimensions[0]["kind"] == "explicit"
    assert dimensions[0]["member_qname"].endswith("}IPhoneMember")


def test_parse_instance_preserves_nil_and_language() -> None:
    result = parse_instance_document(
        _instance(), filing_metadata=_metadata(), instance_filename="instance.xml"
    )
    goodwill = next(
        fact for fact in result.facts if fact["concept_name"] == "Goodwill"
    )
    name = next(
        fact for fact in result.facts if fact["concept_name"] == "EntityRegistrantName"
    )
    assert goodwill["is_nil"] is True
    assert goodwill["value_text"] == ""
    assert name["value_kind"] == "non_numeric"
    assert name["xml_language"] == "en-US"


def test_parse_instance_rejects_missing_context() -> None:
    broken = _instance().replace(b'contextRef="instant"', b'contextRef="missing"', 1)
    with pytest.raises(XBRLParseError, match="missing context"):
        parse_instance_document(
            broken, filing_metadata=_metadata(), instance_filename="instance.xml"
        )


def test_inventory_validates_checksums(tmp_path: Path) -> None:
    directory = _write_raw_source(tmp_path)
    source = inventory_xbrl_source(directory)
    assert len(source.package_members) == 1

    source.instance_path.write_bytes(b"changed")
    with pytest.raises(XBRLExtractionError, match="instance.xml checksum"):
        inventory_xbrl_source(directory)


def test_write_extracted_facts_preserves_types_and_metadata(tmp_path: Path) -> None:
    source = inventory_xbrl_source(_write_raw_source(tmp_path / "raw"))
    result = parse_instance_document(
        source.instance_bytes,
        filing_metadata=source.filing_metadata,
        instance_filename="aapl-20250927_htm.xml",
    )
    paths = write_extracted_facts(
        source=source,
        result=result,
        output_root=tmp_path / "derived",
        now=lambda: datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
    )

    table = pq.read_table(paths.facts)
    assert table.num_rows == 6
    assert str(table.schema.field("period_instant").type) == "date32[day]"
    assert str(table.schema.field("value_text").type) == "large_string"
    metadata = json.loads(paths.metadata.read_text(encoding="utf-8"))
    assert metadata["fact_count"] == 6
    assert metadata["duplicate_group_count"] == 1
    assert metadata["source_instance_sha256"] == source.instance_sha256


def test_batch_extract_inspect_and_show_exports(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    derived_root = tmp_path / "derived"
    _write_raw_source(raw_root)
    output: list[str] = []

    assert inspect_xbrl_filings(
        download_root=raw_root,
        ticker="aapl",
        form_type="10-k",
        printer=output.append,
    ) == 1
    first = extract_xbrl_filings(
        download_root=raw_root,
        output_root=derived_root,
        ticker="AAPL",
        form_type="10-K",
        printer=output.append,
    )
    second = extract_xbrl_filings(
        download_root=raw_root,
        output_root=derived_root,
        ticker="AAPL",
        printer=output.append,
    )

    assert first.extracted == 1
    assert first.facts_written == 6
    assert second.skipped == 1
    json_path = tmp_path / "assets.json"
    count = show_xbrl_facts(
        xbrl_root=derived_root,
        ticker="AAPL",
        form_type="10-K",
        concept="us-gaap:Assets",
        output_format="json",
        output_path=json_path,
    )
    assert count == 1
    exported = json.loads(json_path.read_text(encoding="utf-8"))
    assert exported[0]["value_text"] == "364980000000"

    csv_path = tmp_path / "facts.csv"
    assert show_xbrl_facts(
        xbrl_root=derived_root,
        ticker="AAPL",
        concept="Assets",
        output_format="csv",
        output_path=csv_path,
    ) == 1
    assert "concept_name" in csv_path.read_text(encoding="utf-8")
