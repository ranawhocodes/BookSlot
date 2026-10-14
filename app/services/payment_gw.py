# app/services/payment_gw.py
import base64
import hashlib
import hmac
import io
import json
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Optional, Dict, Any
import qrcode
from cryptography.fernet import Fernet
from flask import current_app
from app.extensions import db
from app.models import Tenant, Booking, Payment, PaymentMode
from app.services.slot_engine import confirm_booking


@dataclass
class OrderCreationResult:
    order_id: str
    amount_paise: int
    currency: str
    mode: str  # 'RAZORPAY_LIVE', 'UPI_INTENT', 'MOCK'
    key_id: Optional[str] = None
    upi_intent_url: Optional[str] = None
    upi_qr_data_uri: Optional[str] = None
    gpay_intent_url: Optional[str] = None
    phonepe_intent_url: Optional[str] = None
    paytm_intent_url: Optional[str] = None


def _get_fernet() -> Fernet:
    master_key = current_app.config.get("APP_MASTER_KEY", "c2xvdGVuZ2luZS1tYXN0ZXIta2V5LTMyYnl0ZXM=")
    if isinstance(master_key, str):
        master_key_bytes = master_key.encode("utf-8")
    else:
        master_key_bytes = master_key
    try:
        return Fernet(master_key_bytes)
    except Exception:
        derived = base64.urlsafe_b64encode(hashlib.sha256(master_key_bytes).digest())
        return Fernet(derived)


def encrypt_gateway_secret(raw_secret: str) -> bytes:
    """Encrypts a tenant's Razorpay Key Secret at rest using Fernet."""
    return _get_fernet().encrypt(raw_secret.strip().encode("utf-8"))


def decrypt_gateway_secret(enc_bytes: Optional[bytes]) -> Optional[str]:
    """Decrypts a tenant's Razorpay Key Secret."""
    if not enc_bytes:
        return None
    try:
        return _get_fernet().decrypt(enc_bytes).decode("utf-8")
    except Exception:
        return None


# Aliases for cross-module compatibility
encrypt_secret = encrypt_gateway_secret
decrypt_secret = decrypt_gateway_secret


def build_upi_intent_links(
    vpa: str,
    payee_name: str,
    amount_paise: int,
    transaction_note: str,
    transaction_ref: str,
) -> Dict[str, str]:
    """
    Generates standard NPCI UPI deep-links and QR code data URI for GPay, PhonePe, Paytm, and BHIM.
    """
    amount_inr = f"{amount_paise / 100:.2f}"
    params = {
        "pa": vpa.strip(),
        "pn": payee_name.strip()[:50],
        "am": amount_inr,
        "cu": "INR",
        "tn": transaction_note.strip()[:45],
        "tr": transaction_ref.strip()[:35],
    }
    query_str = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    generic_upi = f"upi://pay?{query_str}"
    gpay_upi = f"tez://upi/pay?{query_str}"
    phonepe_upi = f"phonepe://pay?{query_str}"
    paytm_upi = f"paytmmp://pay?{query_str}"

    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(generic_upi)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf)
    qr_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return {
        "upi_intent_url": generic_upi,
        "gpay_intent_url": gpay_upi,
        "phonepe_intent_url": phonepe_upi,
        "paytm_intent_url": paytm_upi,
        "upi_qr_data_uri": f"data:image/png;base64,{qr_b64}",
    }


def resolve_tenant_razorpay_credentials(tenant: Tenant) -> tuple[Optional[str], Optional[str]]:
    """Returns (key_id, key_secret) from tenant config or global fallback."""
    if tenant.razorpay_key_id and tenant.razorpay_key_secret_enc:
        secret = decrypt_gateway_secret(tenant.razorpay_key_secret_enc)
        if secret:
            return tenant.razorpay_key_id, secret

    global_key = current_app.config.get("RAZORPAY_KEY_ID")
    global_secret = current_app.config.get("RAZORPAY_KEY_SECRET")
    if global_key and global_secret:
        return global_key, global_secret
    return None, None


