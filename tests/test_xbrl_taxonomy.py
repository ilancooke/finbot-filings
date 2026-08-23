from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pyarrow.parquet as pq

from finbot_filings.xbrl.source import inventory_xbrl_source
from finbot_filings.xbrl.taxonomy.inventory import inventory_taxonomy_filings
from finbot_filings.xbrl.taxonomy.materialize import (
    CONCEPT_LABEL_SCHEMA,
    PRESENTATION_RELATIONSHIP_SCHEMA,
    PRESENTATION_ROLE_SCHEMA,
    build_taxonomy_tables,
    extract_taxonomy_filings,
)
from finbot_filings.xbrl.taxonomy.package import (
    TaxonomyInventoryError,
    inventory_taxonomy_package,
)

ACCESSION = "0000320193-25-000079"


def _schema(*, embedded_linkbase: str = "") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xsd:schema
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:link="http://www.xbrl.org/2003/linkbase"
  xmlns:xlink="http://www.w3.org/1999/xlink"
  xmlns:xbrli="http://www.xbrl.org/2003/instance"
  xmlns:co="http://example.com/company/2025"
  targetNamespace="http://example.com/company/2025">
  <xsd:import namespace="http://fasb.org/us-gaap/2025"
    schemaLocation="https://xbrl.fasb.org/us-gaap/2025/elts/us-gaap-2025.xsd"/>
  <xsd:include schemaLocation="support.xsd"/>
  <xsd:annotation><xsd:appinfo>
    <link:linkbaseRef xlink:type="simple" xlink:href="filing_pre.xml"/>
    <link:roleType id="role_statement" roleURI="http://example.com/role/statement">
      <link:definition>Statement - Example Balance Sheets</link:definition>
      <link:usedOn>link:presentationLink</link:usedOn>
    </link:roleType>
    {embedded_linkbase}
  </xsd:appinfo></xsd:annotation>
  <xsd:element id="co_CustomAsset" name="CustomAsset"
    type="xsd:decimal" substitutionGroup="xbrli:item"
    xbrli:periodType="instant"/>
</xsd:schema>
""".encode()


def _support_schema() -> bytes:
    return b"""<?xml version="1.0"?>
<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema"
 targetNamespace="http://example.com/company/2025"/>
"""


def _presentation_linkbase() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<link:linkbase
 xmlns:link="http://www.xbrl.org/2003/linkbase"
 xmlns:xlink="http://www.w3.org/1999/xlink"
 xml:base="sub/">
 <link:presentationLink xlink:type="extended"
   xlink:role="http://example.com/role/statement">
  <link:loc xlink:type="locator" xlink:label="assets"
    xlink:href="https://xbrl.fasb.org/us-gaap/2025/elts/us-gaap-2025.xsd#us-gaap_Assets"/>
  <link:loc xlink:type="locator" xlink:label="custom"
    xlink:href="../filing.xsd#co_CustomAsset"/>
  <link:presentationArc xlink:type="arc"
    xlink:arcrole="http://www.xbrl.org/2003/arcrole/parent-child"
    xlink:from="assets" xlink:to="custom" order="2"
    preferredLabel="http://www.xbrl.org/2003/role/terseLabel"/>
 </link:presentationLink>
</link:linkbase>
"""


def _label_linkbase() -> bytes:
    return b"""<?xml version="1.0"?>
<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <link:labelLink xlink:type="extended" xlink:role="http://example.com/role/labels">
  <link:loc xlink:type="locator" xlink:label="concept"
    xlink:href="filing.xsd#co_CustomAsset"/>
  <link:label xlink:type="resource" xlink:label="label" xml:lang="en-US"
    xlink:role="http://www.xbrl.org/2003/role/label">Custom asset</link:label>
  <link:labelArc xlink:type="arc"
    xlink:arcrole="http://www.xbrl.org/2003/arcrole/concept-label"
    xlink:from="concept" xlink:to="label"/>
 </link:labelLink>
</link:linkbase>
"""


