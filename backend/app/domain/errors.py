"""Domain errors.

Services raise these; they carry no HTTP knowledge. The REST layer maps them
to status codes in Phase 12 (NotFound -> 404, Conflict -> 409, ...), and the
MCP server maps the same errors to tool errors, so both surfaces behave alike.
"""


class DomainError(Exception):
    """Base class for expected, business-level failures."""

    code = "domain_error"


class ValidationFailedError(DomainError):
    code = "validation_failed"


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class PermissionDeniedError(DomainError):
    code = "permission_denied"


class InvariantViolationError(DomainError):
    """The operation would break a rule that must always hold (e.g. 'at least one ADMIN')."""

    code = "invariant_violation"
