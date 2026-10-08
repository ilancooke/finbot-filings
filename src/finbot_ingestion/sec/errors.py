"""Typed SEC boundary failures."""

class SECError(Exception):
    """SEC acquisition failure."""

class SECDataError(SECError, ValueError):
    """Invalid metadata; never checkpoint the response as complete."""

class SECIncompletePackageError(SECDataError):
    """Package metadata is not yet sufficient for enumeration."""

class SECDocumentTooLarge(SECDataError):
    """Document exceeds the configured bounded acquisition size."""

class SECNetworkError(SECError):
    pass

class SECHTTPError(SECError):
    def __init__(self, status_code: int, url: str):
        self.status_code = status_code
        super().__init__(f"SEC HTTP {status_code}: {url}")
