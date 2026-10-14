"""Small filing/document filters shared by discovery and restart recovery."""
from dataclasses import dataclass
from datetime import datetime
import os
import re
from pathlib import PurePosixPath

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from finbot_ingestion.config import ConfigurationError
from finbot_ingestion.domain.validation import utc_datetime

POLICY_VERSION = "earnings-acquisition-v3"
# Filing admission is unchanged. Only already affirmative work may upgrade;
# blocked work still needs an explicit operator operation.
POLICY_UPGRADES = frozenset({("earnings-acquisition-v1", "earnings-acquisition-v2"),
    ("earnings-acquisition-v1", POLICY_VERSION), ("earnings-acquisition-v2", POLICY_VERSION)})
DEFAULT_LIVE_START = "2026-10-06T04:00:00Z"
MAX_ROUTING_PARSE_BYTES = 4 * 1024 * 1024
PERIODIC_FORMS = frozenset({"10-Q", "10-Q/A", "10-K", "10-K/A"})
DOCUMENT_SUFFIXES = frozenset({".htm", ".html", ".txt", ".pdf"})
GRAPHIC_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".tif", ".tiff"})
_ITEM = re.compile(r"\bItem\s+([1-9]\.[0-9]{2})\b", re.I)
_DECK = re.compile(r"\b(presentation|slide(?:s|show)?|deck)\b", re.I)
_RELEASE = re.compile(r"\b(earnings|financial|quarter(?:ly)?|annual|operating)\b.*\b(release|report|announcement)\b|\b(release|announcement)\b.*\b(earnings|financial|quarter|annual|results)\b", re.I)
_SUPPLEMENT = re.compile(r"\b(financial|statistical|earnings)\s+(?:data\s+)?supplement\b|\bsupplement(?:al|ary)?\s+(?:financial|earnings|statistical)\b", re.I)
_GENERIC_RELEASE = re.compile(r"\b(?:press|news|earnings)\s+release\b", re.I)
_QUARTERLY_SUPPLEMENT = re.compile(r"\bquarterly\s+supplement\b", re.I)
_RESULTS_DOCUMENT = re.compile(r"\bsupplement(?:al|ary)\s+sales\s+data\b|\bfinancial\s+statements\b|\bstatements?\s+of\s+(?:earnings|income|operations)\b", re.I)
_EXHIBIT = re.compile(r"\b(?:Exhibits?|Ex\.?)[\s-]*(99\.\d+\b(?:\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)99\.\d+\b)*)", re.I)
_ITEM_HEADING = re.compile(r"^Item\s+([1-9]\.[0-9]{2})\b", re.I)
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "b", "strong", "p", "div", "td")


@dataclass(frozen=True, slots=True)
class ExhibitEvidence:
    """Attachment-specific descriptions and exact Item 2.02 references."""
    descriptions: dict[str, tuple[str, ...]]
    item_2_02_numbers: frozenset[str] = frozenset()


def _normalized(text):
    return " ".join(text.split())


def _item_2_02_numbers(soup, content):
    sections = []
    current = []
    active = False
    if not soup.find():
        nodes = content.decode("utf-8", errors="replace").splitlines()
        headings = {}
    else:
        # Keep the innermost structural heading, rather than a layout container
        # beginning with a heading and containing subsequent sections as well.
        headings = {}
        containers = set()
        for node in soup.find_all(_HEADING_TAGS):
            text = _normalized(node.get_text(" ", strip=True))
            match = _ITEM_HEADING.match(text) if len(text) <= 300 else None
            if match:
                headings[id(node)] = match.group(1)
                containers.update(id(parent) for parent in node.parents)
        headings = {key: value for key, value in headings.items() if key not in containers}
        nodes = soup.descendants
    for node in nodes:
        if isinstance(node, Comment):
            continue
        if isinstance(node, Tag):
            item = headings.get(id(node))
        elif isinstance(node, NavigableString):
            if node.find_parent(["script", "style", "head"]):
                continue
            item = None
        else:  # Plain-text reports use short, line-start Item headings.
            match = _ITEM_HEADING.match(_normalized(node)) if len(node) <= 300 else None
            item = match.group(1) if match else None
        if item is not None:
            if active:
                sections.append(" ".join(current))
            current = []
            active = item == "2.02"
        if active and isinstance(node, str):
            current.append(str(node))
    if active:
        sections.append(" ".join(current))
    return frozenset(number for section in sections for match in _EXHIBIT.findall(section)
                     for number in re.findall(r"99\.\d+", match))


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: str
    reason: str

    @property
    def selected(self):
        return self.outcome == "SELECT"


@dataclass(frozen=True, slots=True)
class AcquisitionPolicy:
    live_start_at: datetime

    def __post_init__(self):
        object.__setattr__(self, "live_start_at", utc_datetime(self.live_start_at, "live_start_at"))

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        try:
            raw = values.get("LIVE_INGESTION_START_AT", DEFAULT_LIVE_START)
            if not isinstance(raw, str) or "T" not in raw:
                raise ValueError("expected a timezone-aware ISO timestamp")
            return cls(datetime.fromisoformat(raw))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("LIVE_INGESTION_START_AT must be a timezone-aware ISO timestamp") from exc

    def filing(self, filing, *, items=None, item_status="absent"):
        if filing.filed_at < self.live_start_at:
            return Decision("EXCLUDE", "before_live_cutoff")
        if filing.form_type in PERIODIC_FORMS:
            return Decision("SELECT", "periodic_report")
        if filing.form_type in {"6-K", "6-K/A"}:
            return Decision("UNRESOLVED", "unresolved_6_k")
        if item_status == "known":
            return (Decision("SELECT", "earnings_item_2_02") if "2.02" in (items or ())
                    else Decision("EXCLUDE", "non_earnings_8_k"))
        return Decision("UNRESOLVED", "unresolved_items")


