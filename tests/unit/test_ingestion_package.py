from dataclasses import replace
from datetime import datetime, timezone
import pytest

from finbot_ingestion.domain import Filing
from finbot_ingestion.sec.filing_index import parse_filing_index
from finbot_ingestion.sec.errors import SECDataError, SECIncompletePackageError
from finbot_ingestion.sec.urls import filing_index_url

TIME = datetime(2026,10,8,tzinfo=timezone.utc)
ACCESSION = '0000320193-26-000001'

@pytest.fixture
def target():
    return Filing(accession_number=ACCESSION,company_cik='320193',ticker='AAPL',form_type='8-K',filed_at=TIME,discovered_at=TIME,filing_index_url=filing_index_url('320193',ACCESSION),primary_document_name='Primary.htm')

def html(rows):
    return ('<table><tr><th>Seq</th><th>Document</th><th>Type</th></tr>'+''.join(f'<tr><td>1</td><td><a href="{name}">{name}</a></td><td>{kind}</td></tr>' for name,kind in rows)+'</table>').encode()

def directory(names):
    return {'directory':{'item':[{'name':n} for n in names]}}

def test_all_documents_and_extras_preserve_names_types_identity(target):
    rows=[('Primary.htm','8-K'),('slides.PDF','EX-99.2'),('release.htm','EX-99.1'),('slides.PDF','EX-99.2')]
    names=['Primary.htm','slides.PDF','release.htm','image.jpg','raw.xml','index.json',f'{ACCESSION}-index.html']
    result=parse_filing_index(target,html(rows),directory(names))
    assert {d.filename for d in result.documents}==set(names[:5])
    assert {d.filename:d.document_type for d in result.documents}['slides.PDF']=='EX-99.2'
    assert len(result.artifacts(target,discovered_at=TIME))==5
    assert result.artifacts(target,discovered_at=TIME)==result.artifacts(target,discovered_at=TIME)

def test_missing_primary_name_resolved_without_xbrl(target):
    result=parse_filing_index(replace(target,primary_document_name=None),html([('Primary.htm','8-K')]),directory(['Primary.htm']))
    assert result.primary_document_name=='Primary.htm'

@pytest.mark.parametrize('name',['../bad','folder/file.pdf','%2fsecret','x\\bad','\x00bad'])
def test_unsafe_directory_names_fail(target,name):
    with pytest.raises(SECDataError):
        parse_filing_index(target,html([('Primary.htm','8-K')]),directory(['Primary.htm',name]))

@pytest.mark.parametrize('href',['https://evil.test/file.pdf','../other.pdf','%2e%2e%2fother.pdf'])
def test_bad_document_links_fail(target,href):
    with pytest.raises(SECDataError):
        parse_filing_index(target,html([(href,'8-K')]),directory(['Primary.htm']))

def test_conflicting_types_fail(target):
    with pytest.raises(SECDataError,match='conflicting'):
        parse_filing_index(target,html([('Primary.htm','8-K'),('Primary.htm','EX-99')]),directory(['Primary.htm']))

@pytest.mark.parametrize('body,names',[(b'<html>pending</html>',['Primary.htm']),(html([('Primary.htm','8-K'),('late.pdf','EX-99')]),['Primary.htm']),(html([('other.htm','8-K')]),['other.htm'])])
def test_incomplete_metadata_never_returns_success(target,body,names):
    with pytest.raises(SECIncompletePackageError):
        parse_filing_index(target,body,directory(names))

def test_inline_viewer_resolves_original_document(target):
    path='/Archives/edgar/data/320193/000032019326000001/Primary.htm'
    result=parse_filing_index(target,html([('/ix?doc='+path,'8-K')]),directory(['Primary.htm']))
    assert result.documents[0].filename=='Primary.htm'

def test_many_exhibits_have_no_count_cutoff(target):
    rows=[('Primary.htm','8-K')]+[(f'exhibit{i}.pdf',f'EX-10.{i}') for i in range(150)]
    result=parse_filing_index(target,html(rows),directory([name for name,_ in rows]))
    assert len(result.documents)==151

@pytest.mark.parametrize('payload',[None,{}, {'directory':{'item':[None]}}, {'directory':{'item':[{'name':'Primary.htm'}],'name':'/wrong/accession'}}])
def test_invalid_directory_cannot_be_checkpointed(target,payload):
    with pytest.raises(SECDataError):
        parse_filing_index(target,html([('Primary.htm','8-K')]),payload)
