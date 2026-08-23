"""Secure, no-network parsing of taxonomy resources inside one SEC ZIP."""

from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlparse

from lxml import etree

from finbot_filings.xbrl.source import (
    MAX_PACKAGE_MEMBERS,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
)
from finbot_filings.xbrl.taxonomy.models import (
    ArcroleType,
    ExtendedLink,
    LinkbaseLocator,
    LinkbaseRelationship,
    LinkbaseResource,
    RoleType,
    TaxonomyPackageInventory,
    TaxonomyReference,
    TaxonomyResource,
)

XSD = "http://www.w3.org/2001/XMLSchema"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XML = "http://www.w3.org/XML/1998/namespace"
XBRLI = "http://www.xbrl.org/2003/instance"

XSD_SCHEMA = f"{{{XSD}}}schema"
LINKBASE = f"{{{LINK}}}linkbase"
XBRL_INSTANCE = f"{{{XBRLI}}}xbrl"
PACKAGE_BASE = "file:///__finbot_xbrl_package__/"

MAX_XML_MEMBER_BYTES = 67_108_864
MAX_TOTAL_XML_BYTES = 268_435_456
MAX_XML_COMPRESSION_RATIO = 200.0

LINKBASE_TYPES = {
    "label": (f"{{{LINK}}}labelLink", f"{{{LINK}}}labelArc"),
    "presentation": (
        f"{{{LINK}}}presentationLink",
        f"{{{LINK}}}presentationArc",
    ),
    "definition": (f"{{{LINK}}}definitionLink", f"{{{LINK}}}definitionArc"),
    "calculation": (
        f"{{{LINK}}}calculationLink",
        f"{{{LINK}}}calculationArc",
    ),
    "reference": (f"{{{LINK}}}referenceLink", f"{{{LINK}}}referenceArc"),
}
REFERENCE_TAGS = {
    f"{{{XSD}}}import": "schema_import",
    f"{{{XSD}}}include": "schema_include",
    f"{{{XSD}}}redefine": "schema_redefine",
    f"{{{LINK}}}linkbaseRef": "linkbase_ref",
    f"{{{LINK}}}roleRef": "role_ref",
    f"{{{LINK}}}arcroleRef": "arcrole_ref",
    f"{{{LINK}}}schemaRef": "schema_ref",
}
XML_CONTENT_HINT = re.compile(
    rb"(?:www\.w3\.org/2001/XMLSchema|www\.xbrl\.org/2003/linkbase)",
    re.IGNORECASE,
)


class TaxonomyInventoryError(ValueError):
    """Raised when a package cannot be safely and deterministically inventoried."""

    def __init__(self, reason: str, details: str):
        super().__init__(details)
        self.reason = reason
        self.details = details


def _xml_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        recover=False,
        huge_tree=False,
        remove_comments=False,
    )


def _member_base_uri(member_name: str) -> str:
    return PACKAGE_BASE + "/".join(PurePosixPath(member_name).parts)


def _normalize_text(element: etree._Element | None) -> str | None:
    if element is None:
        return None
    value = " ".join("".join(element.itertext()).split())
    return value or None


def _attributes(element: etree._Element) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(name), value) for name, value in element.attrib.items()))


def _read_xml_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
) -> tuple[bytes, etree._Element] | None:
    suffix = PurePosixPath(info.filename).suffix.lower()
    likely_xml = suffix in {".xml", ".xsd"}
    if not likely_xml:
        with archive.open(info) as stream:
            prefix = stream.read(min(info.file_size, 8_192))
        if b"<html" in prefix.lower() or not XML_CONTENT_HINT.search(prefix):
            return None
    if info.file_size > MAX_XML_MEMBER_BYTES:
        raise TaxonomyInventoryError(
            "resource_limit_exceeded",
            f"XML member exceeds {MAX_XML_MEMBER_BYTES} bytes: {info.filename!r}",
        )
    ratio = info.file_size / max(info.compress_size, 1)
    if ratio > MAX_XML_COMPRESSION_RATIO:
        raise TaxonomyInventoryError(
            "resource_limit_exceeded",
            f"XML member compression ratio exceeds {MAX_XML_COMPRESSION_RATIO}: "
            f"{info.filename!r}",
        )
    value = archive.read(info)
    try:
        root = etree.fromstring(
            value,
            parser=_xml_parser(),
            base_url=_member_base_uri(info.filename),
        )
    except etree.XMLSyntaxError as exc:
        raise TaxonomyInventoryError(
            "invalid_taxonomy_xml", f"invalid XML member {info.filename!r}: {exc}"
        ) from exc
    if root.getroottree().docinfo.doctype:
        raise TaxonomyInventoryError(
            "doctype_not_allowed",
            f"DOCTYPE is not allowed in taxonomy XML: {info.filename!r}",
        )
    return value, root


