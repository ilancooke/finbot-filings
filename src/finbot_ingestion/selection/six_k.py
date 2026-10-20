"""Conservative 6-K routing from bounded, document-specific descriptions."""

from collections import defaultdict
from copy import deepcopy
import re
import unicodedata

from bs4 import BeautifulSoup, Comment

from .policy import Decision, MAX_ROUTING_PARSE_BYTES, DOCUMENT_SUFFIXES, primary_exhibits
from finbot_ingestion.sec.filing_index import _link_name
from finbot_ingestion.sec.errors import SECDataError

SIX_K_FORMS = frozenset({"6-K", "6-K/A"})
# Match a reporting period in the same short block, never elsewhere in the filing.
_PERIOD = re.compile(
    r"\bQ[1-4]\s*(?:FY\s*)?20\d{2}\b|"
    r"\b(?:first|second|third|fourth|[1-4](?:st|nd|rd|th))[ -]quarter\b(?=.{0,80}\b20\d{2}\b)|"
    r"\b20\d{2}\s+(?:first|second|third|fourth|[1-4](?:st|nd|rd|th))[ -]quarter\b|"
    r"\b(?:quarter|three[ -]months?|six[ -]months?|nine[ -]months?|year|half[ -]year)(?:\s+period)?\s+ended\b(?=.{0,80}\b20\d{2}\b)|"
    r"\b(?:interim|annual|half[ -]year|full[ -]year|year[ -]end)\s+(?:financial\s+)?(?:report|results)\s+(?:for\s+)?(?:FY\s*)?20\d{2}\b|"
    r"\b(?:FY|fiscal\s+year|full[ -]year)\s*20\d{2}\b", re.I)
_RESULTS = re.compile(r"\b(?:earnings|financial|quarterly|annual|interim|full[ -]year|half[ -]year|year[ -]end)\s+(?:results|release)\b|\bresults\s+of\s+operations\b|\b(?:quarter|Q[1-4])\s*(?:20\d{2}\s*)?(?:financial\s+)?results\b", re.I)
_REPORT = re.compile(r"\b(?:(?:first|second|third|fourth|[1-4](?:st|nd|rd|th))[ -]quarter|quarterly|interim|half[ -]year|annual|full[ -]year|year[ -]end)\s+(?:financial\s+)?report\b", re.I)
_STATEMENTS = re.compile(r"\b(?:consolidated\s+(?:interim\s+)?financial\s+statements|financial\s+statements|statements?\s+of\s+(?:earnings|income|operations)|statutory\s+interim\s+report|(?:audited|unaudited)\s+(?:consolidated\s+)?financial\s+results)\b", re.I)
_SUPPLEMENT = re.compile(r"\b(?:financial|statistical|earnings)\s+(?:data\s+)?supplement\b|\bsupplement(?:al|ary)\s+(?:financial|earnings|statistical|sales)\b|\b(?:data\s*sheet\s+containing\s+)?operating\s+metrics\b", re.I)
_RELEASE = re.compile(r"\b(?:press|news|earnings)\s+release\b", re.I)
_ACTUAL = re.compile(r"\b(?:reports?|reported|announces?|announced|publishes|published|releases?|released|furnishes|furnished)\b", re.I)
_MEASURES = re.compile(r"\b(?:net\s+(?:sales|income|profit|loss)|revenue|earnings\s+per\s+share|EPS)\b", re.I)
_DECK = re.compile(r"\b(?:presentation|slides?|deck)\b", re.I)
_OTHER = re.compile(r"\b(?:transcript|advertisement|newspaper|AGM|annual\s+general\s+meeting|board\s+(?:of\s+directors\s+)?meeting|meeting\s+resolutions|notice\s+of\s+meeting|pro[ -]forma|acquired\s+(?:company|business)|acquisition\s+agreement|offering\s+(?:circular|memorandum)|prospectus)\b", re.I)
_FUTURE = re.compile(r"\b(?:will|plans?\s+to|expects?\s+to|to)\s+(?:announce|report|release|publish)\b|\b(?:date|schedule|invitation)\b.{0,60}\b(?:results|earnings|report)\b", re.I)
_APPROVAL = re.compile(r"\b(?:approved|approval|adopted|adoption)\b", re.I)
_REFERENCE = re.compile(r"\b(?:refer\s+to|see\s+(?:our|the)|incorporated\s+by\s+reference|registration\s+statement|risk\s+factors|forward[ -]looking)\b", re.I)
_NUMBER = re.compile(r"^(?:(?:exhibit|item|EX)[\s.-]*)?(99\.\d+)\b[\s.:–—-]+(.+)$", re.I)
_REFERENCES = re.compile(r"\b(?:exhibit|item)\s+(99\.\d+)\b", re.I)
MAX_BLOCK_CHARS = 1500
MAX_RELEASE_INSPECTIONS = 2
_TAGS = ["p", "div", "li", "td", "h1", "h2", "h3", "h4", "h5", "h6"]


