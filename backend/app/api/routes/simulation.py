"""Development simulation endpoints for payment.failed testing.

Provides two simulation modes:
1. Raw simulation — direct payload control (existing, unchanged)
2. Scenario-driven simulation — pre-defined failure scenarios (Milestone 8)

Both endpoints are for local development and hackathon demos ONLY.
They bypass Razorpay signature verification since they are separate
development-only endpoints. They use the same core ingestion and
decision logic as the real webhook route.

NOT a replacement for the real Razorpay webhook endpoint.
"""

import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_origin, require_permission
from app.core.config import settings
from app.core.roles import Permission
from app.db.session import get_db
from app.models.operator import Operator
from app.schemas.simulation import (
    ExecutionResultItem,
    ScenarioSimulationRequest,
    SimulationRequest,
    SimulationResult,
    SimulationScenario,
    WorkflowResultItem,
)
from app.schemas.webhook import WebhookResponse
from app.services.ingestion_service import ingest_payment_event
from app.services.payment_normalizer import normalize_payment_failed
from app.services.recovery_executor import execute_single_case
from app.services.recovery_workflow import process_received_case

logger = logging.getLogger(__name__)

router = APIRouter()


def _is_simulation_enabled() -> bool:
    """Check if simulation endpoints are enabled for current environment."""
    return settings.APP_ENV in ("development", "test")


# ---------------------------------------------------------------------------
# Scenario definitions — map each scenario to realistic input data
# that the EXISTING failure classifier and policy engine will process.
# ---------------------------------------------------------------------------

def _get_scenario_params(scenario: SimulationScenario) -> dict[str, Any]:
    """Map a scenario enum to realistic simulation input parameters.

    CRITICAL: We only supply INPUT DATA here. The classification, strategy,
    policy decision, and final status are ALWAYS determined by the existing
    pipeline (failure_classifier → strategy_advisor → policy_engine →
    decision_engine → recovery_executor).

    Error reasons are chosen to match entries in the failure_classifier's
    _REASON_MAP so the pipeline classifies them deterministically.
    """
    # Use the ACTUAL configured threshold to guarantee the high-value
    # scenario exceeds it (not a hardcoded ₹75,000).
    high_value_amount = settings.RECOVERY_HIGH_VALUE_THRESHOLD_PAISE + 2500000  # threshold + ₹25,000

    scenarios: dict[SimulationScenario, dict[str, Any]] = {
        SimulationScenario.LOW_VALUE_TRANSIENT: {
            "amount_paise": 50000,  # ₹500
            "error_code": "GATEWAY_ERROR",
            "error_reason": "network_error",  # classifier → TRANSIENT
            "error_description": "Payment failed due to a temporary network issue between the payment gateway and the bank.",
        },
        SimulationScenario.MEDIUM_VALUE_RECOVERABLE: {
            "amount_paise": 250000,  # ₹2,500
            "error_code": "BAD_REQUEST_ERROR",
            "error_reason": "authentication_failed",  # classifier → AUTHENTICATION
            "error_description": "Payment authentication failed. Customer may need to retry with correct credentials.",
        },
        SimulationScenario.HIGH_VALUE_HUMAN_REVIEW: {
            "amount_paise": high_value_amount,
            "error_code": "GATEWAY_ERROR",
            "error_reason": "network_error",  # classifier → TRANSIENT, but policy_engine → REQUIRES_HUMAN due to high value
            "error_description": "High-value payment failed due to a temporary gateway issue. Manual review required per policy.",
        },
        SimulationScenario.PERMANENT_FAILURE: {
            "amount_paise": 150000,  # ₹1,500
            "error_code": "BAD_REQUEST_ERROR",
            "error_reason": "debit_instrument_blocked",  # classifier → HARD_FAILURE
            "error_description": "Payment instrument has been permanently blocked by the issuing bank.",
        },
    }
    return scenarios[scenario]


# ---------------------------------------------------------------------------
# POST /api/dev/simulate/payment-failed (existing raw endpoint — unchanged)
# ---------------------------------------------------------------------------

