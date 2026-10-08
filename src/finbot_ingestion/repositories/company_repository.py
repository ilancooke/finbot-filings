from typing import Protocol

from finbot_ingestion.domain import Company
from .pagination import Page


class CompanyRepository(Protocol):
    async def upsert(self, company: Company) -> None: ...
    async def get(self, cik: str) -> Company | None: ...
    async def list_enabled(self, *, page_size: int = 100, token: str | None = None) -> Page[Company]: ...