def _resource_types(root: etree._Element) -> tuple[str, ...]:
    values: list[str] = []
    if root.tag == XSD_SCHEMA:
        values.append("schema")
    elif root.tag == LINKBASE:
        values.append("linkbase")
    elif root.tag == XBRL_INSTANCE:
        values.append("instance")
    else:
        values.append("other_xml")
    return tuple(values)


def _embedded_linkbase_types(root: etree._Element) -> tuple[str, ...]:
    return tuple(
        kind
        for kind, (link_tag, _) in LINKBASE_TYPES.items()
        if root.find(f".//{link_tag}") is not None
    )


def _xml_id_index(root: etree._Element) -> dict[str, etree._Element]:
    result: dict[str, etree._Element] = {}
    for element in root.iter():
        identifier = element.get("id") or element.get(f"{{{XML}}}id")
        if identifier and identifier not in result:
            result[identifier] = element
    return result


def _local_concept_qname(
    *,
    target: etree._Element | None,
    target_root: etree._Element | None,
) -> str | None:
    if target is None or target_root is None or target.tag != f"{{{XSD}}}element":
        return None
    local_name = target.get("name")
    namespace = target_root.get("targetNamespace")
    if not local_name or not namespace:
        return None
    return f"{{{namespace}}}{local_name}"


def _resolve_reference(
    *,
    reference_type: str,
    source_member: str,
    element: etree._Element,
    raw_href: str,
    namespace: str | None,
    package_members: set[str],
    roots: dict[str, etree._Element],
    ids: dict[str, dict[str, etree._Element]],
) -> TaxonomyReference:
    base_uri = element.base or _member_base_uri(source_member)
    resolved_uri = urljoin(base_uri, raw_href)
    parsed = urlparse(resolved_uri)
    fragment = unquote(parsed.fragment) or None
    target_member: str | None = None
    concept_qname: str | None = None

    if parsed.scheme in {"http", "https"}:
        status = "external_dependency"
    elif parsed.scheme == "file":
        package_root = "/__finbot_xbrl_package__/"
        path = unquote(parsed.path)
        if not path.startswith(package_root):
            status = "unsafe_reference"
        else:
            relative = posixpath.normpath(path[len(package_root) :])
            if relative in {"", "."}:
                relative = source_member
            member = PurePosixPath(relative)
            if member.is_absolute() or ".." in member.parts:
                status = "unsafe_reference"
            else:
                target_member = str(member)
                if target_member not in package_members:
                    status = "missing_local_member"
                elif fragment and fragment not in ids.get(target_member, {}):
                    status = "missing_fragment"
                elif target_member == source_member:
                    status = "same_document"
                else:
                    status = "embedded_package_member"
                if status in {"same_document", "embedded_package_member"} and fragment:
                    concept_qname = _local_concept_qname(
                        target=ids.get(target_member, {}).get(fragment),
                        target_root=roots.get(target_member),
                    )
    else:
        status = "unsupported_uri_scheme"

    return TaxonomyReference(
        reference_type=reference_type,
        source_member=source_member,
        raw_href=raw_href,
        resolved_uri=resolved_uri,
        namespace=namespace,
        fragment=fragment,
        target_member=target_member,
        resolution_status=status,
        concept_qname=concept_qname,
        attributes=_attributes(element),
    )


