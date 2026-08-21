"""Enum definitions for RecoverAI domain concepts."""

from enum import Enum


class RecoveryStatus(str, Enum):
    """Lifecycle status of a recovery case."""

    RECEIVED = "RECEIVED"
    DECISION_PENDING = "DECISION_PENDING"
    PENDING_EXECUTION = "PENDING_EXECUTION"
    REQUIRES_HUMAN = "REQUIRES_HUMAN"
    EXECUTING = "EXECUTING"
    RESOLVED_SUCCESS = "RESOLVED_SUCCESS"
    RESOLVED_FAILED = "RESOLVED_FAILED"


class FailureCategory(str, Enum):
    """Categorization of why a payment failed."""

    TRANSIENT = "TRANSIENT"
    AUTHENTICATION = "AUTHENTICATION"
    HARD_FAILURE = "HARD_FAILURE"
    UNKNOWN = "UNKNOWN"


class RecoveryStrategy(str, Enum):
    """Deterministic recovery strategy to apply."""

    WAIT_AND_RETRY = "WAIT_AND_RETRY"
    CREATE_PAYMENT_LINK = "CREATE_PAYMENT_LINK"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    STOP_RECOVERY = "STOP_RECOVERY"


class ExecutionMode(str, Enum):
    """Whether execution is simulated or via real Razorpay API."""

    SIMULATION = "SIMULATION"
    RAZORPAY = "RAZORPAY"


class ExecutionStatus(str, Enum):
    """Outcome of a single execution attempt."""

    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
