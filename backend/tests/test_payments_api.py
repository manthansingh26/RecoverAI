"""Tests for the payment order creation API (POST /api/payments/create-order)."""

from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
import razorpay.errors

from app.core.config import settings
from app.main import app


@pytest.mark.asyncio
class TestCreateRazorpayOrder:
    """Test suite for POST /api/payments/create-order."""

    async def test_create_order_success_in_rupees(self) -> None:
        """Creating an order in INR rupees converts to paise and returns safe frontend fields."""
        mock_order_response = {
            "id": "order_test_99999",
            "entity": "order",
            "amount": 49900,
            "amount_paid": 0,
            "amount_due": 49900,
            "currency": "INR",
            "receipt": "rcpt_custom_123",
            "status": "created",
            "attempts": 0,
        }

        with (
            patch.object(settings, "RAZORPAY_KEY_ID", "rzp_test_mock_key_id"),
            patch.object(settings, "RAZORPAY_KEY_SECRET", "mock_secret_key_super_private"),
            patch("razorpay.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.order.create.return_value = mock_order_response
            mock_client_cls.return_value = mock_client

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/api/payments/create-order",
                    json={
                        "amount": 499.00,
                        "amount_in_rupees": True,
                        "receipt": "rcpt_custom_123",
                    },
                )

            assert response.status_code == 200
            data = response.json()

            # Required safe fields
            assert data["key_id"] == "rzp_test_mock_key_id"
            assert data["order_id"] == "order_test_99999"
            assert data["amount"] == 49900
            assert data["currency"] == "INR"
            assert data["receipt"] == "rcpt_custom_123"

            # Critical security check: Secret must NEVER be exposed
            assert "mock_secret_key_super_private" not in response.text
            assert "secret" not in data
            assert "key_secret" not in data

            # Verify Razorpay client was called correctly with auth & paise
            mock_client_cls.assert_called_once_with(
                auth=("rzp_test_mock_key_id", "mock_secret_key_super_private")
            )
            mock_client.order.create.assert_called_once()
            call_kwargs = mock_client.order.create.call_args[1]
            assert call_kwargs["data"]["amount"] == 49900
            assert call_kwargs["data"]["currency"] == "INR"
            assert call_kwargs["data"]["receipt"] == "rcpt_custom_123"

    async def test_create_order_success_in_paise(self) -> None:
        """Creating an order with amount_in_rupees=False passes paise directly."""
        mock_order_response = {
            "id": "order_test_paise_001",
            "amount": 75000,
            "currency": "INR",
        }

        with (
            patch.object(settings, "RAZORPAY_KEY_ID", "rzp_test_mock_key_id"),
            patch.object(settings, "RAZORPAY_KEY_SECRET", "mock_secret_key"),
            patch("razorpay.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.order.create.return_value = mock_order_response
            mock_client_cls.return_value = mock_client

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/api/payments/create-order",
                    json={
                        "amount": 75000,
                        "amount_in_rupees": False,
                    },
                )

            assert response.status_code == 200
            data = response.json()
            assert data["amount"] == 75000
            assert data["order_id"] == "order_test_paise_001"

    async def test_missing_credentials_returns_503(self) -> None:
        """When Razorpay keys are not configured in environment, returns 503."""
        with (
            patch.object(settings, "RAZORPAY_KEY_ID", ""),
            patch.object(settings, "RAZORPAY_KEY_SECRET", ""),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/api/payments/create-order",
                    json={"amount": 100},
                )

            assert response.status_code == 503
            assert "not configured" in response.json()["detail"]

    async def test_amount_below_minimum_returns_400(self) -> None:
        """Amounts below 100 paise (₹1) are rejected with 400."""
        with (
            patch.object(settings, "RAZORPAY_KEY_ID", "rzp_test_mock_key_id"),
            patch.object(settings, "RAZORPAY_KEY_SECRET", "mock_secret_key"),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/api/payments/create-order",
                    json={"amount": 0.50, "amount_in_rupees": True},
                )

            assert response.status_code == 400
            assert "at least ₹1.00" in response.json()["detail"]

    async def test_invalid_negative_or_zero_amount_returns_422(self) -> None:
        """Pydantic validation rejects <= 0 amount with 422."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/payments/create-order",
                json={"amount": 0},
            )

        assert response.status_code == 422

    async def test_unsupported_currency_returns_400(self) -> None:
        """Currencies other than INR return 400."""
        with (
            patch.object(settings, "RAZORPAY_KEY_ID", "rzp_test_mock_key_id"),
            patch.object(settings, "RAZORPAY_KEY_SECRET", "mock_secret_key"),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/api/payments/create-order",
                    json={"amount": 500, "currency": "USD"},
                )

            assert response.status_code == 400
            assert "Unsupported currency" in response.json()["detail"]

    async def test_razorpay_bad_request_error_returns_400(self) -> None:
        """Razorpay BadRequestError is forwarded as 400 with detail."""
        with (
            patch.object(settings, "RAZORPAY_KEY_ID", "rzp_test_mock_key_id"),
            patch.object(settings, "RAZORPAY_KEY_SECRET", "mock_secret_key"),
            patch("razorpay.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.order.create.side_effect = razorpay.errors.BadRequestError(
                "Invalid order parameters"
            )
            mock_client_cls.return_value = mock_client

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/api/payments/create-order",
                    json={"amount": 500},
                )

            assert response.status_code == 400
            assert "rejected" in response.json()["detail"]

    async def test_razorpay_gateway_error_returns_502(self) -> None:
        """Razorpay GatewayError is forwarded as 502."""
        with (
            patch.object(settings, "RAZORPAY_KEY_ID", "rzp_test_mock_key_id"),
            patch.object(settings, "RAZORPAY_KEY_SECRET", "mock_secret_key"),
            patch("razorpay.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.order.create.side_effect = razorpay.errors.GatewayError(
                "Gateway upstream error"
            )
            mock_client_cls.return_value = mock_client

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/api/payments/create-order",
                    json={"amount": 500},
                )

            assert response.status_code == 502
            assert "gateway error" in response.json()["detail"]

    async def test_notes_and_auto_receipt_metadata_forwarded(self) -> None:
        """Notes dictionary and automatic receipt generation work properly."""
        mock_order_response = {
            "id": "order_with_notes_123",
            "amount": 20000,
            "currency": "INR",
        }

        with (
            patch.object(settings, "RAZORPAY_KEY_ID", "rzp_test_mock_key_id"),
            patch.object(settings, "RAZORPAY_KEY_SECRET", "mock_secret_key"),
            patch("razorpay.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.order.create.return_value = mock_order_response
            mock_client_cls.return_value = mock_client

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    "/api/payments/create-order",
                    json={
                        "amount": 200,
                        "notes": {"purpose": "test_checkout", "customer_id": "cust_123"},
                    },
                )

            assert response.status_code == 200
            call_kwargs = mock_client.order.create.call_args[1]
            assert call_kwargs["data"]["notes"] == {
                "purpose": "test_checkout",
                "customer_id": "cust_123",
                "created_by": "test@recoverai.local",  # Milestone 14A audit attribution
            }
            assert call_kwargs["data"]["receipt"].startswith("rcpt_")
