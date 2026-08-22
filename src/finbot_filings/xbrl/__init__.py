"""Official SEC XBRL package acquisition."""

from finbot_filings.xbrl.download import (
    XBRLDownloadSummary,
    download_xbrl_packages,
)
from finbot_filings.xbrl.extract import XBRLExtractionSummary, extract_xbrl_filings

__all__ = [
    "XBRLDownloadSummary",
    "XBRLExtractionSummary",
    "download_xbrl_packages",
    "extract_xbrl_filings",
]
