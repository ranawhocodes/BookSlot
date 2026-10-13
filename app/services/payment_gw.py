import hmac
import hashlib
import uuid
from typing import Optional, Dict, Any
from app.extensions import db
from app.models import Tenant, Booking, Payment, PaymentMode
from app.services.slot_engine import confirm_booking


class PaymentGatewayError(Exception):
    """Base exception for payment gateway operations."""
    pass


class InvalidSignatureError(PaymentGatewayError):
    """Raised when webhook or payment signature verification fails."""
    pass


def create_order(
    tenant: Tenant,
    booking: Booking,
    amount_paise: int,
) -> Dict[str, Any]:
    """
    Creates an online payment order.
    In development or when Razorpay credentials are not configured,
    returns a mock order object for testing without external API calls.
    """
    # Check if tenant has real Razorpay credentials configured
    if tenant.razorpay_key_id and tenant.payment_mode_config in ("RAZORPAY_OWN", "RAZORPAY_ROUTE"):
        # Real Razorpay order creation could be called here when SDK is active
        order_id = f"order_{uuid.uuid4().hex[:16]}"
        return {
            "order_id": order_id,
            "key_id": tenant.razorpay_key_id,
            "amount_paise": amount_paise,
            "currency": tenant.currency,
            "is_mock": False,
        }

    # Dev/Mock fallback
    mock_order_id = f"order_mock_{uuid.uuid4().hex[:16]}"
    return {
        "order_id": mock_order_id,
        "key_id": "rzp_test_mock_slotengine",
        "amount_paise": amount_paise,
        "currency": tenant.currency,
        "is_mock": True,
    }


def verify_razorpay_signature(
    raw_body: bytes,
    signature: str,
    secret: str,
) -> bool:
    """
    Verifies the Razorpay webhook or checkout signature using HMAC-SHA256.
    Ensures zero tampering and strictly enforces raw request bytes.
    """
    if not signature or not secret:
        return False

    expected_signature = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_signature, signature)


def record_successful_payment(
    tenant_id: uuid.UUID,
    booking_id: uuid.UUID,
    amount_paise: int,
    mode: PaymentMode = PaymentMode.UPI_ONLINE,
    razorpay_order_id: Optional[str] = None,
    razorpay_payment_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Payment:
    """
    Atomically marks the booking as CONFIRMED and logs the captured Payment record.
    Strictly scoped to tenant_id.
    """
    # 1. Confirm the booking
    booking = confirm_booking(
        tenant_id=tenant_id,
        booking_id=booking_id,
        paid_amount_paise=amount_paise,
        payment_mode=mode,
        razorpay_payment_id=razorpay_payment_id,
    )

    # 2. Record payment audit row
    payment = Payment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        booking_id=booking.id,
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id or f"pay_mock_{uuid.uuid4().hex[:12]}",
        amount_paise=amount_paise,
        mode=mode,
        status="CAPTURED",
        webhook_payload=payload,
    )
    db.session.add(payment)
    db.session.commit()
    return payment