def _soup(content):
    soup = BeautifulSoup(content, "html.parser")
    for node in soup.find_all(["head", "script", "style"]):
        node.decompose()
    for node in soup.find_all(string=lambda value: isinstance(value, Comment)):
        node.extract()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return soup


def _continued_titles(units):
    """Recover quoted titles split into adjacent absolutely positioned lines.

    This is deliberately not paragraph/window concatenation. The opening quote,
    closing quote, shared container and layout-line structure bound one title.
    A cloned node preserves link checks across every continuation line.
    """
    for node in units:
        text = normalized(node.get_text(" ", strip=True))
        match = re.search(r'\bregarding\s+([“"])([^”"]*)$', text, re.I)
        if not match or not _layout_line(node):
            continue
        closing = "”" if match[1] == "“" else '"'
        parts, sibling = [node], node
        for _ in range(2):
            sibling = sibling.find_next_sibling()
            if sibling is None or not _layout_line(sibling):
                break
            continuation = normalized(sibling.get_text(" ", strip=True))
            if (not continuation or _REFERENCES.search(continuation)
                    or continuation.upper() in {"EXHIBITS", "SIGNATURES"}
                    or "“" in continuation):
                break
            parts.append(sibling)
            joined = normalized(" ".join(part.get_text(" ", strip=True) for part in parts))
            if len(joined) > MAX_BLOCK_CHARS:
                break
            if closing in continuation:
                if not re.fullmatch(r'[^”"]*[”"]\.?', continuation):
                    break
                wrapper = BeautifulSoup("<div></div>", "html.parser").div
                for part in parts:
                    wrapper.append(deepcopy(part))
                yield joined, wrapper
                break


def _layout_line(node):
    return (node.name == "div" and not node.find(_TAGS)
            and re.search(r"\bposition\s*:\s*absolute\b", node.get("style", ""), re.I))


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).split())


def results_document(text):
    """Affirmative result/report description, excluding notices and references."""
    text = normalized(text)
    if not text or len(text) > MAX_BLOCK_CHARS or _REFERENCE.search(text) or _OTHER.search(text) or _APPROVAL.search(text):
        return False
    if _FUTURE.search(text):
        return False
    if not _PERIOD.search(text):
        return False
    return bool(_REPORT.search(text) or _STATEMENTS.search(text) or _RESULTS.search(text)
                or (_ACTUAL.search(text) and _MEASURES.search(text)))


def role(text, *, results_context=False):
    text = normalized(text)
    if len(text) > MAX_BLOCK_CHARS:
        return None
    affirmative = results_document(text)
    if _DECK.search(text):
        # Earnings presentation labels do not admit a filing. A combined release
        # and deck remains ambiguous, just as for earnings 8-Ks.
        return "UNRESOLVED" if _RELEASE.search(text) else "EXCLUDE"
    if _OTHER.search(text) or _FUTURE.search(text) or _REFERENCE.search(text):
        return "EXCLUDE"
    if _STATEMENTS.search(text) or _SUPPLEMENT.search(text):
        return "financial_supplement" if results_context or affirmative else None
    if affirmative:
        return "earnings_release" if _RELEASE.search(text) or not _REPORT.search(text) else "financial_report"
    if results_context and _RELEASE.search(text) and (_RESULTS.search(text) or re.search(r"\b(?:its|our|the)\s+results\b", text, re.I)):
        return "earnings_release"
    return None