def create_payment_order(
    tenant: Tenant,
    booking: Booking,
    amount_paise: int,
) -> OrderCreationResult:
    """
    Creates a live Razorpay Order if credentials exist, or generates direct UPI Intent deep-links
    alongside mock/sandbox support.
    """
    short_ref = f"SE{booking.id.hex[:10].upper()}"
    vpa = tenant.razorpay_account_id or current_app.config.get("DEFAULT_MERCHANT_UPI_VPA", "pondyarena@okaxis")
    upi_links = build_upi_intent_links(
        vpa=vpa if "@" in str(vpa) else "pondyarena@okaxis",
        payee_name=tenant.name,
        amount_paise=amount_paise,
        transaction_note=f"Slot Booking {short_ref}",
        transaction_ref=short_ref,
    )

    key_id, key_secret = resolve_tenant_razorpay_credentials(tenant)
    if key_id and key_secret and not current_app.config.get("TESTING"):
        try:
            payload: Dict[str, Any] = {
                "amount": amount_paise,
                "currency": tenant.currency or "INR",
                "receipt": short_ref,
                "notes": {
                    "booking_id": str(booking.id),
                    "tenant_id": str(tenant.id),
                    "tenant_slug": tenant.slug,
                },
            }
            if tenant.payment_mode_config == "RAZORPAY_ROUTE" and tenant.razorpay_account_id and tenant.razorpay_account_id.startswith("acc_"):
                payload["transfers"] = [
                    {
                        "account": tenant.razorpay_account_id,
                        "amount": amount_paise,
                        "currency": "INR",
                        "on_hold": 0,
                    }
                ]

            req_data = json.dumps(payload).encode("utf-8")
            auth_str = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("utf-8")
            req = urllib.request.Request(
                "https://api.razorpay.com/v1/orders",
                data=req_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Basic {auth_str}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp_body = json.loads(resp.read().decode("utf-8"))
                return OrderCreationResult(
                    order_id=resp_body["id"],
                    amount_paise=amount_paise,
                    currency=resp_body.get("currency", "INR"),
                    mode="RAZORPAY_LIVE",
                    key_id=key_id,
                    **upi_links,
                )
        except Exception as exc:
            current_app.logger.warning(f"Razorpay live order creation failed, falling back to UPI Intent: {exc}")

    mock_order_id = f"order_mock_{booking.id.hex[:10]}"
    return OrderCreationResult(
        order_id=mock_order_id,
        amount_paise=amount_paise,
        currency=tenant.currency or "INR",
        mode="UPI_INTENT",
        key_id=key_id,
        **upi_links,
    )


def verify_razorpay_checkout_signature(
    order_id: str,
    payment_id: str,
    signature: str,
    key_secret: str,
) -> bool:
    """Verifies Razorpay Standard Checkout callback signature (order_id|payment_id)."""
    if not order_id or not payment_id or not signature or not key_secret:
        return False
    msg = f"{order_id}|{payment_id}".encode("utf-8")
    expected = hmac.new(key_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_razorpay_webhook_signature(raw_body: bytes, signature: Optional[str], secret: str) -> bool:
    """Verifies X-Razorpay-Signature header against raw request payload bytes."""
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_razorpay_signature(raw_body: bytes, signature: Optional[str], secret: str) -> bool:
    """Compatibility alias for webhook HMAC signature verification."""
    return verify_razorpay_webhook_signature(raw_body, signature, secret)


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
    booking = confirm_booking(
        tenant_id=tenant_id,
        booking_id=booking_id,
        paid_amount_paise=amount_paise,
        payment_mode=mode,
        razorpay_payment_id=razorpay_payment_id,
    )

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


def create_order(
    tenant: Tenant,
    booking: Booking,
    amount_paise: int,
) -> Dict[str, Any]:
    """Compatibility wrapper returning dictionary format for create_order."""
    res = create_payment_order(tenant, booking, amount_paise)
    return {
        "order_id": res.order_id,
        "key_id": res.key_id or "rzp_test_mock_slotengine",
        "amount_paise": res.amount_paise,
        "currency": res.currency,
        "is_mock": res.mode == "UPI_INTENT",
        "upi_intent_url": res.upi_intent_url,
        "upi_qr_data_uri": res.upi_qr_data_uri,
    }