def _references(
    *,
    source_member: str,
    root: etree._Element,
    package_members: set[str],
    roots: dict[str, etree._Element],
    ids: dict[str, dict[str, etree._Element]],
) -> list[TaxonomyReference]:
    result: list[TaxonomyReference] = []
    for element in root.iter():
        reference_type = REFERENCE_TAGS.get(element.tag)
        if reference_type is None:
            continue
        href_attribute = "schemaLocation" if element.tag in {
            f"{{{XSD}}}import",
            f"{{{XSD}}}include",
            f"{{{XSD}}}redefine",
        } else f"{{{XLINK}}}href"
        raw_href = (element.get(href_attribute) or "").strip()
        if not raw_href:
            continue
        result.append(
            _resolve_reference(
                reference_type=reference_type,
                source_member=source_member,
                element=element,
                raw_href=raw_href,
                namespace=element.get("namespace"),
                package_members=package_members,
                roots=roots,
                ids=ids,
            )
        )
    return result


def _role_types(source_member: str, root: etree._Element) -> list[RoleType]:
    result: list[RoleType] = []
    for source_ordinal, element in enumerate(
        root.findall(f".//{{{LINK}}}roleType")
    ):
        role_uri = (element.get("roleURI") or "").strip()
        if not role_uri:
            continue
        result.append(
            RoleType(
                source_member=source_member,
                source_ordinal=source_ordinal,
                role_uri=role_uri,
                definition=_normalize_text(element.find(f"{{{LINK}}}definition")),
                used_on=tuple(
                    value
                    for child in element.findall(f"{{{LINK}}}usedOn")
                    if (value := _normalize_text(child)) is not None
                ),
                attributes=_attributes(element),
            )
        )
    return result


def _arcrole_types(source_member: str, root: etree._Element) -> list[ArcroleType]:
    result: list[ArcroleType] = []
    for element in root.findall(f".//{{{LINK}}}arcroleType"):
        arcrole_uri = (element.get("arcroleURI") or "").strip()
        if not arcrole_uri:
            continue
        result.append(
            ArcroleType(
                source_member=source_member,
                arcrole_uri=arcrole_uri,
                cycles_allowed=element.get("cyclesAllowed"),
                definition=_normalize_text(element.find(f"{{{LINK}}}definition")),
                used_on=tuple(
                    value
                    for child in element.findall(f"{{{LINK}}}usedOn")
                    if (value := _normalize_text(child)) is not None
                ),
            )
        )
    return result


