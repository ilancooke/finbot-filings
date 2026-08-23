"""Materialize source-shaped XBRL labels and presentation networks."""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from finbot_filings.layout import filing_directory, form_directory
from finbot_filings.xbrl.source import (
    XBRLSource,
    inventory_xbrl_source,
    read_json_object,
)
from finbot_filings.xbrl.taxonomy.models import (
    ExtendedLink,
    LinkbaseLocator,
    LinkbaseRelationship,
    LinkbaseResource,
    RoleType,
    TaxonomyPackageInventory,
)
from finbot_filings.xbrl.taxonomy.package import (
    TaxonomyInventoryError,
    inventory_taxonomy_package,
)

TABLE_SCHEMA_VERSION = 1
METADATA_SCHEMA_VERSION = 1
PARSER_VERSION = "xbrl-taxonomy-tables-v1"
METADATA_FILENAME = "taxonomy_metadata.json"
LABELS_FILENAME = "concept_labels.parquet"
ROLES_FILENAME = "presentation_roles.parquet"
RELATIONSHIPS_FILENAME = "presentation_relationships.parquet"


def _identity_fields() -> list[pa.Field]:
    return [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("ticker", pa.string(), nullable=False),
        pa.field("cik", pa.int64(), nullable=False),
        pa.field("form", pa.string(), nullable=False),
        pa.field("accession_number", pa.string(), nullable=False),
        pa.field("filing_date", pa.date32()),
        pa.field("report_date", pa.date32()),
        pa.field("source_package_sha256", pa.string(), nullable=False),
        pa.field("row_order", pa.int64(), nullable=False),
        pa.field("source_member", pa.string(), nullable=False),
        pa.field("extended_link_index", pa.int32(), nullable=False),
        pa.field("source_ordinal", pa.int32(), nullable=False),
    ]


CONCEPT_LABEL_SCHEMA = pa.schema(
    _identity_fields()
    + [
        pa.field("record_kind", pa.string(), nullable=False),
        pa.field("relationship_status", pa.string(), nullable=False),
        pa.field("extended_link_role_uri", pa.string()),
        pa.field("arcrole_uri", pa.string()),
        pa.field("arc_from_label", pa.string()),
        pa.field("arc_to_label", pa.string()),
        pa.field("arc_order", pa.string()),
        pa.field("arc_priority", pa.string()),
        pa.field("arc_use", pa.string()),
        pa.field("arc_attributes_json", pa.string()),
        pa.field("expansion_ordinal", pa.int32(), nullable=False),
        pa.field("concept_locator_ordinal", pa.int32()),
        pa.field("concept_xlink_label", pa.string()),
        pa.field("concept_reference_uri", pa.string()),
        pa.field("concept_raw_href", pa.string()),
        pa.field("concept_fragment", pa.string()),
        pa.field("concept_target_member", pa.string()),
        pa.field("concept_qname", pa.string()),
        pa.field("concept_resolution_status", pa.string()),
        pa.field("concept_locator_role_uri", pa.string()),
        pa.field("concept_locator_attributes_json", pa.string()),
        pa.field("label_resource_ordinal", pa.int32()),
        pa.field("label_xlink_label", pa.string()),
        pa.field("label_role_uri", pa.string()),
        pa.field("language", pa.string()),
        pa.field("label_text", pa.large_string()),
        pa.field("label_content_xml", pa.large_string()),
        pa.field("label_attributes_json", pa.string()),
    ]
)

PRESENTATION_ROLE_SCHEMA = pa.schema(
    _identity_fields()
    + [
        pa.field("role_uri", pa.string()),
        pa.field("link_attributes_json", pa.string(), nullable=False),
        pa.field("role_type_source_member", pa.string()),
        pa.field("role_type_source_ordinal", pa.int32()),
        pa.field("role_type_attributes_json", pa.string()),
        pa.field("role_definition", pa.large_string()),
        pa.field("used_on_json", pa.string(), nullable=False),
        pa.field("definition_status", pa.string(), nullable=False),
    ]
)

