"""Curated company identity."""

from dataclasses import dataclass

from .identity import normalize_cik, normalize_ticker


@dataclass(frozen=True, slots=True)
class Company:
    ticker: str
    cik: str
    name: str
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "cik", normalize_cik(self.cik))
        object.__setattr__(self, "ticker", normalize_ticker(self.ticker))
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("company name must be nonempty")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        object.__setattr__(self, "name", self.name.strip())