def primary_items(content):
    """Only structural Item headings establish primary-report evidence."""
    if len(content) > MAX_ROUTING_PARSE_BYTES:
        return ()  # Keep oversized/unsupported routing visible as unresolved.
    soup = BeautifulSoup(content, "html.parser")
    items = set()
    if not soup.find():
        for line in content.decode("utf-8", errors="replace").splitlines():
            if len(line) <= 300 and re.match(r"^\s*Item\s+[1-9]\.[0-9]{2}\b", line, re.I):
                items.update(_ITEM.findall(line))
        return tuple(sorted(items))
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "b", "strong", "p", "div", "td"]):
        # Avoid treating an entire document/container or a prose cross-reference
        # as a heading. SEC reports often split headings into nested spans.
        text = node.get_text(" ", strip=True)
        if len(text) <= 300 and re.match(r"^Item\s+[1-9]\.[0-9]{2}\b", text, re.I):
            items.update(_ITEM.findall(text))
    return tuple(sorted(items))


def primary_exhibits(filing, content):
    """Collect safe per-attachment labels and bounded Item 2.02 references."""
    if len(content) > MAX_ROUTING_PARSE_BYTES:
        return ExhibitEvidence({})
    from finbot_ingestion.sec.filing_index import _link_name
    from finbot_ingestion.sec.errors import SECDataError
    soup = BeautifulSoup(content, "html.parser")
    descriptions = {}
    for link in soup.find_all("a", href=True):
        try:
            name = _link_name(link["href"], filing)
        except SECDataError:
            continue
        text = _normalized(link.get_text(" ", strip=True))
        if text and len(text) <= 4096:
            descriptions.setdefault(name, []).append(text)
    for row in soup.find_all("tr"):
        cells = [cell for cell in row.find_all("td", recursive=False)
                 if cell.get_text(" ", strip=True)]
        if len(cells) < 2 or not re.fullmatch(r"(?:(?:EX-|Exhibit\s+))?99\.\d+\.?", _normalized(cells[0].get_text(" ", strip=True)), re.I):
            continue
        if any(cell.find("table") for cell in cells):
            continue  # Layout rows must not assign every exhibit's role to every link.
        text = _normalized(row.get_text(" ", strip=True))
        if len(text) > 4096:
            continue
        names = set()
        for link in row.find_all("a", href=True):
            try:
                name = _link_name(link["href"], filing)
            except SECDataError:
                continue  # External references are never fetched or selected.
            names.add(name)
        if len(names) == 1:
            descriptions.setdefault(names.pop(), []).append(text)
    return ExhibitEvidence({name: tuple(dict.fromkeys(values)) for name, values in descriptions.items()},
                           _item_2_02_numbers(soup, content))


def document_decision(filing, package, document, *, references=None):
    name = document.filename
    if name.lower() == f"{filing.accession_number}.txt":
        return Decision("EXCLUDE", "submission_bundle")
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in GRAPHIC_SUFFIXES:
        return Decision("EXCLUDE", "standalone_graphic")
    if name == package.primary_document_name:
        return Decision("SELECT", "primary_report")
    if filing.form_type in PERIODIC_FORMS:
        return Decision("EXCLUDE", "periodic_attachment")
    if suffix not in DOCUMENT_SUFFIXES:
        return Decision("EXCLUDE", "auxiliary_file")
    kind = (document.document_type or "").upper()
    if not re.fullmatch(r"EX-\d+(?:\.\d+)?", kind):
        return Decision("EXCLUDE", "unrelated_exhibit")
    texts = [document.description or "", *(references.descriptions.get(name, ()) if references else ())]
    in_results_item = bool(references and kind.removeprefix("EX-") in references.item_2_02_numbers)
    roles = set()
    for text in texts:
        text = _normalized(text)
        if _DECK.search(text):
            roles.add("presentation")
        if (_SUPPLEMENT.search(text) or _RESULTS_DOCUMENT.search(text)
                or (in_results_item and _QUARTERLY_SUPPLEMENT.search(text))):
            roles.add("financial_supplement")
        if _RELEASE.search(text) and (not _DECK.search(text) or re.search(r"\brelease\b", text, re.I)):
            roles.add("earnings_release")
        if in_results_item and _GENERIC_RELEASE.search(text):
            roles.add("earnings_release")
    if "presentation" in roles:
        if roles - {"presentation"}:
            return Decision("UNRESOLVED", "combined_release_and_presentation")
        return Decision("EXCLUDE", "presentation")
    if "financial_supplement" in roles:
        return Decision("SELECT", "financial_supplement")
    if "earnings_release" in roles:
        return Decision("SELECT", "earnings_release")
    if not re.fullmatch(r"EX-99(?:\.\d+)?", kind):
        return Decision("EXCLUDE", "unrelated_exhibit")
    return Decision("UNRESOLVED", "unresolved_exhibit_role")


def image_only_html(content):
    """A coverage diagnostic only; never fetch linked images or transform bytes."""
    if len(content) > MAX_ROUTING_PARSE_BYTES or b"<img" not in content.lower():
        return False
    soup = BeautifulSoup(content, "html.parser")
    for node in soup.find_all(["head", "script", "style"]):
        node.decompose()
    return len(soup.get_text(" ", strip=True)) < 80
