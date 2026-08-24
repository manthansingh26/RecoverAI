"""Operator roles and permission definitions for RecoverAI RBAC.

Role hierarchy: VIEWER < OPERATOR < ADMIN.

Permissions are the unit of authorization. Each role maps to a set of
permissions. Route-level authorization is expressed as permission checks
(see app.api.deps.require_permission). A route requiring an action such as
approving a case must declare APPROVE_CASE so a future role reshuffle cannot
silently grant or revoke access without an explicit decision.
"""

from enum import Enum


class OperatorRole(str, Enum):
    """Hierarchical roles available to RecoverAI operators."""

    VIEWER = "VIEWER"
    OPERATOR = "OPERATOR"
    ADMIN = "ADMIN"

    def __lt__(self, other: "OperatorRole") -> bool:
        """Order roles by hierarchy level (VIEWER < OPERATOR < ADMIN)."""
        return _ROLE_LEVEL[self] < _ROLE_LEVEL[other]

    def __le__(self, other: "OperatorRole") -> bool:
        return _ROLE_LEVEL[self] <= _ROLE_LEVEL[other]


_ROLE_LEVEL = {
    OperatorRole.VIEWER: 1,
    OperatorRole.OPERATOR: 2,
    OperatorRole.ADMIN: 3,
}


class Permission:
    """Permission identifiers used to protect routes.

    These are the granular capability names checked by require_permission.
    """

    # Viewer-level (read)
    VIEW_CASES = "VIEW_CASES"
    VIEW_EXECUTION_LOGS = "VIEW_EXECUTION_LOGS"
    VIEW_DASHBOARD = "VIEW_DASHBOARD"
    VIEW_ANALYTICS = "VIEW_ANALYTICS"
    VIEW_ACTIVITY = "VIEW_ACTIVITY"

    # Operator-level (write / act)
    APPROVE_CASE = "APPROVE_CASE"
    REJECT_CASE = "REJECT_CASE"
    EXECUTE_CASE = "EXECUTE_CASE"
    CREATE_RECOVERY_CHECKOUT = "CREATE_RECOVERY_CHECKOUT"
    CREATE_PAYMENT_ORDER = "CREATE_PAYMENT_ORDER"
    RUN_SIMULATION = "RUN_SIMULATION"
    RUN_WORKFLOW = "RUN_WORKFLOW"

    # Admin-level
    MANAGE_OPERATORS = "MANAGE_OPERATORS"
    REVOKE_SESSIONS = "REVOKE_SESSIONS"


VIEWER_PERMISSIONS: frozenset[str] = frozenset({
    Permission.VIEW_CASES,
    Permission.VIEW_EXECUTION_LOGS,
    Permission.VIEW_DASHBOARD,
    Permission.VIEW_ANALYTICS,
    Permission.VIEW_ACTIVITY,
})

OPERATOR_PERMISSIONS: frozenset[str] = frozenset(
    VIEWER_PERMISSIONS
    | {
        Permission.APPROVE_CASE,
        Permission.REJECT_CASE,
        Permission.EXECUTE_CASE,
        Permission.CREATE_RECOVERY_CHECKOUT,
        Permission.CREATE_PAYMENT_ORDER,
        Permission.RUN_SIMULATION,
        Permission.RUN_WORKFLOW,
    }
)

ADMIN_PERMISSIONS: frozenset[str] = frozenset(
    OPERATOR_PERMISSIONS
    | {
        Permission.MANAGE_OPERATORS,
        Permission.REVOKE_SESSIONS,
    }
)

ROLE_PERMISSIONS: dict[OperatorRole, frozenset[str]] = {
    OperatorRole.VIEWER: VIEWER_PERMISSIONS,
    OperatorRole.OPERATOR: OPERATOR_PERMISSIONS,
    OperatorRole.ADMIN: ADMIN_PERMISSIONS,
}


def role_has_permission(role: str | OperatorRole, permission: str) -> bool:
    """Return True if the given role includes the permission."""
    try:
        role_enum = OperatorRole(role)
    except (ValueError, TypeError):
        return False
    return permission in ROLE_PERMISSIONS[role_enum]
