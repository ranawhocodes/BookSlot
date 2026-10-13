import json
import uuid
from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import select
from app.models import Tenant, Booking, PaymentMode
from app.services.payment_gw import verify_razorpay_signature, record_successful_payment
from app.extensions import db

webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


@webhooks_bp.route("/razorpay", methods=["POST"])
def razorpay_webhook():
    """
    Razorpay Webhook endpoint.
    Strictly verifies X-Razorpay-Signature HMAC on raw request bytes.
    Idempotently handles payment.captured and order.paid events.
    """
    signature = request.headers.get("X-Razorpay-Signature")
    raw_body = request.get_data()

    if not signature:
        return jsonify({"error": "Missing X-Razorpay-Signature header"}), 400

    secret = current_app.config.get("RAZORPAY_WEBHOOK_SECRET", "dev_razorpay_secret_change_in_prod")

    if not verify_razorpay_signature(raw_body, signature, secret):
        return jsonify({"error": "Invalid webhook signature"}), 400

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return jsonify({"error": "Invalid JSON payload"}), 400

    event = payload.get("event")
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    order_entity = payload.get("payload", {}).get("order", {}).get("entity", {})

    order_id = payment_entity.get("order_id") or order_entity.get("id")
    payment_id = payment_entity.get("id")
    amount_paise = payment_entity.get("amount") or order_entity.get("amount")
    notes = payment_entity.get("notes", {}) or order_entity.get("notes", {})
    booking_id_str = notes.get("booking_id")
    tenant_id_str = notes.get("tenant_id")

    if event in ("payment.captured", "order.paid") and booking_id_str:
        try:
            booking_id = uuid.UUID(booking_id_str)
            tenant_id = uuid.UUID(tenant_id_str) if tenant_id_str else None
        except ValueError:
            return jsonify({"status": "invalid_uuids_in_notes"}), 400

        # Query booking scoped to tenant
        query = select(Booking).filter(Booking.id == booking_id)
        if tenant_id:
            query = query.filter(Booking.tenant_id == tenant_id)

        booking = db.session.execute(query).scalar_one_or_none()

        if booking:
            record_successful_payment(
                tenant_id=booking.tenant_id,
                booking_id=booking.id,
                amount_paise=amount_paise or booking.total_amount_paise,
                mode=PaymentMode.UPI_ONLINE,
                razorpay_order_id=order_id,
                razorpay_payment_id=payment_id,
                payload=payload,
            )

    return jsonify({"status": "ok"}), 200