def _simple_relationship_linkbase(kind: str) -> bytes:
    link = f"{kind}Link"
    arc = f"{kind}Arc"
    weight = ' weight="1"' if kind == "calculation" else ""
    return f"""<?xml version="1.0"?>
<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase"
 xmlns:xlink="http://www.w3.org/1999/xlink">
 <link:{link} xlink:type="extended" xlink:role="http://example.com/role/{kind}">
  <link:loc xlink:type="locator" xlink:label="a" xlink:href="filing.xsd#co_CustomAsset"/>
  <link:loc xlink:type="locator" xlink:label="b" xlink:href="filing.xsd#co_CustomAsset"/>
  <link:{arc} xlink:type="arc" xlink:arcrole="http://example.com/arcrole/{kind}"
    xlink:from="a" xlink:to="b" order="1"{weight}/>
 </link:{link}>
</link:linkbase>
""".encode()


def _embedded_label_linkbase() -> str:
    return """
<link:linkbase>
 <link:labelLink xlink:type="extended" xlink:role="http://example.com/role/labels">
  <link:loc xlink:type="locator" xlink:label="concept"
    xlink:href="#co_CustomAsset"/>
  <link:label xlink:type="resource" xlink:label="label" xml:lang="en-US"
    xlink:role="http://www.xbrl.org/2003/role/label">Custom asset</link:label>
  <link:labelArc xlink:type="arc"
    xlink:arcrole="http://www.xbrl.org/2003/arcrole/concept-label"
    xlink:from="concept" xlink:to="label"/>
 </link:labelLink>
</link:linkbase>
"""


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    value = io.BytesIO()
    with zipfile.ZipFile(value, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return value.getvalue()


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    path.write_bytes(_zip_bytes(members))


def _standalone_members() -> dict[str, bytes]:
    return {
        "filing.xsd": _schema(),
        "support.xsd": _support_schema(),
        "filing_pre.xml": _presentation_linkbase(),
        "filing_lab.xml": _label_linkbase(),
        "filing_def.xml": _simple_relationship_linkbase("definition"),
        "filing_cal.xml": _simple_relationship_linkbase("calculation"),
        "filing.htm": b"<html><body>not parsed as taxonomy XML</body></html>",
    }


def _write_raw_source(root: Path, members: dict[str, bytes]) -> Path:
    directory = root / "AAPL" / "10-K" / ACCESSION
    xbrl = directory / "xbrl"
    xbrl.mkdir(parents=True)
    package = _zip_bytes(members)
    instance = b"<xbrl/>"
    metadata = {
        "ticker": "AAPL",
        "cik": 320193,
        "form": "10-K",
        "accession_number": ACCESSION,
        "filing_date": "2025-10-31",
        "report_date": "2025-09-27",
    }
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (xbrl / "package.zip").write_bytes(package)
    (xbrl / "instance.xml").write_bytes(instance)
    (xbrl / "metadata.json").write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(package).hexdigest(),
                "instance_sha256": hashlib.sha256(instance).hexdigest(),
                "instance_source_filename": "filing_htm.xml",
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_inventory_discovers_standalone_resources_and_resolves_local_qname(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package.zip"
    _write_zip(package, _standalone_members())

    inventory = inventory_taxonomy_package(package)

    assert len(inventory.resources) == 6
    assert inventory.link_counts == {
        "label": 1,
        "presentation": 1,
        "definition": 1,
        "calculation": 1,
        "reference": 0,
    }
    assert {relationship.linkbase_type for relationship in inventory.relationships} == {
        "label",
        "presentation",
        "definition",
        "calculation",
    }
    external = next(
        reference
        for reference in inventory.references
        if reference.reference_type == "schema_import"
    )
    assert external.resolution_status == "external_dependency"
    external_locator = next(
        locator.reference
        for locator in inventory.locators
        if locator.label == "assets"
    )
    assert external_locator.resolution_status == "external_dependency"
    assert external_locator.concept_qname is None
    local = next(
        locator.reference
        for locator in inventory.locators
        if locator.label == "custom" and locator.linkbase_type == "presentation"
    )
    assert local.resolution_status == "embedded_package_member"
    assert local.target_member == "filing.xsd"
    assert local.concept_qname == "{http://example.com/company/2025}CustomAsset"
    include = next(
        reference
        for reference in inventory.references
        if reference.reference_type == "schema_include"
    )
    assert include.resolution_status == "embedded_package_member"
    calculation = next(
        relationship
        for relationship in inventory.relationships
        if relationship.linkbase_type == "calculation"
    )
    assert calculation.weight == "1"
    assert ("weight", "1") in calculation.attributes
    assert inventory.unresolved_local_references == []


def test_inventory_discovers_schema_embedded_linkbase(tmp_path: Path) -> None:
    package = tmp_path / "package.zip"
    _write_zip(
        package,
        {
            "filing.xsd": _schema(embedded_linkbase=_embedded_label_linkbase()),
            "support.xsd": _support_schema(),
            "filing_pre.xml": _presentation_linkbase(),
        },
    )

    inventory = inventory_taxonomy_package(package)

    schema = next(resource for resource in inventory.resources if resource.member_name == "filing.xsd")
    assert schema.embedded_linkbase_types == ("label",)
    assert inventory.link_counts["label"] == 1
    locator = next(
        locator for locator in inventory.locators if locator.linkbase_type == "label"
    )
    assert locator.reference.resolution_status == "same_document"
    assert locator.reference.concept_qname.endswith("}CustomAsset")


def test_inventory_records_missing_local_member_and_fragment(tmp_path: Path) -> None:
    broken = _presentation_linkbase().replace(
        b"../filing.xsd#co_CustomAsset", b"../missing.xsd#missing"
    )
    package = tmp_path / "package.zip"
    _write_zip(
        package,
        {
            "filing.xsd": _schema(),
            "support.xsd": _support_schema(),
            "filing_pre.xml": broken,
        },
    )

    inventory = inventory_taxonomy_package(package)

    assert any(
        reference.resolution_status == "missing_local_member"
        for reference in inventory.unresolved_local_references
    )

    fragment_package = tmp_path / "fragment.zip"
    _write_zip(
        fragment_package,
        {
            "filing.xsd": _schema(),
            "support.xsd": _support_schema(),
            "filing_pre.xml": _presentation_linkbase().replace(
                b"co_CustomAsset", b"co_Missing"
            ),
        },
    )
    fragment_inventory = inventory_taxonomy_package(fragment_package)
    assert any(
        reference.resolution_status == "missing_fragment"
        for reference in fragment_inventory.unresolved_local_references
    )


@pytest.mark.parametrize(
    "content, reason",
    [
        (b"<xsd:schema", "invalid_taxonomy_xml"),
        (
            b'<!DOCTYPE x [<!ENTITY e "value">]><x xmlns="http://www.w3.org/2001/XMLSchema"/>',
            "doctype_not_allowed",
        ),
    ],
)
def test_inventory_rejects_invalid_or_doctype_xml(
    tmp_path: Path, content: bytes, reason: str
) -> None:
    package = tmp_path / "package.zip"
    _write_zip(package, {"filing.xsd": content})

    with pytest.raises(TaxonomyInventoryError) as exc:
        inventory_taxonomy_package(package)

    assert exc.value.reason == reason


def test_inventory_rejects_duplicate_members(tmp_path: Path) -> None:
    package = tmp_path / "package.zip"
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("filing.xsd", _schema())
            archive.writestr("filing.xsd", _schema())

    with pytest.raises(TaxonomyInventoryError) as exc:
        inventory_taxonomy_package(package)

    assert exc.value.reason == "duplicate_package_member"


def test_inventory_rejects_unsafe_member_and_invalid_zip(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.zip"
    _write_zip(unsafe, {"../filing.xsd": _schema()})

    with pytest.raises(TaxonomyInventoryError) as unsafe_error:
        inventory_taxonomy_package(unsafe)
    assert unsafe_error.value.reason == "unsafe_package_member"

    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not a zip")
    with pytest.raises(TaxonomyInventoryError) as invalid_error:
        inventory_taxonomy_package(invalid)
    assert invalid_error.value.reason == "invalid_xbrl_package"


def test_inventory_enforces_calibrated_xml_member_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import finbot_filings.xbrl.taxonomy.package as taxonomy_package

    monkeypatch.setattr(taxonomy_package, "MAX_XML_MEMBER_BYTES", 10)
    package = tmp_path / "package.zip"
    _write_zip(package, {"filing.xsd": _schema()})

    with pytest.raises(TaxonomyInventoryError) as exc:
        inventory_taxonomy_package(package)

    assert exc.value.reason == "resource_limit_exceeded"


def test_batch_writes_compact_source_aware_inventory_and_skips_current(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw"
    derived = tmp_path / "derived"
    _write_raw_source(raw, _standalone_members())
    output: list[str] = []
    now = lambda: datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

    first = inventory_taxonomy_filings(
        download_root=raw,
        output_root=derived,
        ticker="aapl",
        form_type="10-k",
        accession_number=ACCESSION,
        printer=output.append,
        now=now,
    )
    second = inventory_taxonomy_filings(
        download_root=raw,
        output_root=derived,
        printer=output.append,
        now=now,
    )

    path = derived / "AAPL" / "10-K" / ACCESSION / "taxonomy_inventory.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert first.inventoried == 1
    assert first.failed == 0
    assert second.skipped == 1
    assert manifest["status"] == "success"
    assert manifest["parser_version"] == "xbrl-taxonomy-inventory-v2"
    assert manifest["counts"]["relationship_counts"] == {
        "calculation": 1,
        "definition": 1,
        "label": 1,
        "presentation": 1,
    }
    assert len(manifest["external_dependencies"]) == 1
    assert "locators" not in manifest
    assert "relationships" not in manifest
    assert manifest["inventoried_at"] == "2026-08-22T12:00:00+00:00"

    manifest["source_package_sha256"] = "stale"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    refreshed = inventory_taxonomy_filings(
        download_root=raw,
        output_root=derived,
        printer=lambda _: None,
        now=now,
    )
    assert refreshed.inventoried == 1
    assert refreshed.skipped == 0


def test_batch_writes_failure_manifest(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    derived = tmp_path / "derived"
    _write_raw_source(raw, {"broken.xsd": b"<xsd:schema"})

    summary = inventory_taxonomy_filings(
        download_root=raw,
        output_root=derived,
        printer=lambda _: None,
    )

    path = derived / "AAPL" / "10-K" / ACCESSION / "taxonomy_inventory.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert summary.failed == 1
    assert summary.failure_reasons == {"invalid_taxonomy_xml": 1}
    assert manifest["status"] == "failure"
    assert manifest["failure_reason"] == "invalid_taxonomy_xml"
    assert manifest["source_package_sha256"]


def test_build_taxonomy_tables_preserves_labels_roles_and_endpoints(
    tmp_path: Path,
) -> None:
    filing = _write_raw_source(tmp_path / "raw", _standalone_members())
    source = inventory_xbrl_source(filing)
    inventory = inventory_taxonomy_package(source.package_path)

    tables = build_taxonomy_tables(source=source, inventory=inventory)

    assert len(tables.concept_labels) == 1
    label = tables.concept_labels[0]
    assert label["record_kind"] == "relationship"
    assert label["relationship_status"] == "resolved"
    assert label["concept_qname"] == "{http://example.com/company/2025}CustomAsset"
    assert label["label_text"] == "Custom asset"
    assert label["language"] == "en-US"
    assert any(
        name.endswith("}label") and value == "label"
        for name, value in json.loads(label["label_attributes_json"])
    )

    assert len(tables.presentation_roles) == 1
    role = tables.presentation_roles[0]
    assert role["role_definition"] == "Statement - Example Balance Sheets"
    assert json.loads(role["used_on_json"]) == ["link:presentationLink"]
    assert role["definition_status"] == "resolved"
    assert role["role_type_source_ordinal"] == 0
    assert any(
        name == "roleURI" and value == "http://example.com/role/statement"
        for name, value in json.loads(role["role_type_attributes_json"])
    )

    assert len(tables.presentation_relationships) == 1
    relationship = tables.presentation_relationships[0]
    assert relationship["parent_qname"] is None
    assert relationship["parent_resolution_status"] == "external_dependency"
    assert relationship["parent_reference_uri"].endswith(
        "us-gaap-2025.xsd#us-gaap_Assets"
    )
    assert relationship["child_qname"].endswith("}CustomAsset")
    assert relationship["arc_order"] == "2"
    assert relationship["preferred_label_role_uri"].endswith("/terseLabel")
    assert tables.diagnostics["source_counts"]["label_arcs"] == 1
    assert tables.diagnostics["source_counts"]["presentation_arcs"] == 1
    assert tables.warnings == ()


def test_build_taxonomy_tables_accounts_for_orphans_and_missing_endpoints(
    tmp_path: Path,
) -> None:
    labels = _label_linkbase().replace(
        b"<link:labelArc",
        b'<link:label xlink:type="resource" xlink:label="orphan" '
        b'xml:lang="fr" xlink:role="http://www.xbrl.org/2003/role/label">'
        b"Actif personnalis\xc3\xa9</link:label>\n  <link:labelArc",
    ).replace(b'xlink:from="concept"', b'xlink:from="missing"')
    filing = _write_raw_source(
        tmp_path / "raw",
        {
            "filing.xsd": _schema(),
            "support.xsd": _support_schema(),
            "filing_pre.xml": _presentation_linkbase(),
            "filing_lab.xml": labels,
        },
    )
    source = inventory_xbrl_source(filing)
    tables = build_taxonomy_tables(
        source=source, inventory=inventory_taxonomy_package(source.package_path)
    )

    relationship = next(
        row for row in tables.concept_labels if row["record_kind"] == "relationship"
    )
    orphan_locator = next(
        row for row in tables.concept_labels if row["record_kind"] == "orphan_locator"
    )
    orphan_resources = [
        row for row in tables.concept_labels if row["record_kind"] == "orphan_resource"
    ]
    assert relationship["relationship_status"] == "missing_from_endpoint"
    assert relationship["arc_from_label"] == "missing"
    assert orphan_locator["concept_qname"].endswith("}CustomAsset")
    assert {row["label_text"] for row in orphan_resources} == {
        "Actif personnalis\u00e9"
    }
    assert tables.diagnostics["orphan_label_locator_count"] == 1
    assert tables.diagnostics["orphan_label_resource_count"] == 1
    assert tables.diagnostics["unresolved_label_arc_row_count"] == 1
    assert tables.warnings


def test_extract_taxonomy_writes_typed_bundle_and_skips_current(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw"
    derived = tmp_path / "derived"
    _write_raw_source(raw, _standalone_members())
    now = lambda: datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    output: list[str] = []

    first = extract_taxonomy_filings(
        download_root=raw,
        output_root=derived,
        ticker="aapl",
        form_type="10-k",
        accession_number=ACCESSION,
        printer=output.append,
        now=now,
    )
    second = extract_taxonomy_filings(
        download_root=raw,
        output_root=derived,
        printer=output.append,
        now=now,
    )

    directory = derived / "AAPL" / "10-K" / ACCESSION
    labels = pq.read_table(directory / "concept_labels.parquet")
    roles = pq.read_table(directory / "presentation_roles.parquet")
    relationships = pq.read_table(directory / "presentation_relationships.parquet")
    metadata = json.loads(
        (directory / "taxonomy_metadata.json").read_text(encoding="utf-8")
    )
    assert first.extracted == 1
    assert first.label_rows == 1
    assert first.role_rows == 1
    assert first.presentation_relationship_rows == 1
    assert second.skipped == 1
    assert labels.schema == CONCEPT_LABEL_SCHEMA
    assert roles.schema == PRESENTATION_ROLE_SCHEMA
    assert relationships.schema == PRESENTATION_RELATIONSHIP_SCHEMA
    assert metadata["status"] == "success"
    assert metadata["source_package_sha256"]
    assert metadata["generated_at"] == "2026-08-23T12:00:00+00:00"
    assert metadata["diagnostics"]["source_counts"]["label_resources"] == 1
    assert metadata["diagnostics"]["output_counts"] == {
        "concept_label_rows": 1,
        "presentation_relationship_rows": 1,
        "presentation_role_rows": 1,
    }

    (directory / "presentation_roles.parquet").unlink()
    repaired = extract_taxonomy_filings(
        download_root=raw,
        output_root=derived,
        printer=lambda _: None,
        now=now,
    )
    assert repaired.extracted == 1
    assert (directory / "presentation_roles.parquet").is_file()


def test_extract_taxonomy_rebuilds_stale_bundle_and_writes_failure_metadata(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw"
    derived = tmp_path / "derived"
    _write_raw_source(raw, _standalone_members())
    extract_taxonomy_filings(
        download_root=raw, output_root=derived, printer=lambda _: None
    )
    metadata_path = derived / "AAPL" / "10-K" / ACCESSION / "taxonomy_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["source_package_sha256"] = "stale"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    rebuilt = extract_taxonomy_filings(
        download_root=raw, output_root=derived, printer=lambda _: None
    )
    assert rebuilt.extracted == 1
    assert rebuilt.skipped == 0

    broken_raw = tmp_path / "broken"
    broken_derived = tmp_path / "broken-derived"
    _write_raw_source(broken_raw, {"broken.xsd": b"<xsd:schema"})
    failed = extract_taxonomy_filings(
        download_root=broken_raw,
        output_root=broken_derived,
        printer=lambda _: None,
    )
    failure_path = (
        broken_derived / "AAPL" / "10-K" / ACCESSION / "taxonomy_metadata.json"
    )
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    assert failed.failed == 1
    assert failure["status"] == "failure"
    assert failure["failure_reason"] == "invalid_taxonomy_xml"


def test_taxonomy_table_rows_are_deterministic(tmp_path: Path) -> None:
    filing = _write_raw_source(tmp_path / "raw", _standalone_members())
    source = inventory_xbrl_source(filing)
    inventory = inventory_taxonomy_package(source.package_path)

    first = build_taxonomy_tables(source=source, inventory=inventory)
    second = build_taxonomy_tables(source=source, inventory=inventory)

    assert first == second


def test_taxonomy_tables_scope_repeated_labels_across_embedded_links(
    tmp_path: Path,
) -> None:
    filing = _write_raw_source(
        tmp_path / "raw",
        {
            "filing.xsd": _schema(
                embedded_linkbase=_embedded_label_linkbase()
            ),
            "support.xsd": _support_schema(),
            "filing_pre.xml": _presentation_linkbase(),
            "filing_lab.xml": _label_linkbase(),
        },
    )
    source = inventory_xbrl_source(filing)
    tables = build_taxonomy_tables(
        source=source, inventory=inventory_taxonomy_package(source.package_path)
    )

    assert len(tables.concept_labels) == 2
    assert {row["source_member"] for row in tables.concept_labels} == {
        "filing.xsd",
        "filing_lab.xml",
    }
    assert {row["concept_qname"] for row in tables.concept_labels} == {
        "{http://example.com/company/2025}CustomAsset"
    }
    assert {row["label_text"] for row in tables.concept_labels} == {"Custom asset"}


def test_taxonomy_tables_preserve_duplicate_arcs_and_missing_locator_href(
    tmp_path: Path,
) -> None:
    presentation = _presentation_linkbase().replace(
        b" </link:presentationLink>",
        b""" <link:presentationArc xlink:type="arc"
    xlink:arcrole="http://www.xbrl.org/2003/arcrole/parent-child"
    xlink:from="assets" xlink:to="custom" order="2"
    preferredLabel="http://www.xbrl.org/2003/role/terseLabel"/>
 </link:presentationLink>""",
    )
    labels = _label_linkbase().replace(
        b'xlink:href="filing.xsd#co_CustomAsset"', b'xlink:href=""'
    )
    filing = _write_raw_source(
        tmp_path / "raw",
        {
            "filing.xsd": _schema(),
            "support.xsd": _support_schema(),
            "filing_pre.xml": presentation,
            "filing_lab.xml": labels,
        },
    )
    source = inventory_xbrl_source(filing)
    tables = build_taxonomy_tables(
        source=source, inventory=inventory_taxonomy_package(source.package_path)
    )

    assert len(tables.presentation_relationships) == 2
    assert tables.diagnostics["duplicate_presentation_arc_count"] == 1
    assert tables.concept_labels[0]["concept_resolution_status"] == "missing_href"
    assert tables.concept_labels[0]["concept_reference_uri"] == ""
    assert any("locator without href" in warning for warning in tables.warnings)


def test_taxonomy_bundle_does_not_publish_success_after_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import finbot_filings.xbrl.taxonomy.materialize as materialize

    raw = tmp_path / "raw"
    derived = tmp_path / "derived"
    _write_raw_source(raw, _standalone_members())

    def fail_write(*args, **kwargs):
        raise OSError("simulated parquet failure")

    monkeypatch.setattr(materialize.pq, "write_table", fail_write)
    summary = extract_taxonomy_filings(
        download_root=raw,
        output_root=derived,
        printer=lambda _: None,
    )

    directory = derived / "AAPL" / "10-K" / ACCESSION
    metadata = json.loads(
        (directory / "taxonomy_metadata.json").read_text(encoding="utf-8")
    )
    assert summary.failed == 1
    assert metadata["status"] == "failure"
    assert metadata["failure_reason"] == "taxonomy_extraction_error"
    assert not list(directory.glob(".*.tmp"))
    assert not (directory / "concept_labels.parquet").exists()
