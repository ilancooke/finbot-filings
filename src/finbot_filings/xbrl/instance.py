"""Standards-shaped extraction from SEC-generated XBRL instance documents."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from lxml import etree

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XML = "http://www.w3.org/XML/1998/namespace"
XBRL_ROOT = f"{{{XBRLI}}}xbrl"


class XBRLParseError(ValueError):
    """Raised when an instance cannot be normalized without losing meaning."""


@dataclass(frozen=True, slots=True)
class InstanceParseResult:
    facts: list[dict[str, Any]]
    context_count: int
    unit_count: int
    namespace_count: int
    duplicate_group_count: int
    value_kind_counts: dict[str, int]


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _expanded_qname(value: str, element: etree._Element) -> str:
    value = value.strip()
    if value.startswith("{"):
        return value
    if ":" in value:
        prefix, local_name = value.split(":", 1)
        namespace = element.nsmap.get(prefix)
        if namespace is None:
            raise XBRLParseError(f"unknown QName prefix {prefix!r} in {value!r}")
        return f"{{{namespace}}}{local_name}"
    return value


def _date(value: str | None, *, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise XBRLParseError(f"invalid {field} date {value!r}") from exc


def _first_text(element: etree._Element, xpath: str) -> str | None:
    found = element.find(xpath)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _dimensions(context: etree._Element) -> list[dict[str, str]]:
    dimensions: list[dict[str, str]] = []
    for member in context.iterfind(f".//{{{XBRLDI}}}explicitMember"):
        dimension = member.get("dimension")
        value = (member.text or "").strip()
        if not dimension or not value:
            raise XBRLParseError("explicit dimension is missing its dimension or member")
        dimensions.append(
            {
                "kind": "explicit",
                "dimension_qname": _expanded_qname(dimension, member),
                "member_qname": _expanded_qname(value, member),
            }
        )
    for member in context.iterfind(f".//{{{XBRLDI}}}typedMember"):
        dimension = member.get("dimension")
        if not dimension:
            raise XBRLParseError("typed dimension is missing its dimension")
        children = list(member)
        typed_xml = "".join(
            etree.tostring(child, method="c14n", with_comments=False).decode("utf-8")
            for child in children
        )
        dimensions.append(
            {
                "kind": "typed",
                "dimension_qname": _expanded_qname(dimension, member),
                "typed_value_xml": typed_xml,
            }
        )
    return sorted(dimensions, key=lambda item: _json(item))


def _parse_contexts(root: etree._Element) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for context in root.findall(f"{{{XBRLI}}}context"):
        context_id = context.get("id")
        if not context_id or context_id in contexts:
            raise XBRLParseError("context IDs must be present and unique")
        identifier = context.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}identifier")
        if identifier is None or not (identifier.text or "").strip():
            raise XBRLParseError(f"context {context_id!r} has no entity identifier")

        instant = _first_text(context, f"{{{XBRLI}}}period/{{{XBRLI}}}instant")
        start = _first_text(context, f"{{{XBRLI}}}period/{{{XBRLI}}}startDate")
        end = _first_text(context, f"{{{XBRLI}}}period/{{{XBRLI}}}endDate")
        forever = context.find(f"{{{XBRLI}}}period/{{{XBRLI}}}forever") is not None
        if instant is not None:
            period_kind = "instant"
        elif start is not None and end is not None:
            period_kind = "duration"
        elif forever:
            period_kind = "forever"
        else:
            raise XBRLParseError(f"context {context_id!r} has an invalid period")

        dimensions = _dimensions(context)
        aspects = {
            "entity_identifier": (identifier.text or "").strip(),
            "entity_scheme": identifier.get("scheme"),
            "period_kind": period_kind,
            "period_start": start,
            "period_end": end,
            "period_instant": instant,
            "dimensions": dimensions,
        }
        contexts[context_id] = {
            **aspects,
            "period_start_date": _date(start, field="period start"),
            "period_end_date": _date(end, field="period end"),
            "period_instant_date": _date(instant, field="period instant"),
            "dimensions_json": _json(dimensions),
            "context_signature": _sha256_text(_json(aspects)),
        }
    return contexts


def _measure_list(parent: etree._Element | None) -> list[str]:
    if parent is None:
        return []
    return [
        _expanded_qname((measure.text or "").strip(), measure)
        for measure in parent.findall(f"{{{XBRLI}}}measure")
        if (measure.text or "").strip()
    ]


def _parse_units(root: etree._Element) -> dict[str, dict[str, str]]:
    units: dict[str, dict[str, str]] = {}
    for unit in root.findall(f"{{{XBRLI}}}unit"):
        unit_id = unit.get("id")
        if not unit_id or unit_id in units:
            raise XBRLParseError("unit IDs must be present and unique")
        divide = unit.find(f"{{{XBRLI}}}divide")
        if divide is None:
            numerator = _measure_list(unit)
            denominator: list[str] = []
        else:
            numerator = _measure_list(divide.find(f"{{{XBRLI}}}unitNumerator"))
            denominator = _measure_list(divide.find(f"{{{XBRLI}}}unitDenominator"))
        if not numerator:
            raise XBRLParseError(f"unit {unit_id!r} has no numerator measure")
        definition = {"numerator": numerator, "denominator": denominator}
        display = " * ".join(etree.QName(value).localname for value in numerator)
        if denominator:
            display += " / " + " * ".join(
                etree.QName(value).localname for value in denominator
            )
        units[unit_id] = {
            "unit_json": _json(definition),
            "unit_display": display,
        }
    return units


def parse_instance_document(
    instance_bytes: bytes,
    *,
    filing_metadata: Mapping[str, Any],
    instance_filename: str,
) -> InstanceParseResult:
    """Extract every item fact without enumerating financial concepts."""
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        recover=False,
        huge_tree=True,
    )
    try:
        root = etree.fromstring(instance_bytes, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise XBRLParseError(f"invalid XBRL instance XML: {exc}") from exc
    if root.tag != XBRL_ROOT:
        raise XBRLParseError(f"expected XBRL instance root, found {root.tag!r}")

    contexts = _parse_contexts(root)
    units = _parse_units(root)
    filing_date = _date(
        str(filing_metadata["filing_date"])
        if filing_metadata.get("filing_date")
        else None,
        field="filing",
    )
    report_date = _date(
        str(filing_metadata["report_date"])
        if filing_metadata.get("report_date")
        else None,
        field="report",
    )

    facts: list[dict[str, Any]] = []
    duplicate_keys: list[str] = []
    namespaces: set[str] = set()
    for element in root.iter():
        context_id = element.get("contextRef")
        if context_id is None:
            continue
        context = contexts.get(context_id)
        if context is None:
            raise XBRLParseError(
                f"fact references missing context {context_id!r}"
            )
        concept = etree.QName(element)
        namespaces.add(concept.namespace or "")
        unit_id = element.get("unitRef")
        if unit_id is not None and unit_id not in units:
            raise XBRLParseError(f"fact references missing unit {unit_id!r}")
        unit = units.get(unit_id or "", {"unit_json": None, "unit_display": None})
        is_nil = element.get(f"{{{XSI}}}nil", "false").lower() in {"true", "1"}
        value_text = "" if is_nil else "".join(element.itertext()).strip()
        value_kind = "nil" if is_nil else "numeric" if unit_id else "non_numeric"
        concept_qname = element.tag
        xml_language = element.get(f"{{{XML}}}lang")
        duplicate_key = _json(
            {
                "concept_qname": concept_qname,
                "context_signature": context["context_signature"],
                "unit": unit["unit_json"],
                "xml_language": xml_language,
            }
        )
        duplicate_keys.append(duplicate_key)
        facts.append(
            {
                "schema_version": 1,
                "ticker": str(filing_metadata["ticker"]).strip().upper(),
                "cik": int(filing_metadata["cik"]),
                "form": str(filing_metadata["form"]).strip().upper(),
                "accession_number": str(filing_metadata["accession_number"]),
                "filing_date": filing_date,
                "report_date": report_date,
                "instance_document": instance_filename,
                "document_order": len(facts) + 1,
                "fact_id": element.get("id"),
                "concept_qname": concept_qname,
                "concept_namespace": concept.namespace or "",
                "concept_name": concept.localname,
                "concept_prefix": element.prefix,
                "value_text": value_text,
                "value_kind": value_kind,
                "context_id": context_id,
                "entity_identifier": context["entity_identifier"],
                "entity_scheme": context["entity_scheme"],
                "context_period_kind": context["period_kind"],
                "period_start": context["period_start_date"],
                "period_end": context["period_end_date"],
                "period_instant": context["period_instant_date"],
                "unit_id": unit_id,
                "unit_json": unit["unit_json"],
                "unit_display": unit["unit_display"],
                "dimensions_json": context["dimensions_json"],
                "context_signature": context["context_signature"],
                "decimals": element.get("decimals"),
                "precision": element.get("precision"),
                "is_nil": is_nil,
                "xml_language": xml_language,
                "duplicate_group_id": _sha256_text(duplicate_key),
                "duplicate_group_size": 0,
            }
        )

    group_sizes = Counter(duplicate_keys)
    for fact, duplicate_key in zip(facts, duplicate_keys, strict=True):
        fact["duplicate_group_size"] = group_sizes[duplicate_key]
    return InstanceParseResult(
        facts=facts,
        context_count=len(contexts),
        unit_count=len(units),
        namespace_count=len(namespaces),
        duplicate_group_count=sum(1 for count in group_sizes.values() if count > 1),
        value_kind_counts=dict(Counter(fact["value_kind"] for fact in facts)),
    )