@router.post(
    "/api/dev/simulate/payment-failed",
    dependencies=[Depends(require_origin)],
)
async def simulate_payment_failed(
    request: SimulationRequest,
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_permission(Permission.RUN_SIMULATION)),
) -> WebhookResponse:
    """Simulate a payment.failed event for development/testing.

    This endpoint:
    - Only works in development or test environments.
    - Uses the same ingestion service as the real webhook.
    - Does NOT bypass or weaken the real webhook route.
    - Creates PaymentEvent + RecoveryCase using identical persistence rules.
    - Handles duplicate simulation event IDs idempotently.
    - Requires RUN_SIMULATION permission (operator).

    Args:
        request: Simulation payload with optional event_id and payment details.
        db: Database session dependency.
        operator: Authenticated operator.

    Returns:
        WebhookResponse indicating acceptance and duplicate status.

    Raises:
        HTTPException 404: Simulation disabled outside dev/test.
        HTTPException 422: Invalid simulation request schema.
    """
    if not _is_simulation_enabled():
        raise HTTPException(
            status_code=404,
            detail="Simulation endpoint not available in this environment",
        )

    # Generate or use provided event ID
    event_id = request.event_id or f"sim_{uuid.uuid4().hex[:16]}"

    # Build a Razorpay-compatible payload structure
    simulated_payload: dict[str, Any] = {
        "entity": "event",
        "event": "payment.failed",
        "account_id": "simulated_account",
        "created_at": int(time.time()),
        "payload": {
            "payment": {
                "id": request.payment_id or f"pay_sim_{uuid.uuid4().hex[:12]}",
                "entity": "payment",
                "amount": request.amount_paise,
                "currency": request.currency,
                "status": "failed",
                "order_id": request.order_id,
                "error_code": request.error_code,
                "error_reason": request.error_reason,
                "error_description": request.error_description,
            }
        },
    }

    # Normalize using the same normalizer as the real webhook
    try:
        normalized = normalize_payment_failed(
            event_id=event_id,
            payload_data=simulated_payload,
            raw_payload=simulated_payload,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Simulation payload invalid: {e}",
        )

    # Persist using the shared ingestion function
    result = ingest_payment_event(
        db=db,
        normalized=normalized,
        source="simulation",
        signature_verified=False,
    )

    if not result.success:
        logger.error("Simulation ingestion failed: %s", result.message)
        raise HTTPException(
            status_code=500,
            detail="Internal error processing simulation",
        )

    logger.info(
        "Simulation ingested: event_id=%s duplicate=%s recovery_case_id=%s",
        event_id,
        result.duplicate,
        result.recovery_case_id,
    )

    return WebhookResponse(
        accepted=True,
        duplicate=result.duplicate,
        event_id=event_id,
        recovery_case_id=result.recovery_case_id,
        message=result.message,
    )


# ---------------------------------------------------------------------------
# POST /api/dev/simulate-payment-failure (Milestone 8 scenario endpoint)
# ---------------------------------------------------------------------------

