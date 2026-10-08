from finbot_ingestion.domain import Company
from finbot_ingestion.domain.identity import normalize_cik
from ..pagination import Page
from ..errors import RepositoryDataError
from .base import DynamoDBRepository
from .serialization import encode, record, restore

ENABLED_INDEX = "EnabledCompanies"


class DynamoDBCompanyRepository(DynamoDBRepository):
    def __init__(self, execution):
        super().__init__(execution, execution.config.companies_table, "cik")

    @staticmethod
    def _validate(item):
        company = restore(Company, item)
        expected = "ENABLED" if company.enabled else None
        if item.get("enabled_marker") != expected:
            raise RepositoryDataError("enabled index disagrees with company")
        return company

    async def upsert(self, company: Company) -> None:
        item = record(company)
        if company.enabled:
            item["enabled_marker"] = "ENABLED"
        self._validate(item)
        await self._call("put_item", Item=encode(item))

    async def get(self, cik: str) -> Company | None:
        item = await self._get(normalize_cik(cik))
        return None if item is None else self._validate(item)

    async def list_enabled(self, *, page_size=None, token=None) -> Page[Company]:
        items, token = await self._query(index=ENABLED_INDEX, partition="enabled_marker",
                                         value="ENABLED", page_size=page_size, token=token)
        companies = tuple(self._validate(item) for item in items)
        return Page(tuple(company for company in companies if company.enabled), token)