def _label_decision(texts, *, results_context=False):
    roles = {value for text in texts if (value := role(text, results_context=results_context)) is not None}
    selected = roles & {"earnings_release", "financial_report", "financial_supplement"}
    if "UNRESOLVED" in roles or (selected and "EXCLUDE" in roles):
        return Decision("UNRESOLVED", "unresolved_exhibit_role")
    if selected:
        reason = "financial_supplement" if "financial_supplement" in selected else sorted(selected)[0]
        return Decision("SELECT", reason)
    if "EXCLUDE" in roles:
        return Decision("EXCLUDE", "unrelated_exhibit")
    return Decision("UNRESOLVED", "unresolved_exhibit_role")


def _evidence(filing, package, content):
    labels = {d.filename: [d.description or ""] for d in package.documents}
    blocks = []
    inspected = content is not None and len(content) <= MAX_ROUTING_PARSE_BYTES
    if inspected:
        soup = _soup(content)
        by_number = defaultdict(list)
        for document in package.documents:
            by_number[(document.document_type or "").upper().removeprefix("EX-")].append(document.filename)
        # Exhibit tables need the complete numbered row, not separated cells.
        exhibit_rows = [row for row in soup.find_all("tr") if _NUMBER.match(normalized(row.get_text(" ", strip=True)))]
        row_ids = {id(row) for row in exhibit_rows}
        units = [node for node in soup.find_all(_TAGS) if not node.find(_TAGS)
                 and not any(id(parent) in row_ids for parent in node.parents)] + exhibit_rows
        raw_blocks = [(node.get_text(" ", strip=True), node) for node in units] if units else [(soup.get_text(" "), soup)]
        raw_blocks.extend(_continued_titles(units))
        for raw, node in raw_blocks:
            for part in raw.splitlines():
                text = normalized(part)
                if not text or len(text) > MAX_BLOCK_CHARS:
                    continue
                numbered = _NUMBER.match(text)
                numbers = ([numbered.group(1)] if numbered else list(set(_REFERENCES.findall(text))))
                # Linked labels must not bypass the attachment's conflicts.
                # Results stated outside links can still qualify the primary.
                attachment_links = [link for link in node.find_all("a", href=True)
                                    if not link["href"].startswith("#")]
                if not numbered and not numbers:
                    if attachment_links:
                        linked_nodes = {id(value) for link in attachment_links for value in link.find_all(string=True)}
                        unlinked = " ".join(str(value) for value in node.find_all(string=True)
                                            if id(value) not in linked_nodes)
                        blocks.extend(value for part in unlinked.splitlines()
                                      if (value := normalized(part)) and len(value) <= MAX_BLOCK_CHARS)
                    else:
                        blocks.append(text)
                if len(numbers) != 1 or len(by_number[numbers[0]]) != 1:
                    continue
                # Do not attach multi-exhibit rows/paragraphs to the first child.
                if len(set(re.findall(r"\b99\.\d+\b", text))) != 1:
                    continue
                filename = by_number[numbers[0]][0]
                try:
                    links = {_link_name(link["href"], filing) for link in attachment_links}
                except SECDataError:
                    continue
                if links and links != {filename}:
                    continue
                labels[filename].append(numbered.group(2) if numbered else text)
        references = primary_exhibits(filing, content)
        for filename, texts in references.descriptions.items():
            if filename in labels:
                labels[filename].extend(texts)
    return labels, blocks, inspected


def _inspection_candidates(filing, package, labels):
    counts = defaultdict(int)
    for document in package.documents:
        counts[(document.document_type or "").upper()] += 1
    candidates = []
    for document in package.documents:
        kind = (document.document_type or "").upper()
        texts = [normalized(text) for text in labels[document.filename]]
        if (document.filename == package.primary_document_name
                or not re.fullmatch(r"EX-99\.\d+", kind) or counts[kind] != 1
                or not document.filename.lower().endswith((".htm", ".html", ".txt"))
                or not any(_RELEASE.search(text) for text in texts)
                or any(role(text) is not None or _APPROVAL.search(text) for text in texts)):
            continue
        try:
            if _link_name(document.sec_url, filing) != document.filename:
                continue
        except SECDataError:
            continue
        candidates.append(document)
    return tuple(sorted(candidates, key=lambda d: d.filename)[:MAX_RELEASE_INSPECTIONS])


