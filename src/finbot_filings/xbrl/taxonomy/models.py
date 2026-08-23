"""Internal records for a fully inspectable filing taxonomy inventory."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TaxonomyResource:
    member_name: str
    byte_count: int
    compressed_byte_count: int
    sha256: str
    root_qname: str | None
    resource_types: tuple[str, ...]
    embedded_linkbase_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaxonomyReference:
    reference_type: str
    source_member: str
    raw_href: str
    resolved_uri: str
    namespace: str | None
    fragment: str | None
    target_member: str | None
    resolution_status: str
    concept_qname: str | None = None
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RoleType:
    source_member: str
    source_ordinal: int
    role_uri: str
    definition: str | None
    used_on: tuple[str, ...]
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ArcroleType:
    source_member: str
    arcrole_uri: str
    cycles_allowed: str | None
    definition: str | None
    used_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LinkbaseLocator:
    linkbase_type: str
    source_member: str
    link_index: int
    source_ordinal: int
    label: str
    role: str | None
    reference: TaxonomyReference
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class LinkbaseResource:
    linkbase_type: str
    source_member: str
    link_index: int
    source_ordinal: int
    label: str
    role: str | None
    language: str | None
    value: str
    content_xml: str
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ExtendedLink:
    linkbase_type: str
    source_member: str
    link_index: int
    link_role: str | None
    attributes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class LinkbaseRelationship:
    linkbase_type: str
    source_member: str
    link_index: int
    source_ordinal: int
    link_role: str | None
    arcrole: str | None
    from_label: str
    to_label: str
    order: str | None
    priority: str | None
    use: str | None
    weight: str | None
    preferred_label: str | None
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True)
class TaxonomyPackageInventory:
    resources: list[TaxonomyResource] = field(default_factory=list)
    references: list[TaxonomyReference] = field(default_factory=list)
    role_types: list[RoleType] = field(default_factory=list)
    arcrole_types: list[ArcroleType] = field(default_factory=list)
    extended_links: list[ExtendedLink] = field(default_factory=list)
    locators: list[LinkbaseLocator] = field(default_factory=list)
    linkbase_resources: list[LinkbaseResource] = field(default_factory=list)
    relationships: list[LinkbaseRelationship] = field(default_factory=list)
    link_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def unresolved_local_references(self) -> list[TaxonomyReference]:
        return [
            reference
            for reference in self.references
            if reference.resolution_status
            in {
                "missing_local_member",
                "missing_fragment",
                "missing_href",
                "unsafe_reference",
                "unsupported_uri_scheme",
            }
        ]

    @property
    def external_dependencies(self) -> list[TaxonomyReference]:
        return [
            reference
            for reference in self.references
            if reference.resolution_status == "external_dependency"
        ]