def _linkbase_records(
    *,
    source_member: str,
    root: etree._Element,
    package_members: set[str],
    roots: dict[str, etree._Element],
    ids: dict[str, dict[str, etree._Element]],
    inventory: TaxonomyPackageInventory,
) -> None:
    for linkbase_type, (link_tag, arc_tag) in LINKBASE_TYPES.items():
        links = root.findall(f".//{link_tag}")
        inventory.link_counts[linkbase_type] = (
            inventory.link_counts.get(linkbase_type, 0) + len(links)
        )
        for link_index, link in enumerate(links):
            link_role = link.get(f"{{{XLINK}}}role")
            inventory.extended_links.append(
                ExtendedLink(
                    linkbase_type=linkbase_type,
                    source_member=source_member,
                    link_index=link_index,
                    link_role=link_role,
                    attributes=_attributes(link),
                )
            )
            endpoint_labels: set[str] = set()
            for locator_ordinal, locator in enumerate(
                link.findall(f"{{{LINK}}}loc")
            ):
                label = (locator.get(f"{{{XLINK}}}label") or "").strip()
                href = (locator.get(f"{{{XLINK}}}href") or "").strip()
                if href:
                    reference = _resolve_reference(
                        reference_type="locator",
                        source_member=source_member,
                        element=locator,
                        raw_href=href,
                        namespace=None,
                        package_members=package_members,
                        roots=roots,
                        ids=ids,
                    )
                else:
                    reference = TaxonomyReference(
                        reference_type="locator",
                        source_member=source_member,
                        raw_href="",
                        resolved_uri="",
                        namespace=None,
                        fragment=None,
                        target_member=None,
                        resolution_status="missing_href",
                        attributes=_attributes(locator),
                    )
                    inventory.warnings.append(
                        f"locator without href in {source_member} "
                        f"{linkbase_type} link {link_index}"
                    )
                if not label:
                    inventory.warnings.append(
                        f"locator without label in {source_member} "
                        f"{linkbase_type} link {link_index}"
                    )
                inventory.references.append(reference)
                inventory.locators.append(
                    LinkbaseLocator(
                        linkbase_type=linkbase_type,
                        source_member=source_member,
                        link_index=link_index,
                        source_ordinal=locator_ordinal,
                        label=label,
                        role=locator.get(f"{{{XLINK}}}role"),
                        reference=reference,
                        attributes=_attributes(locator),
                    )
                )
                endpoint_labels.add(label)
            resource_tag = (
                f"{{{LINK}}}label" if linkbase_type == "label" else
                f"{{{LINK}}}reference" if linkbase_type == "reference" else None
            )
            if resource_tag is not None:
                for resource_ordinal, resource in enumerate(
                    link.findall(resource_tag)
                ):
                    label = (resource.get(f"{{{XLINK}}}label") or "").strip()
                    if not label:
                        inventory.warnings.append(
                            f"resource without label in {source_member} "
                            f"{linkbase_type} link {link_index}"
                        )
                    inventory.linkbase_resources.append(
                        LinkbaseResource(
                            linkbase_type=linkbase_type,
                            source_member=source_member,
                            link_index=link_index,
                            source_ordinal=resource_ordinal,
                            label=label,
                            role=resource.get(f"{{{XLINK}}}role"),
                            language=resource.get(f"{{{XML}}}lang"),
                            value=" ".join("".join(resource.itertext()).split()),
                            content_xml=etree.tostring(
                                resource, method="c14n", with_comments=False
                            ).decode("utf-8"),
                            attributes=_attributes(resource),
                        )
                    )
                    endpoint_labels.add(label)
            for arc_ordinal, arc in enumerate(link.findall(arc_tag)):
                from_label = (arc.get(f"{{{XLINK}}}from") or "").strip()
                to_label = (arc.get(f"{{{XLINK}}}to") or "").strip()
                if from_label not in endpoint_labels or to_label not in endpoint_labels:
                    inventory.warnings.append(
                        f"unresolved arc endpoint in {source_member} "
                        f"{linkbase_type} link {link_index}: {from_label!r} -> {to_label!r}"
                    )
                inventory.relationships.append(
                    LinkbaseRelationship(
                        linkbase_type=linkbase_type,
                        source_member=source_member,
                        link_index=link_index,
                        source_ordinal=arc_ordinal,
                        link_role=link_role,
                        arcrole=arc.get(f"{{{XLINK}}}arcrole"),
                        from_label=from_label,
                        to_label=to_label,
                        order=arc.get("order"),
                        priority=arc.get("priority"),
                        use=arc.get("use"),
                        weight=arc.get("weight"),
                        preferred_label=arc.get("preferredLabel"),
                        attributes=_attributes(arc),
                    )
                )


