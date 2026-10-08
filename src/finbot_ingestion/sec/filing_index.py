"""Pure filing-package enumeration; no XBRL prerequisites or interpretation."""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from finbot_ingestion.domain import Artifact, Filing
from finbot_ingestion.domain.identity import validate_filename
from .errors import SECDataError, SECIncompletePackageError
from .urls import accession_directory_url, document_url


@dataclass(frozen=True)
class PackageDocument:
    filename: str
    sec_url: str
    document_type: str | None


@dataclass(frozen=True)
class FilingIndex:
    documents: tuple[PackageDocument, ...]
    primary_document_name: str

    def artifacts(self, filing: Filing, *, discovered_at: datetime) -> tuple[Artifact, ...]:
        return tuple(Artifact(
            accession_number=filing.accession_number, company_cik=filing.company_cik,
            ticker=filing.ticker, form_type=filing.form_type,
            filename=d.filename, sec_url=d.sec_url, document_type=d.document_type,
            discovered_at=discovered_at,
        ) for d in self.documents)


def _filename(value):
    try:
        return validate_filename(value)
    except (ValueError, TypeError) as exc:
        raise SECDataError("unsafe or invalid package filename") from exc


def _link_name(href, filing):
    url = urlsplit(urljoin(filing.filing_index_url, href))
    if url.scheme != 'https' or url.hostname != 'www.sec.gov' or url.port not in (None, 443) or url.username or url.password:
        raise SECDataError("package link must remain on SEC archives")
    if url.path in ('/ixviewer/doc/action', '/ixviewer/ix.html', '/ix', '/ix.html'):
        values = parse_qs(url.query).get('doc', [])
        if len(values) != 1:
            raise SECDataError("invalid inline viewer document link")
        return _link_name(values[0], filing)
    prefix = urlsplit(accession_directory_url(filing.company_cik, filing.accession_number)).path + '/'
    if not url.path.startswith(prefix) or url.query:
        raise SECDataError("document link is outside expected accession")
    return _filename(unquote(url.path[len(prefix):]))


def parse_filing_index(filing: Filing, html: bytes, directory_payload) -> FilingIndex:
    """Require consistent HTML document tables and directory snapshot.

    All directory files are acquired except explicitly recognized index infrastructure.
    Completeness describes this snapshot, not a guarantee against later SEC additions.
    """
    if not isinstance(directory_payload, Mapping):
        raise SECDataError("directory must be an object")
    directory = directory_payload.get('directory')
    if not isinstance(directory, Mapping) or not isinstance(directory.get('item'), list):
        raise SECIncompletePackageError("missing directory items")
    expected_path = urlsplit(accession_directory_url(filing.company_cik, filing.accession_number)).path
    if 'name' in directory and directory['name'] != expected_path:
        raise SECDataError("directory identity does not match filing")
    names = set()
    for item in directory['item']:
        if not isinstance(item, Mapping):
            raise SECDataError("invalid directory item")
        names.add(_filename(item.get('name')))
    soup = BeautifulSoup(html, 'html.parser')
    types = {}
    table_count = 0
    for table in soup.find_all('table'):
        headers = [h.get_text(' ', strip=True).lower() for h in table.find_all('th')]
        if 'document' not in headers or 'type' not in headers:
            continue
        table_count += 1
        for row in table.find_all('tr'):
            cells = row.find_all('td', recursive=False)
            if not cells:
                continue
            if len(cells) != len(headers):
                raise SECIncompletePackageError("incomplete document table row")
            cell = cells[headers.index('document')]
            links = cell.find_all('a', href=True)
            if not links:
                raise SECIncompletePackageError("document row has no link")
            document_type = cells[headers.index('type')].get_text(' ', strip=True) or None
            for link in links:
                name = _link_name(link['href'], filing)
                if name in types and types[name] != document_type:
                    raise SECDataError("conflicting document types")
                types[name] = document_type
    if not table_count or not types:
        raise SECIncompletePackageError("missing document tables")
    if not set(types) <= names:
        raise SECIncompletePackageError("document tables and directory are inconsistent")
    primary = filing.primary_document_name
    if primary is None:
        candidates = [name for name, kind in types.items() if kind == filing.form_type]
        if len(candidates) != 1:
            raise SECIncompletePackageError("primary document cannot be resolved")
        primary = candidates[0]
    if primary not in names or primary not in types:
        raise SECIncompletePackageError("primary document not yet listed")
    excluded = {'index.json', 'index.xml', 'index.html',
                f'{filing.accession_number}-index.html', f'{filing.accession_number}-index.htm',
                f'{filing.accession_number}-index-headers.html'}
    if excluded & set(types):
        raise SECDataError("document table references index infrastructure")
    documents = tuple(PackageDocument(name, document_url(filing.company_cik, filing.accession_number, name), types.get(name))
                      for name in sorted(names - excluded))
    return FilingIndex(documents, primary)