PRESENTATION_RELATIONSHIP_SCHEMA = pa.schema(
    _identity_fields()
    + [
        pa.field("relationship_status", pa.string(), nullable=False),
        pa.field("role_uri", pa.string()),
        pa.field("arcrole_uri", pa.string()),
        pa.field("arc_from_label", pa.string()),
        pa.field("arc_to_label", pa.string()),
        pa.field("arc_order", pa.string()),
        pa.field("preferred_label_role_uri", pa.string()),
        pa.field("arc_priority", pa.string()),
        pa.field("arc_use", pa.string()),
        pa.field("arc_attributes_json", pa.string(), nullable=False),
        pa.field("expansion_ordinal", pa.int32(), nullable=False),
        pa.field("parent_locator_ordinal", pa.int32()),
        pa.field("parent_xlink_label", pa.string()),
        pa.field("parent_reference_uri", pa.string()),
        pa.field("parent_raw_href", pa.string()),
        pa.field("parent_fragment", pa.string()),
        pa.field("parent_target_member", pa.string()),
        pa.field("parent_qname", pa.string()),
        pa.field("parent_resolution_status", pa.string()),
        pa.field("parent_locator_role_uri", pa.string()),
        pa.field("parent_locator_attributes_json", pa.string()),
        pa.field("child_locator_ordinal", pa.int32()),
        pa.field("child_xlink_label", pa.string()),
        pa.field("child_reference_uri", pa.string()),
        pa.field("child_raw_href", pa.string()),
        pa.field("child_fragment", pa.string()),
        pa.field("child_target_member", pa.string()),
        pa.field("child_qname", pa.string()),
        pa.field("child_resolution_status", pa.string()),
        pa.field("child_locator_role_uri", pa.string()),
        pa.field("child_locator_attributes_json", pa.string()),
    ]
)


@dataclass(frozen=True, slots=True)
class TaxonomyTables:
    concept_labels: tuple[dict[str, Any], ...]
    presentation_roles: tuple[dict[str, Any], ...]
    presentation_relationships: tuple[dict[str, Any], ...]
    diagnostics: dict[str, Any]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaxonomyDerivedPaths:
    directory: Path
    concept_labels: Path
    presentation_roles: Path
    presentation_relationships: Path
    metadata: Path


@dataclass(slots=True)
class TaxonomyExtractionSummary:
    files_found: int = 0
    processed: int = 0
    extracted: int = 0
    with_warnings: int = 0
    skipped: int = 0
    failed: int = 0
    label_rows: int = 0
    role_rows: int = 0
    presentation_relationship_rows: int = 0
    failure_reasons: Counter[str] = field(default_factory=Counter)