def inspection_candidates(filing, package, content=None):
    """Only uniquely identified generic releases can trigger extra SEC reads."""
    labels, _, _ = _evidence(filing, package, content)
    return _inspection_candidates(filing, package, labels)


def _opening_results(content):
    if len(content) > MAX_ROUTING_PARSE_BYTES:
        return None
    soup = _soup(content)
    units = [node for node in soup.find_all(_TAGS) if not node.find(_TAGS)]
    texts = [normalized(node.get_text(" ", strip=True)) for node in units] if units else [normalized(soup.get_text(" "))]
    # Ignore empty/logo-only nodes and the SEC exhibit marker, not substantive
    # text. Never scan later boilerplate for historical financial references.
    texts = [text for text in texts if text and not re.fullmatch(r"(?:exhibit|EX-?)\s*99\.\d+", text, re.I)][:4]
    if not texts:
        return None
    headline = texts[0]
    if (len(headline) > MAX_BLOCK_CHARS or _DECK.search(headline) or _OTHER.search(headline)
            or _FUTURE.search(headline) or _REFERENCE.search(headline) or _APPROVAL.search(headline)):
        return None
    if not (_RESULTS.search(headline) or _REPORT.search(headline)
            or (_ACTUAL.search(headline) and _MEASURES.search(headline))):
        return None
    return next((text for text in texts if results_document(text)
                 and role(text) not in {"EXCLUDE", "UNRESOLVED"}), None)


def select_six_k(filing, package, content=None, *, attachment_contents=None):
    """Return minimal admission/document decisions; retain no evidence record."""
    labels, blocks, inspected = _evidence(filing, package, content)
    for document in _inspection_candidates(filing, package, labels):
        body = (attachment_contents or {}).get(document.filename)
        if body is not None and (evidence := _opening_results(body)):
            labels[document.filename].append(evidence)
    context = any(results_document(text) and role(text) not in {"EXCLUDE", "UNRESOLVED"} for text in blocks)
    candidates = [d for d in package.documents if "." + d.filename.lower().rsplit(".", 1)[-1] in DOCUMENT_SUFFIXES
                  and (d.filename == package.primary_document_name
                       or re.fullmatch(r"EX-99(?:\.\d+)?", (d.document_type or "").upper()))
                  and d.filename.lower() != filing.accession_number + ".txt"]
    affirmative_candidates = [d for d in candidates
                              if any(results_document(text) and role(text) not in {"EXCLUDE", "UNRESOLVED"}
                                     for text in labels[d.filename])]
    # Resolve all labels before an attachment can establish package context.
    # Generic supplements must not bootstrap their own results qualification.
    affirmative = context or any(_label_decision(labels[d.filename]).selected for d in affirmative_candidates)
    if not affirmative:
        known_other = any(_OTHER.search(text) or _FUTURE.search(text)
                          for text in [*blocks, *(text for d in candidates for text in labels[d.filename])])
        admission = (Decision("EXCLUDE", "non_earnings_6_k") if inspected and known_other and not affirmative_candidates
                     else Decision("UNRESOLVED", "unresolved_6_k"))
        return admission, {}
    decisions = {}
    for document in package.documents:
        name = document.filename
        suffix = name.lower().rsplit(".", 1)[-1]
        if name == package.primary_document_name:
            decisions[name] = Decision("SELECT", "primary_report")
        elif name.lower() == filing.accession_number + ".txt":
            decisions[name] = Decision("EXCLUDE", "submission_bundle")
        elif "." + suffix not in DOCUMENT_SUFFIXES or not re.fullmatch(r"EX-99(?:\.\d+)?", (document.document_type or "").upper()):
            decisions[name] = Decision("EXCLUDE", "unrelated_exhibit")
        else:
            decisions[name] = _label_decision(labels[name], results_context=True)
    return Decision("SELECT", "earnings_6_k"), decisions