@router.post(
    "/api/dev/simulate-payment-failure",
    dependencies=[Depends(require_origin)],
)
async def simulate_payment_failure_scenario(
    request: ScenarioSimulationRequest,
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_permission(Permission.RUN_SIMULATION)),
) -> SimulationResult:
    """Simulate a complete payment failure recovery pipeline for a pre-defined scenario.

    This endpoint orchestrates the ENTIRE existing pipeline in a single call:
    1. Generate realistic simulated payment data for the chosen scenario
    2. Normalize using the existing payment normalizer
    3. Ingest using the existing ingestion service (creates PaymentEvent + RecoveryCase)
    4. Process the newly-created case through the existing decision engine
    5. If auto-executable, execute using the existing safe simulation executor
    6. Return the final state of the recovery case

    Only available in development/test environments.
    Does NOT bypass policy, human approval, or safety checks.
    Does NOT execute real financial actions.
    Requires RUN_SIMULATION permission (operator).

    Args:
        request: Scenario selection.
        db: Database session dependency.
        operator: Authenticated operator.

    Returns:
        SimulationResult with full pipeline outcome.

    Raises:
        HTTPException 404: Simulation disabled outside dev/test.
        HTTPException 422: Invalid scenario.
        HTTPException 500: Pipeline processing error.
    """
    if not _is_simulation_enabled():
        raise HTTPException(
            status_code=404,
            detail="Simulation endpoint not available in this environment",
        )

    scenario = request.scenario
    params = _get_scenario_params(scenario)

    # 1. Generate unique IDs to avoid idempotency collisions
    event_id = f"sim_{scenario.value.lower()}_{uuid.uuid4().hex[:12]}"
    payment_id = f"pay_sim_{uuid.uuid4().hex[:12]}"
    order_id = f"order_sim_{uuid.uuid4().hex[:8]}"

    # 2. Build Razorpay-compatible payload
    simulated_payload: dict[str, Any] = {
        "entity": "event",
        "event": "payment.failed",
        "account_id": "simulated_account",
        "created_at": int(time.time()),
        "payload": {
            "payment": {
                "id": payment_id,
                "entity": "payment",
                "amount": params["amount_paise"],
                "currency": "INR",
                "status": "failed",
                "order_id": order_id,
                "error_code": params["error_code"],
                "error_reason": params["error_reason"],
                "error_description": params["error_description"],
            }
        },
    }

    # 3. Normalize using the existing normalizer
    try:
        normalized = normalize_payment_failed(
            event_id=event_id,
            payload_data=simulated_payload,
            raw_payload=simulated_payload,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Simulation payload invalid: {e}",
        )

    # 4. Ingest using the existing ingestion service
    ingestion_result = ingest_payment_event(
        db=db,
        normalized=normalized,
        source="simulation",
        signature_verified=False,
    )

    if not ingestion_result.success:
        logger.error("Scenario simulation ingestion failed: %s", ingestion_result.message)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to ingest simulated event: {ingestion_result.message}",
        )

    if ingestion_result.duplicate:
        return SimulationResult(
            success=True,
            scenario=scenario.value,
            payment_id=payment_id,
            event_id=event_id,
            recovery_case_id=ingestion_result.recovery_case_id,
            amount_paise=params["amount_paise"],
            error_code=params["error_code"],
            error_reason=params["error_reason"],
            duplicate=True,
            message="Duplicate event — case already exists",
        )

    recovery_case_id = ingestion_result.recovery_case_id
    if recovery_case_id is None:
        raise HTTPException(
            status_code=500,
            detail="Ingestion succeeded but no recovery_case_id was returned",
        )

    # 5. Process the specific newly-created case through the decision engine
    #    Using process_received_case() to target only THIS case, not all RECEIVED.
    workflow_result = process_received_case(db, recovery_case_id)

    workflow_item = WorkflowResultItem(
        recovery_case_id=workflow_result.recovery_case_id,
        previous_status=workflow_result.previous_status,
        new_status=workflow_result.new_status,
        processed=workflow_result.processed,
        message=workflow_result.message,
    )

    # 6. Re-read the case to get the current state after decision processing
    from app.models.recovery_case import RecoveryCase
    rc = db.get(RecoveryCase, uuid.UUID(recovery_case_id))
    if rc is None:
        raise HTTPException(
            status_code=500,
            detail="Recovery case disappeared after processing",
        )

    # 7. If the case is auto-executable (PENDING_EXECUTION), execute it
    #    using the existing safe simulation executor.
    #    If it REQUIRES_HUMAN, do NOT bypass — leave as-is.
    execution_item: ExecutionResultItem | None = None
    if rc.status == "PENDING_EXECUTION":
        exec_result = execute_single_case(db, recovery_case_id, actor=operator.email)
        if exec_result is not None:
            execution_item = ExecutionResultItem(
                strategy=exec_result.strategy,
                execution_mode=exec_result.execution_mode,
                status=exec_result.status,
                previous_case_status=exec_result.previous_case_status,
                new_case_status=exec_result.new_case_status,
                message=exec_result.message,
            )
            # Refresh to get post-execution state
            db.refresh(rc)

    # 8. Build human-readable message
    message = _build_result_message(scenario, rc)

    logger.info(
        "Scenario simulation complete: scenario=%s case=%s status=%s strategy=%s",
        scenario.value,
        recovery_case_id,
        rc.status,
        rc.recommended_strategy,
    )

    return SimulationResult(
        success=True,
        scenario=scenario.value,
        payment_id=payment_id,
        event_id=event_id,
        recovery_case_id=recovery_case_id,
        amount_paise=params["amount_paise"],
        error_code=params["error_code"],
        error_reason=params["error_reason"],
        failure_category=rc.failure_category,
        recommended_strategy=rc.recommended_strategy,
        recovery_probability=float(rc.recovery_probability) if rc.recovery_probability is not None else None,
        status=rc.status,
        requires_human_approval=rc.requires_human_approval,
        approved_by_human=rc.approved_by_human,
        execution_result=execution_item,
        workflow=workflow_item,
        message=message,
        duplicate=False,
    )


def _build_result_message(scenario: SimulationScenario, rc: Any) -> str:
    """Build a human-readable result message based on the final case state."""
    status = rc.status
    strategy = rc.recommended_strategy or "N/A"

    if status == "REQUIRES_HUMAN":
        return (
            f"Recovery case created and analyzed. Strategy '{strategy}' requires "
            f"human approval before execution. Navigate to the case to approve or reject."
        )
    if status == "RESOLVED_SUCCESS":
        return (
            f"Recovery case created, analyzed, and successfully executed using "
            f"strategy '{strategy}' in simulation mode."
        )
    if status == "RESOLVED_FAILED":
        return (
            f"Recovery case created and analyzed. The recovery was safely stopped "
            f"with strategy '{strategy}' — no retry is appropriate for this failure type."
        )
    if status == "PENDING_EXECUTION":
        return (
            f"Recovery case created and analyzed. Strategy '{strategy}' is pending "
            f"execution. The case is scheduled for processing."
        )
    return f"Recovery case created. Current status: {status}, strategy: {strategy}."
