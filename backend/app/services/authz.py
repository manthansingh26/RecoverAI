"""Authorization seam for recovery-case scoping (IDOR protection).

RecoverAI is currently a single-merchant deployment. The routes must NOT
know that: every case access goes through these functions, which form the
one place to add tenant/owner scoping in the future. For now the functions
are identity passes over the existing ``recovery_review`` queries.

Convention (Milestone 14A):
- ``get_case_for_operator`` returns the case or ``None``.
- ``None`` is rendered as HTTP 404 by the route. This makes a nonexistent
  case and a case the operator is not allowed to see *indistinguishable*
  — no existence oracle.
- ``list_cases_for_operator`` is the only list entry point, so a future
  tenant filter is applied everywhere automatically.
"""

from typing import Any

from sqlalchemy.orm import Session

from app.models.operator import Operator
from app.models.recovery_case import RecoveryCase
from app.services.recovery_review import (
    get_case_detail,
    get_execution_logs,
    list_recovery_cases,
)


def get_case_for_operator(
    db: Session,
    recovery_case_id: str,
    operator: Operator,
) -> RecoveryCase | None:
    """Return a single case the operator is allowed to see, else None.

    Single-merchant today: visibility is global. The ``operator`` argument is
    deliberately required so the signature documents the authorization
    boundary and future scoping slots in here without touching route code.
    """
    # Future: if an operator's scope were limited, filter here and return None
    # when the case falls outside the operator's scope.
    del operator  # currently unused; kept as the documented authz boundary
    return get_case_detail(db, recovery_case_id)


def list_cases_for_operator(
    db: Session,
    operator: Operator,
    *,
    status: str | None = None,
    strategy: str | None = None,
    requires_human_approval: bool | None = None,
    approved_by_human: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[RecoveryCase], int]:
    """List cases visible to the operator, with filters and pagination.

    Delegates to the existing query layer so behavior is unchanged. Future
    tenant scoping is added here.
    """
    del operator  # currently unused; kept as the documented authz boundary
    return list_recovery_cases(
        db,
        status=status,
        strategy=strategy,
        requires_human_approval=requires_human_approval,
        approved_by_human=approved_by_human,
        page=page,
        page_size=page_size,
    )


def get_execution_logs_for_operator(
    db: Session,
    recovery_case_id: str,
    operator: Operator,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Any], int]:
    """Return execution logs for a case the operator is allowed to see."""
    del operator  # currently unused; kept as the documented authz boundary
    return get_execution_logs(
        db,
        recovery_case_id,
        page=page,
        page_size=page_size,
    )
