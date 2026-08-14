"""Named fail-closed errors for Clerk read projections."""


class ProjectionReadError(Exception):
    """The verified authority could not produce a coherent read snapshot."""


class ProjectionIdentityMismatch(ProjectionReadError):
    """The read connection does not point at the repository identity supplied."""


class InvalidTimelineCursor(ProjectionReadError):
    """A timeline cursor is malformed or belongs to another projection."""
