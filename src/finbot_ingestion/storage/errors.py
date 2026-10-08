class StorageConflict(ValueError):
    """Existing raw object cannot be reconciled with canonical provenance."""


class StorageLimitError(ValueError):
    """An object/key/metadata exceeds the bounded v0 storage contract."""


class StorageNotVisibleError(RuntimeError):
    """A successful or conflicting write could not yet be inspected."""