def _attributes_json(attributes: Iterable[tuple[str, str]]) -> str:
    return json.dumps(
        [[name, value] for name, value in attributes],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _string_list_json(values: Iterable[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _date(value: object) -> date | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _identity(source: XBRLSource) -> dict[str, Any]:
    metadata = source.filing_metadata
    return {
        "schema_version": TABLE_SCHEMA_VERSION,
        "ticker": str(metadata["ticker"]).strip().upper(),
        "cik": int(metadata["cik"]),
        "form": form_directory(str(metadata["form"])),
        "accession_number": str(metadata["accession_number"]).strip(),
        "filing_date": _date(metadata.get("filing_date")),
        "report_date": _date(metadata.get("report_date")),
        "source_package_sha256": source.package_sha256,
    }


def _key(
    value: ExtendedLink | LinkbaseLocator | LinkbaseResource | LinkbaseRelationship,
) -> tuple[str, str, int]:
    return value.linkbase_type, value.source_member, value.link_index


def _locator_columns(prefix: str, locator: LinkbaseLocator | None) -> dict[str, Any]:
    if locator is None:
        return {
            f"{prefix}_locator_ordinal": None,
            f"{prefix}_xlink_label": None,
            f"{prefix}_reference_uri": None,
            f"{prefix}_raw_href": None,
            f"{prefix}_fragment": None,
            f"{prefix}_target_member": None,
            f"{prefix}_qname": None,
            f"{prefix}_resolution_status": None,
            f"{prefix}_locator_role_uri": None,
            f"{prefix}_locator_attributes_json": None,
        }
    reference = locator.reference
    return {
        f"{prefix}_locator_ordinal": locator.source_ordinal,
        f"{prefix}_xlink_label": locator.label,
        f"{prefix}_reference_uri": reference.resolved_uri,
        f"{prefix}_raw_href": reference.raw_href,
        f"{prefix}_fragment": reference.fragment,
        f"{prefix}_target_member": reference.target_member,
        f"{prefix}_qname": reference.concept_qname,
        f"{prefix}_resolution_status": reference.resolution_status,
        f"{prefix}_locator_role_uri": locator.role,
        f"{prefix}_locator_attributes_json": _attributes_json(locator.attributes),
    }


def _label_resource_columns(resource: LinkbaseResource | None) -> dict[str, Any]:
    if resource is None:
        return {
            "label_resource_ordinal": None,
            "label_xlink_label": None,
            "label_role_uri": None,
            "language": None,
            "label_text": None,
            "label_content_xml": None,
            "label_attributes_json": None,
        }
    return {
        "label_resource_ordinal": resource.source_ordinal,
        "label_xlink_label": resource.label,
        "label_role_uri": resource.role,
        "language": resource.language,
        "label_text": resource.value,
        "label_content_xml": resource.content_xml,
        "label_attributes_json": _attributes_json(resource.attributes),
    }


def _relationship_status(first: object | None, second: object | None) -> str:
    if first is not None and second is not None:
        return "resolved"
    if first is None and second is None:
        return "missing_both_endpoints"
    return "missing_from_endpoint" if first is None else "missing_to_endpoint"


def _duplicate_arc_count(
    relationships: Iterable[LinkbaseRelationship],
) -> int:
    signatures = Counter(
        (
            value.linkbase_type,
            value.source_member,
            value.link_index,
            value.link_role,
            value.arcrole,
            value.from_label,
            value.to_label,
            value.order,
            value.preferred_label,
            value.priority,
            value.use,
            value.attributes,
        )
        for value in relationships
    )
    return sum(count - 1 for count in signatures.values() if count > 1)


def build_taxonomy_tables(
    *, source: XBRLSource, inventory: TaxonomyPackageInventory
) -> TaxonomyTables:
    """Convert a source taxonomy graph into deterministic, lossless tables."""
    identity = _identity(source)
    locators_by_key: dict[tuple[str, str, int], list[LinkbaseLocator]] = defaultdict(list)
    resources_by_key: dict[tuple[str, str, int], list[LinkbaseResource]] = defaultdict(list)
    links_by_key = {_key(link): link for link in inventory.extended_links}
    for locator in inventory.locators:
        locators_by_key[_key(locator)].append(locator)
    for resource in inventory.linkbase_resources:
        resources_by_key[_key(resource)].append(resource)

    label_rows: list[dict[str, Any]] = []
    used_label_locators: set[tuple[str, int, int]] = set()
    used_label_resources: set[tuple[str, int, int]] = set()
    label_relationships = sorted(
        (value for value in inventory.relationships if value.linkbase_type == "label"),
        key=lambda value: (value.source_member, value.link_index, value.source_ordinal),
    )
    for relationship in label_relationships:
        key = _key(relationship)
        locators = [
            value
            for value in locators_by_key[key]
            if value.label == relationship.from_label
        ] or [None]
        resources = [
            value
            for value in resources_by_key[key]
            if value.label == relationship.to_label
        ] or [None]
        expansion = 0
        for locator in locators:
            for resource in resources:
                if locator is not None:
                    used_label_locators.add(
                        (locator.source_member, locator.link_index, locator.source_ordinal)
                    )
                if resource is not None:
                    used_label_resources.add(
                        (resource.source_member, resource.link_index, resource.source_ordinal)
                    )
                label_rows.append(
                    {
                        **identity,
                        "row_order": 0,
                        "source_member": relationship.source_member,
                        "extended_link_index": relationship.link_index,
                        "source_ordinal": relationship.source_ordinal,
                        "record_kind": "relationship",
                        "relationship_status": _relationship_status(locator, resource),
                        "extended_link_role_uri": relationship.link_role,
                        "arcrole_uri": relationship.arcrole,
                        "arc_from_label": relationship.from_label,
                        "arc_to_label": relationship.to_label,
                        "arc_order": relationship.order,
                        "arc_priority": relationship.priority,
                        "arc_use": relationship.use,
                        "arc_attributes_json": _attributes_json(relationship.attributes),
                        "expansion_ordinal": expansion,
                        **_locator_columns("concept", locator),
                        **_label_resource_columns(resource),
                    }
                )
                expansion += 1

    label_locators = sorted(
        (value for value in inventory.locators if value.linkbase_type == "label"),
        key=lambda value: (value.source_member, value.link_index, value.source_ordinal),
    )
    label_resources = sorted(
        (
            value
            for value in inventory.linkbase_resources
            if value.linkbase_type == "label"
        ),
        key=lambda value: (value.source_member, value.link_index, value.source_ordinal),
    )
    orphan_label_locators = [
        value
        for value in label_locators
        if (value.source_member, value.link_index, value.source_ordinal)
        not in used_label_locators
    ]
    orphan_label_resources = [
        value
        for value in label_resources
        if (value.source_member, value.link_index, value.source_ordinal)
        not in used_label_resources
    ]
    for locator in orphan_label_locators:
        link = links_by_key.get(_key(locator))
        label_rows.append(
            {
                **identity,
                "row_order": 0,
                "source_member": locator.source_member,
                "extended_link_index": locator.link_index,
                "source_ordinal": locator.source_ordinal,
                "record_kind": "orphan_locator",
                "relationship_status": "not_referenced_by_arc",
                "extended_link_role_uri": link.link_role if link else None,
                "arcrole_uri": None,
                "arc_from_label": None,
                "arc_to_label": None,
                "arc_order": None,
                "arc_priority": None,
                "arc_use": None,
                "arc_attributes_json": None,
                "expansion_ordinal": 0,
                **_locator_columns("concept", locator),
                **_label_resource_columns(None),
            }
        )
    for resource in orphan_label_resources:
        link = links_by_key.get(_key(resource))
        label_rows.append(
            {
                **identity,
                "row_order": 0,
                "source_member": resource.source_member,
                "extended_link_index": resource.link_index,
                "source_ordinal": resource.source_ordinal,
                "record_kind": "orphan_resource",
                "relationship_status": "not_referenced_by_arc",
                "extended_link_role_uri": link.link_role if link else None,
                "arcrole_uri": None,
                "arc_from_label": None,
                "arc_to_label": None,
                "arc_order": None,
                "arc_priority": None,
                "arc_use": None,
                "arc_attributes_json": None,
                "expansion_ordinal": 0,
                **_locator_columns("concept", None),
                **_label_resource_columns(resource),
            }
        )
    label_rows.sort(
        key=lambda row: (
            row["source_member"],
            row["extended_link_index"],
            row["source_ordinal"],
            row["record_kind"],
            row["expansion_ordinal"],
        )
    )
    for row_order, row in enumerate(label_rows):
        row["row_order"] = row_order

    role_types: dict[str, list[RoleType]] = defaultdict(list)
    for role_type in inventory.role_types:
        role_types[role_type.role_uri].append(role_type)
    role_rows: list[dict[str, Any]] = []
    presentation_links = sorted(
        (
            value
            for value in inventory.extended_links
            if value.linkbase_type == "presentation"
        ),
        key=lambda value: (value.source_member, value.link_index, value.link_role or ""),
    )
    used_role_types: set[tuple[str, int, str]] = set()
    for link in presentation_links:
        definitions = role_types.get(link.link_role or "", []) or [None]
        status = (
            "missing_role_type"
            if definitions == [None]
            else "multiple_role_types"
            if len(definitions) > 1
            else "resolved"
        )
        for role_type in definitions:
            if role_type is not None:
                used_role_types.add(
                    (
                        role_type.source_member,
                        role_type.source_ordinal,
                        role_type.role_uri,
                    )
                )
            role_rows.append(
                {
                    **identity,
                    "row_order": 0,
                    "source_member": link.source_member,
                    "extended_link_index": link.link_index,
                    "source_ordinal": link.link_index,
                    "role_uri": link.link_role,
                    "link_attributes_json": _attributes_json(link.attributes),
                    "role_type_source_member": (
                        role_type.source_member if role_type else None
                    ),
                    "role_type_source_ordinal": (
                        role_type.source_ordinal if role_type else None
                    ),
                    "role_type_attributes_json": (
                        _attributes_json(role_type.attributes) if role_type else None
                    ),
                    "role_definition": role_type.definition if role_type else None,
                    "used_on_json": _string_list_json(
                        role_type.used_on if role_type else ()
                    ),
                    "definition_status": status,
                }
            )
    role_rows.sort(
        key=lambda row: (
            row["source_member"],
            row["extended_link_index"],
            row["role_type_source_member"] or "",
        )
    )
    for row_order, row in enumerate(role_rows):
        row["row_order"] = row_order

    presentation_rows: list[dict[str, Any]] = []
    used_presentation_locators: set[tuple[str, int, int]] = set()
    presentation_relationships = sorted(
        (
            value
            for value in inventory.relationships
            if value.linkbase_type == "presentation"
        ),
        key=lambda value: (value.source_member, value.link_index, value.source_ordinal),
    )
    for relationship in presentation_relationships:
        key = _key(relationship)
        parents = [
            value
            for value in locators_by_key[key]
            if value.label == relationship.from_label
        ] or [None]
        children = [
            value
            for value in locators_by_key[key]
            if value.label == relationship.to_label
        ] or [None]
        expansion = 0
        for parent in parents:
            for child in children:
                for locator in (parent, child):
                    if locator is not None:
                        used_presentation_locators.add(
                            (
                                locator.source_member,
                                locator.link_index,
                                locator.source_ordinal,
                            )
                        )
                presentation_rows.append(
                    {
                        **identity,
                        "row_order": 0,
                        "source_member": relationship.source_member,
                        "extended_link_index": relationship.link_index,
                        "source_ordinal": relationship.source_ordinal,
                        "relationship_status": _relationship_status(parent, child),
                        "role_uri": relationship.link_role,
                        "arcrole_uri": relationship.arcrole,
                        "arc_from_label": relationship.from_label,
                        "arc_to_label": relationship.to_label,
                        "arc_order": relationship.order,
                        "preferred_label_role_uri": relationship.preferred_label,
                        "arc_priority": relationship.priority,
                        "arc_use": relationship.use,
                        "arc_attributes_json": _attributes_json(
                            relationship.attributes
                        ),
                        "expansion_ordinal": expansion,
                        **_locator_columns("parent", parent),
                        **_locator_columns("child", child),
                    }
                )
                expansion += 1
    presentation_rows.sort(
        key=lambda row: (
            row["source_member"],
            row["extended_link_index"],
            row["source_ordinal"],
            row["expansion_ordinal"],
        )
    )
    for row_order, row in enumerate(presentation_rows):
        row["row_order"] = row_order

    presentation_locators = [
        value for value in inventory.locators if value.linkbase_type == "presentation"
    ]
    orphan_presentation_locators = [
        value
        for value in presentation_locators
        if (value.source_member, value.link_index, value.source_ordinal)
        not in used_presentation_locators
    ]
    unresolved_label_arcs = sum(
        row["relationship_status"] != "resolved"
        for row in label_rows
        if row["record_kind"] == "relationship"
    )
    unresolved_presentation_arcs = sum(
        row["relationship_status"] != "resolved" for row in presentation_rows
    )
    locator_statuses = Counter(
        value.reference.resolution_status
        for value in inventory.locators
        if value.linkbase_type in {"label", "presentation"}
    )
    unreferenced_role_types = [
        value
        for value in inventory.role_types
        if "link:presentationLink" in value.used_on
        and (value.source_member, value.source_ordinal, value.role_uri)
        not in used_role_types
    ]
    diagnostics = {
        "source_counts": {
            "label_resources": len(label_resources),
            "label_locators": len(label_locators),
            "label_arcs": len(label_relationships),
            "presentation_links": len(presentation_links),
            "presentation_locators": len(presentation_locators),
            "presentation_arcs": len(presentation_relationships),
        },
        "output_counts": {
            "concept_label_rows": len(label_rows),
            "presentation_role_rows": len(role_rows),
            "presentation_relationship_rows": len(presentation_rows),
        },
        "locator_resolution_status_counts": dict(sorted(locator_statuses.items())),
        "orphan_label_locator_count": len(orphan_label_locators),
        "orphan_label_resource_count": len(orphan_label_resources),
        "orphan_presentation_locator_count": len(orphan_presentation_locators),
        "orphan_presentation_locators": [
            {
                "source_member": value.source_member,
                "extended_link_index": value.link_index,
                "source_ordinal": value.source_ordinal,
                "xlink_label": value.label,
                "reference_uri": value.reference.resolved_uri,
                "resolution_status": value.reference.resolution_status,
            }
            for value in sorted(
                orphan_presentation_locators,
                key=lambda item: (
                    item.source_member,
                    item.link_index,
                    item.source_ordinal,
                ),
            )
        ],
        "unresolved_label_arc_row_count": unresolved_label_arcs,
        "unresolved_presentation_arc_row_count": unresolved_presentation_arcs,
        "duplicate_label_arc_count": _duplicate_arc_count(label_relationships),
        "duplicate_presentation_arc_count": _duplicate_arc_count(
            presentation_relationships
        ),
        "unreferenced_presentation_role_type_count": len(unreferenced_role_types),
    }
    warnings = list(inventory.warnings)
    for name, count in (
        ("orphan label locators", len(orphan_label_locators)),
        ("orphan label resources", len(orphan_label_resources)),
        ("orphan presentation locators", len(orphan_presentation_locators)),
        ("unresolved label arc rows", unresolved_label_arcs),
        ("unresolved presentation arc rows", unresolved_presentation_arcs),
    ):
        if count:
            warnings.append(f"{count} {name}")
    return TaxonomyTables(
        concept_labels=tuple(label_rows),
        presentation_roles=tuple(role_rows),
        presentation_relationships=tuple(presentation_rows),
        diagnostics=diagnostics,
        warnings=tuple(sorted(set(warnings))),
    )


def _derived_paths(
    output_root: Path, metadata: dict[str, Any]
) -> TaxonomyDerivedPaths:
    directory = filing_directory(
        output_root,
        ticker=str(metadata["ticker"]),
        form=str(metadata["form"]),
        accession_number=str(metadata["accession_number"]),
    )
    return TaxonomyDerivedPaths(
        directory=directory,
        concept_labels=directory / LABELS_FILENAME,
        presentation_roles=directory / ROLES_FILENAME,
        presentation_relationships=directory / RELATIONSHIPS_FILENAME,
        metadata=directory / METADATA_FILENAME,
    )


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _schema_metadata(schema: pa.Schema) -> list[dict[str, Any]]:
    return [
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in schema
    ]


def _base_metadata(source: XBRLSource) -> dict[str, Any]:
    metadata = source.filing_metadata
    return {
        "schema_version": METADATA_SCHEMA_VERSION,
        "table_schema_version": TABLE_SCHEMA_VERSION,
        "parser": "xbrl_taxonomy_tables",
        "parser_version": PARSER_VERSION,
        "ticker": metadata.get("ticker"),
        "cik": metadata.get("cik"),
        "form": metadata.get("form"),
        "accession_number": metadata.get("accession_number"),
        "filing_date": metadata.get("filing_date"),
        "report_date": metadata.get("report_date"),
        "source_package_path": str(source.package_path),
        "source_package_sha256": source.package_sha256,
    }


def _failure_metadata(
    *,
    metadata: dict[str, Any],
    package_path: Path,
    source_package_sha256: str | None,
    reason: str,
    details: str,
    now: Callable[[], datetime] | None,
) -> dict[str, Any]:
    generated_at = (now or (lambda: datetime.now(timezone.utc)))()
    return {
        "schema_version": METADATA_SCHEMA_VERSION,
        "table_schema_version": TABLE_SCHEMA_VERSION,
        "parser": "xbrl_taxonomy_tables",
        "parser_version": PARSER_VERSION,
        "status": "failure",
        "ticker": metadata.get("ticker"),
        "cik": metadata.get("cik"),
        "form": metadata.get("form"),
        "accession_number": metadata.get("accession_number"),
        "source_package_path": str(package_path),
        "source_package_sha256": source_package_sha256,
        "failure_reason": reason,
        "details": details,
        "warnings": [],
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
    }


def write_taxonomy_tables(
    *,
    source: XBRLSource,
    tables: TaxonomyTables,
    output_root: Path,
    now: Callable[[], datetime] | None = None,
) -> TaxonomyDerivedPaths:
    """Publish all three Parquet tables, with metadata as the completion marker."""
    paths = _derived_paths(output_root, source.filing_metadata)
    paths.directory.mkdir(parents=True, exist_ok=True)
    processing = {
        **_base_metadata(source),
        "status": "processing",
        "warnings": [],
    }
    _atomic_write_json(paths.metadata, processing)
    table_specs = (
        (paths.concept_labels, tables.concept_labels, CONCEPT_LABEL_SCHEMA),
        (paths.presentation_roles, tables.presentation_roles, PRESENTATION_ROLE_SCHEMA),
        (
            paths.presentation_relationships,
            tables.presentation_relationships,
            PRESENTATION_RELATIONSHIP_SCHEMA,
        ),
    )
    temporary_paths: list[Path] = []
    try:
        for path, rows, schema in table_specs:
            temporary = path.with_name(f".{path.name}.tmp")
            temporary_paths.append(temporary)
            table = pa.Table.from_pylist(list(rows), schema=schema)
            pq.write_table(table, temporary, compression="zstd")
        for (path, _, _), temporary in zip(table_specs, temporary_paths, strict=True):
            os.replace(temporary, path)
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)

    generated_at = (now or (lambda: datetime.now(timezone.utc)))()
    metadata = {
        **_base_metadata(source),
        "status": "warning" if tables.warnings else "success",
        "files": {
            "concept_labels": paths.concept_labels.name,
            "presentation_roles": paths.presentation_roles.name,
            "presentation_relationships": paths.presentation_relationships.name,
        },
        "table_schemas": {
            "concept_labels": _schema_metadata(CONCEPT_LABEL_SCHEMA),
            "presentation_roles": _schema_metadata(PRESENTATION_ROLE_SCHEMA),
            "presentation_relationships": _schema_metadata(
                PRESENTATION_RELATIONSHIP_SCHEMA
            ),
        },
        "diagnostics": tables.diagnostics,
        "warnings": list(tables.warnings),
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
    }
    _atomic_write_json(paths.metadata, metadata)
    return paths


def _current_bundle(paths: TaxonomyDerivedPaths, source_sha256: str) -> bool:
    if not all(
        path.is_file()
        for path in (
            paths.concept_labels,
            paths.presentation_roles,
            paths.presentation_relationships,
            paths.metadata,
        )
    ):
        return False
    try:
        metadata = read_json_object(paths.metadata)
    except ValueError:
        return False
    return (
        metadata.get("status") in {"success", "warning"}
        and metadata.get("schema_version") == METADATA_SCHEMA_VERSION
        and metadata.get("table_schema_version") == TABLE_SCHEMA_VERSION
        and metadata.get("parser_version") == PARSER_VERSION
        and metadata.get("source_package_sha256") == source_sha256
    )


def _matches(
    metadata: dict[str, Any],
    *,
    ticker: str | None,
    form_type: str | None,
    accession_number: str | None,
) -> bool:
    if ticker and str(metadata.get("ticker", "")).strip().upper() != ticker.upper():
        return False
    if form_type and form_directory(str(metadata.get("form", ""))) != form_directory(
        form_type
    ):
        return False
    return not (
        accession_number
        and str(metadata.get("accession_number", "")).strip()
        != accession_number.strip()
    )


def _validate_metadata_layout(
    metadata: dict[str, Any], *, metadata_path: Path, download_root: Path
) -> None:
    ticker = str(metadata["ticker"]).strip().upper()
    form = form_directory(str(metadata["form"]))
    accession = str(metadata["accession_number"]).strip()
    if not ticker or not accession:
        raise ValueError("metadata ticker and accession number must not be empty")
    expected = filing_directory(
        download_root,
        ticker=ticker,
        form=form,
        accession_number=accession,
    ) / "metadata.json"
    if metadata_path != expected:
        raise ValueError("filing metadata does not match its storage hierarchy")


def _label(metadata: dict[str, Any]) -> str:
    return (
        f"{str(metadata['ticker']).upper()}/{str(metadata['form']).upper()}/"
        f"{metadata['accession_number']}"
    )


def extract_taxonomy_filings(
    *,
    download_root: Path,
    output_root: Path,
    ticker: str | None = None,
    form_type: str | None = None,
    accession_number: str | None = None,
    overwrite: bool = False,
    printer: Callable[[str], None] = print,
    now: Callable[[], datetime] | None = None,
) -> TaxonomyExtractionSummary:
    """Materialize labels and presentation networks for matching local filings."""
    summary = TaxonomyExtractionSummary()
    for metadata_path in sorted(download_root.glob("*/*/*/metadata.json")):
        filing_path = metadata_path.parent
        try:
            metadata = read_json_object(metadata_path)
        except (OSError, TypeError, ValueError) as exc:
            summary.files_found += 1
            summary.processed += 1
            summary.failed += 1
            summary.failure_reasons["invalid_local_metadata"] += 1
            printer(f"[FAIL] {metadata_path} — invalid_local_metadata: {exc}")
            continue
        if not _matches(
            metadata,
            ticker=ticker,
            form_type=form_type,
            accession_number=accession_number,
        ):
            continue
        summary.files_found += 1
        summary.processed += 1
        try:
            _validate_metadata_layout(
                metadata, metadata_path=metadata_path, download_root=download_root
            )
            paths = _derived_paths(output_root, metadata)
            label = _label(metadata)
        except (KeyError, TypeError, ValueError) as exc:
            summary.failed += 1
            summary.failure_reasons["invalid_local_metadata"] += 1
            printer(f"[FAIL] {metadata_path} — invalid_local_metadata: {exc}")
            continue

        source: XBRLSource | None = None
        try:
            source = inventory_xbrl_source(filing_path)
            if not overwrite and _current_bundle(paths, source.package_sha256):
                summary.processed -= 1
                summary.skipped += 1
                printer(f"[SKIP] {label} — current taxonomy tables already exist")
                continue
            inventory = inventory_taxonomy_package(source.package_path)
            tables = build_taxonomy_tables(source=source, inventory=inventory)
            write_taxonomy_tables(
                source=source,
                tables=tables,
                output_root=output_root,
                now=now,
            )
        except TaxonomyInventoryError as exc:
            reason = exc.reason
            details = exc.details
        except (KeyError, OSError, TypeError, ValueError) as exc:
            reason = "taxonomy_extraction_error"
            details = str(exc)
        else:
            summary.extracted += 1
            summary.label_rows += len(tables.concept_labels)
            summary.role_rows += len(tables.presentation_roles)
            summary.presentation_relationship_rows += len(
                tables.presentation_relationships
            )
            if tables.warnings:
                summary.with_warnings += 1
            prefix = "WARNING" if tables.warnings else "PASS"
            printer(
                f"[{prefix}] {label} — {len(tables.concept_labels)} label rows; "
                f"{len(tables.presentation_roles)} roles; "
                f"{len(tables.presentation_relationships)} presentation relationships"
            )
            continue

        summary.failed += 1
        summary.failure_reasons[reason] += 1
        try:
            failure = _failure_metadata(
                metadata=metadata,
                package_path=filing_path / "xbrl" / "package.zip",
                source_package_sha256=(source.package_sha256 if source else None),
                reason=reason,
                details=details,
                now=now,
            )
            _atomic_write_json(paths.metadata, failure)
        except (OSError, TypeError, ValueError):
            pass
        printer(f"[FAIL] {label} — {reason}: {details}")

    printer("")
    printer("Taxonomy extraction summary")
    printer("---------------------------")
    printer(f"Filings found:              {summary.files_found}")
    printer(f"Processed:                  {summary.processed}")
    printer(f"Extracted:                  {summary.extracted}")
    printer(f"With warnings:              {summary.with_warnings}")
    printer(f"Skipped:                    {summary.skipped}")
    printer(f"Failed:                     {summary.failed}")
    printer(f"Concept label rows:         {summary.label_rows}")
    printer(f"Presentation role rows:     {summary.role_rows}")
    printer(
        f"Presentation relationships: {summary.presentation_relationship_rows}"
    )
    if summary.failure_reasons:
        printer("")
        printer("Failure reasons:")
        for reason, count in sorted(summary.failure_reasons.items()):
            printer(f"  {reason}: {count}")
    return summary
