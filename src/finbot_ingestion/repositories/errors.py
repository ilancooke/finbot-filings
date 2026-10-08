"""Infrastructure-independent persistence failures."""


class RepositoryError(RuntimeError):
    pass


class RepositoryConflict(RepositoryError):
    """A write would replace incompatible provenance or checkpoint facts."""


class RepositoryNotFound(RepositoryError):
    """An update asserted the existence of a missing record."""


class RepositoryDataError(RepositoryError):
    """A durable record violates the repository schema."""


class RepositoryBusy(RepositoryError):
    """Bounded compare-and-set attempts were exhausted; caller may retry."""