def inventory_taxonomy_package(package_path: Path) -> TaxonomyPackageInventory:
    """Parse all local taxonomy resources without resolving network references."""
    inventory = TaxonomyPackageInventory()
    try:
        archive = zipfile.ZipFile(package_path)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise TaxonomyInventoryError(
            "invalid_xbrl_package", f"invalid XBRL package {package_path}"
        ) from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_PACKAGE_MEMBERS:
            raise TaxonomyInventoryError(
                "resource_limit_exceeded",
                f"package contains more than {MAX_PACKAGE_MEMBERS} members",
            )
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise TaxonomyInventoryError(
                "duplicate_package_member", "package contains duplicate member names"
            )
        total_size = sum(info.file_size for info in infos)
        if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise TaxonomyInventoryError(
                "resource_limit_exceeded",
                f"package uncompressed size exceeds {MAX_TOTAL_UNCOMPRESSED_BYTES} bytes",
            )
        for name in names:
            member = PurePosixPath(name)
            if member.is_absolute() or ".." in member.parts:
                raise TaxonomyInventoryError(
                    "unsafe_package_member", f"unsafe archive member {name!r}"
                )

        parsed: dict[str, tuple[bytes, etree._Element]] = {}
        xml_total = 0
        for info in infos:
            value = _read_xml_member(archive, info)
            if value is None:
                continue
            xml_bytes, root = value
            xml_total += len(xml_bytes)
            if xml_total > MAX_TOTAL_XML_BYTES:
                raise TaxonomyInventoryError(
                    "resource_limit_exceeded",
                    f"taxonomy XML exceeds {MAX_TOTAL_XML_BYTES} aggregate bytes",
                )
            parsed[info.filename] = (xml_bytes, root)

        roots = {name: value[1] for name, value in parsed.items()}
        ids = {name: _xml_id_index(root) for name, root in roots.items()}
        package_members = set(names)
        info_by_name = {info.filename: info for info in infos}

        for member_name in sorted(parsed):
            xml_bytes, root = parsed[member_name]
            info = info_by_name[member_name]
            embedded_types = _embedded_linkbase_types(root)
            inventory.resources.append(
                TaxonomyResource(
                    member_name=member_name,
                    byte_count=info.file_size,
                    compressed_byte_count=info.compress_size,
                    sha256=hashlib.sha256(xml_bytes).hexdigest(),
                    root_qname=str(root.tag),
                    resource_types=_resource_types(root),
                    embedded_linkbase_types=embedded_types,
                )
            )
            inventory.references.extend(
                _references(
                    source_member=member_name,
                    root=root,
                    package_members=package_members,
                    roots=roots,
                    ids=ids,
                )
            )
            inventory.role_types.extend(_role_types(member_name, root))
            inventory.arcrole_types.extend(_arcrole_types(member_name, root))
            _linkbase_records(
                source_member=member_name,
                root=root,
                package_members=package_members,
                roots=roots,
                ids=ids,
                inventory=inventory,
            )

    inventory.resources.sort(key=lambda value: value.member_name)
    inventory.references.sort(
        key=lambda value: (
            value.source_member,
            value.reference_type,
            value.resolved_uri,
            value.fragment or "",
        )
    )
    inventory.role_types.sort(key=lambda value: (value.role_uri, value.source_member))
    inventory.arcrole_types.sort(
        key=lambda value: (value.arcrole_uri, value.source_member)
    )
    inventory.warnings = sorted(set(inventory.warnings))
    return inventory


def inventory_counts(inventory: TaxonomyPackageInventory) -> dict[str, object]:
    relationship_counts = Counter(
        relationship.linkbase_type for relationship in inventory.relationships
    )
    locator_status_counts = Counter(
        locator.reference.resolution_status for locator in inventory.locators
    )
    label_roles = Counter(
        resource.role
        for resource in inventory.linkbase_resources
        if resource.linkbase_type == "label"
    )
    label_languages = Counter(
        resource.language
        for resource in inventory.linkbase_resources
        if resource.linkbase_type == "label"
    )
    return {
        "resource_count": len(inventory.resources),
        "schema_count": sum(
            "schema" in resource.resource_types for resource in inventory.resources
        ),
        "standalone_linkbase_count": sum(
            "linkbase" in resource.resource_types for resource in inventory.resources
        ),
        "embedded_linkbase_document_count": sum(
            bool(resource.embedded_linkbase_types) and "schema" in resource.resource_types
            for resource in inventory.resources
        ),
        "link_counts": dict(sorted(inventory.link_counts.items())),
        "extended_link_count": len(inventory.extended_links),
        "relationship_counts": dict(sorted(relationship_counts.items())),
        "locator_count": len(inventory.locators),
        "locator_status_counts": dict(sorted(locator_status_counts.items())),
        "linkbase_resource_count": len(inventory.linkbase_resources),
        "label_role_counts": {
            str(key): count for key, count in sorted(label_roles.items(), key=lambda x: str(x[0]))
        },
        "label_language_counts": {
            str(key): count for key, count in sorted(label_languages.items(), key=lambda x: str(x[0]))
        },
        "role_type_count": len(inventory.role_types),
        "arcrole_type_count": len(inventory.arcrole_types),
    }
