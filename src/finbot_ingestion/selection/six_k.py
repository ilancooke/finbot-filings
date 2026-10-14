"""Conservative 6-K routing from bounded, document-specific descriptions."""

from collections import defaultdict
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


def select_six_k(filing, package, content=None):
    """Return minimal admission/document decisions; retain no evidence record."""
    labels = {d.filename: [d.description or ""] for d in package.documents}
    blocks = []
    inspected = content is not None and len(content) <= MAX_ROUTING_PARSE_BYTES
    if inspected:
        soup = BeautifulSoup(content, "html.parser")
        for node in soup.find_all(["head", "script", "style"]):
            node.decompose()
        for node in soup.find_all(string=lambda value: isinstance(value, Comment)):
            node.extract()
        for br in soup.find_all("br"):
            br.replace_with("\n")
        by_number = defaultdict(list)
        for document in package.documents:
            by_number[(document.document_type or "").upper().removeprefix("EX-")].append(document.filename)
        tags = ["p", "div", "li", "td", "h1", "h2", "h3", "h4", "h5", "h6"]
        # Exhibit tables need the complete numbered row, not separated cells.
        exhibit_rows = [row for row in soup.find_all("tr") if _NUMBER.match(normalized(row.get_text(" ", strip=True)))]
        row_ids = {id(row) for row in exhibit_rows}
        units = [node for node in soup.find_all(tags) if not node.find(tags)
                 and not any(id(parent) in row_ids for parent in node.parents)] + exhibit_rows
        raw_blocks = [(node.get_text(" ", strip=True), node) for node in units] if units else [(soup.get_text(" "), soup)]
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
