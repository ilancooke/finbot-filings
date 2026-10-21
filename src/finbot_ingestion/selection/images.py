"""Supporting-images v1: preserve selected HTML, without classifying financial values.

Only references from an already-selected document authorize acquisition. Page
images and ambiguous content graphics are retained; clearly decorative resources
are omitted. Dimensions alone never exclude a material image. No image bytes,
external URLs, CSS resources, OCR or model calls are used to make this decision.
"""

from collections import Counter
from html.parser import HTMLParser
import codecs
import re

from finbot_ingestion.sec.errors import SECDataError, SECIncompletePackageError
from finbot_ingestion.sec.filing_index import _link_name
from .policy import Decision

RASTER_SUFFIXES = (".jpg", ".jpeg", ".gif", ".png")
MAX_SUPPORTING_IMAGES = 128
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_HTML_BYTES = 128 * 1024 * 1024  # cumulative temporary spool per package


class _Images(HTMLParser):
    """Collect bounded image attributes and text offsets without retaining a DOM."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images = []
        self.text_length = 0
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        attrs = {key: value or "" for key, value in attrs}
        if tag == "body" and self.hidden == ["head"]:
            self.hidden.clear()  # HTML implicitly closes an omitted </head>.
        if tag in {"head", "script", "style", "ix:header", "ix:hidden", "svg", "math", "iframe", "object", "template"}:
            self.hidden.append(tag)
        if tag == "img" and not self.hidden:
            if len(self.images) >= 4096:
                raise SECDataError("supporting image reference limit exceeded")
            self.images.append((attrs, self.text_length))

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()

    def handle_data(self, data):
        if not self.hidden:
            self.text_length += len(data.strip())


def _dimension(attrs, name):
    style = attrs.get("style", "")
    values = re.findall(rf"(?:^|;)\s*{name}\s*:\s*([0-9.]+)\s*(px|pt)?\s*(?:!important)?\s*(?=;|$)", style, re.I)
    value, unit = values[-1] if values else (attrs.get(name, ""), "px")
    try:
        number = float(value) * (4 / 3 if unit.lower() == "pt" else 1)
        return number if 0 <= number <= 100_000 else None
    except (ValueError, TypeError):
        return None


def supporting_images(filing, package, content):
    """Return filename decisions; required missing/unsupported local assets fail visibly.

    Parsing is incremental to avoid building a DOM for a large annual report.
    The caller bounds original HTML bytes and downloads through the normal client.
    """
    parser = _Images()
    match = re.search(br'charset\s*=\s*["\x27]?([a-zA-Z0-9_-]+)', content[:4096], re.I)
    encoding = "utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else match[1].decode("ascii") if match else "utf-8-sig"
    try:
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    except LookupError:
        raise SECDataError("unsupported HTML encoding for image discovery") from None
    for start in range(0, len(content), 64 * 1024):
        parser.feed(decoder.decode(content[start:start + 64 * 1024]))
    parser.feed(decoder.decode(b"", final=True))
    parser.close()
    repeats = Counter(attrs.get("src", "") for attrs, _ in parser.images)
    members = {document.filename for document in package.documents}
    decisions = {}
    for attrs, text_before in parser.images:
        src = attrs.get("src", "")
        if not src:
            raise SECDataError("selected HTML has an image without a source")
        try:
            name = _link_name(src, filing)
        except (SECDataError, ValueError):
            # Never follow external, data, query, traversal or cross-accession URLs.
            raise SECDataError("selected HTML has an unsafe supporting image reference") from None
        width, height = _dimension(attrs, "width"), _dimension(attrs, "height")
        label = " ".join((name, attrs.get("alt", ""), attrs.get("title", "")))
        page = width is not None and height is not None and min(width, height) >= 400
        rule = None
        if not page:
            if width is not None and height is not None and min(width, height) <= 2:
                rule = "decorative_separator"
            elif re.search(r"logo|\bicon\b|\bspacer\b|\bbanner\b", label, re.I):
                rule = "decorative_branding"
            elif (width is not None and height is not None and height <= 40
                  and width / max(height, 1) >= 3 and (text_before < 100 or repeats[src] > 1)):
                rule = "decorative_header"
        decision = Decision("EXCLUDE", rule) if rule else Decision("SELECT", "supporting_content_image")
        if decision.selected:
            if not name.lower().endswith(RASTER_SUFFIXES):
                raise SECDataError("selected HTML requires an unsupported supporting image format")
            if name not in members:
                raise SECIncompletePackageError("required supporting image is absent from the accession directory")
        # Any content use wins over a decorative use of a shared image.
        if name not in decisions or decision.selected:
            decisions[name] = decision
    if sum(item.selected for item in decisions.values()) > MAX_SUPPORTING_IMAGES:
        raise SECDataError("supporting image count limit exceeded")
    return decisions
