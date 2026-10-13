import uuid
from datetime import date, datetime, time, timedelta, timezone
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, abort
from sqlalchemy import select
from app.extensions import db
from app.models import (
    Tenant,
    Resource,
    Customer,
    Booking,
    BookingStatus,
    PaymentMode,
)
from app.middleware.tenant import get_tenant_by_slug
from app.services.slot_engine import (
    get_available_slots,
    create_slot_hold,
    SlotUnavailableError,
)
from app.services.pricing import calculate_booking_quote
from app.services.payment_gw import create_order, record_successful_payment
from app.services.qr_generator import generate_qr_code_data_uri

public_bp = Blueprint("public", __name__, url_prefix="/b")


def _get_upcoming_dates(days: int = 7) -> list[dict]:
    """Generates next `days` dates with day labels for mobile pill selector."""
    today = date.today()
    dates = []
    day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    for i in range(days):
        d = today + timedelta(days=i)
        is_today = (i == 0)
        is_tomorrow = (i == 1)
        sub_label = "Today" if is_today else ("Tomorrow" if is_tomorrow else day_labels[d.weekday()])
        dates.append({
            "date": d.isoformat(),
            "date_obj": d,
            "day_number": d.day,
            "month_name": month_labels[d.month - 1],
            "day_name": day_labels[d.weekday()],
            "sub_label": sub_label,
            "is_today": is_today,
            "is_weekend": d.weekday() >= 5,
        })
    return dates


@public_bp.route("/<slug>")
def booking_page(slug: str):
    """Main mobile-first public booking page for a business."""
    tenant = get_tenant_by_slug(slug)

    # Fetch active resources
    resources = db.session.execute(
        select(Resource).filter(
            Resource.tenant_id == tenant.id,
            Resource.is_active.is_(True)
        )
    ).scalars().all()

    if not resources:
        return render_template("public/no_resources.html", tenant=tenant)

    # Selected resource (default to first)
    resource_id_str = request.args.get("resource_id")
    selected_resource = None
    if resource_id_str:
        selected_resource = next((r for r in resources if str(r.id) == resource_id_str), None)
    if not selected_resource:
        selected_resource = resources[0]

    # Date handling
    upcoming_dates = _get_upcoming_dates(days=7)
    date_str = request.args.get("date", upcoming_dates[0]["date"])
    try:
        selected_date = date.fromisoformat(date_str)
    except ValueError:
        selected_date = upcoming_dates[0]["date_obj"]

    # Calculate initial slots for the selected resource and date
    slots = get_available_slots(
        tenant_id=tenant.id,
        resource_id=selected_resource.id,
        slot_date=selected_date,
    )

    return render_template(
        "public/booking.html",
        tenant=tenant,
        resources=resources,
        resource=selected_resource,
        selected_resource=selected_resource,
        upcoming_dates=upcoming_dates,
        selected_date=selected_date,
        slots=slots,
    )


@public_bp.route("/<slug>/slots")
def slots_partial(slug: str):
    """HTMX endpoint returning the refreshed slot grid partial."""
    tenant = get_tenant_by_slug(slug)

    resource_id_str = request.args.get("resource_id")
    date_str = request.args.get("date")

    if not resource_id_str or not date_str:
        return "<p class='text-red-500 p-4'>Resource and date are required.</p>", 400

    try:
        resource_id = uuid.UUID(resource_id_str)
        slot_date = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return "<p class='text-red-500 p-4'>Invalid resource or date format.</p>", 400

    resource = db.session.execute(
        select(Resource).filter(
            Resource.tenant_id == tenant.id,
            Resource.id == resource_id,
            Resource.is_active.is_(True)
        )
    ).scalar_one_or_none()

    if not resource:
        return "<p class='text-red-500 p-4'>Resource not found.</p>", 404

    slots = get_available_slots(
        tenant_id=tenant.id,
        resource_id=resource.id,
        slot_date=slot_date,
    )

    return render_template(
        "public/_slots_grid.html",
        tenant=tenant,
        resource=resource,
        selected_date=slot_date,
        slots=slots,
    )


@public_bp.route("/<slug>/hold", methods=["POST"])
def create_hold(slug: str):
    """
    Places an atomic 7-minute lock on a slot and prepares checkout.
    Requires DPDP consent and customer details.
    """
    tenant = get_tenant_by_slug(slug)

    data = request.form if request.form else (request.get_json() or {})

    resource_id_str = data.get("resource_id")
    date_str = data.get("date")
    start_time_str = data.get("start_time")
    end_time_str = data.get("end_time")
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip().replace(" ", "").replace("-", "")
    dpdp_consent = data.get("dpdp_consent") in ("on", "true", True, "1")
    payment_choice = data.get("payment_choice", "advance")  # "advance" or "full"

    # Validation
    if not dpdp_consent:
        if request.headers.get("HX-Request"):
            return "<div class='p-3 bg-red-900/60 border border-red-500 text-red-200 rounded-lg text-sm'>Consent is required under DPDP Act to proceed.</div>", 400
        return jsonify({"error": "DPDP consent is required"}), 400

    if not name or len(phone) < 10:
        if request.headers.get("HX-Request"):
            return "<div class='p-3 bg-red-900/60 border border-red-500 text-red-200 rounded-lg text-sm'>Please enter a valid name and 10-digit phone number.</div>", 400
        return jsonify({"error": "Invalid name or phone number"}), 400

    try:
        resource_id = uuid.UUID(resource_id_str)
        slot_date = date.fromisoformat(date_str)
        start_time = time.fromisoformat(start_time_str)
        end_time = time.fromisoformat(end_time_str)
    except (ValueError, TypeError) as err:
        return jsonify({"error": f"Invalid slot timing: {err}"}), 400

    resource = db.session.execute(
        select(Resource).filter(
            Resource.tenant_id == tenant.id,
            Resource.id == resource_id,
            Resource.is_active.is_(True)
        )
    ).scalar_one_or_none()

    if not resource:
        return jsonify({"error": "Resource not found"}), 404

    # 1. Upsert Customer with DPDP consent
    customer = db.session.execute(
        select(Customer).filter_by(tenant_id=tenant.id, phone=phone)
    ).scalar_one_or_none()

    now_utc = datetime.now(timezone.utc)

    if customer:
        customer.name = name
        customer.dpdp_consent_given = True
        customer.dpdp_consent_at = now_utc
    else:
        customer = Customer(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name=name,
            phone=phone,
            dpdp_consent_given=True,
            dpdp_consent_at=now_utc,
        )
        db.session.add(customer)
    db.session.flush()

    # 2. Calculate Pricing Quote
    quote = calculate_booking_quote(
        resource=resource,
        slot_date=slot_date,
        slot_intervals=[(start_time, end_time)],
        tenant=tenant,
    )

    if payment_choice == "full" or tenant.advance_percentage == 100:
        payable_paise = quote.total_amount_paise
    else:
        payable_paise = quote.advance_amount_paise

    # 3. Create 7-minute atomic hold
    try:
        booking = create_slot_hold(
            tenant_id=tenant.id,
            resource_id=resource.id,
            slot_date=slot_date,
            start_time=start_time,
            end_time=end_time,
            customer_id=customer.id,
            hold_duration_minutes=7,
            total_amount_paise=quote.total_amount_paise,
            paid_amount_paise=0,
            now=now_utc,
        )
    except SlotUnavailableError as err:
        if request.headers.get("HX-Request"):
            return f"<div class='p-3 bg-red-900/60 border border-red-500 text-red-200 rounded-lg text-sm'>{err}</div>", 409
        return jsonify({"error": str(err)}), 409

    # 4. Create Order (mock or real)
    order_data = create_order(
        tenant=tenant,
        booking=booking,
        amount_paise=payable_paise,
    )

    if request.headers.get("HX-Request"):
        return render_template(
            "public/_checkout_payment_step.html",
            tenant=tenant,
            resource=resource,
            booking=booking,
            customer=customer,
            quote=quote,
            payable_paise=payable_paise,
            payment_choice=payment_choice,
            order_data=order_data,
        )

    return jsonify({
        "booking_id": str(booking.id),
        "locked_until": booking.locked_until.isoformat() if booking.locked_until else None,
        "total_paise": quote.total_amount_paise,
        "payable_paise": payable_paise,
        "order_id": order_data["order_id"],
        "is_mock": order_data["is_mock"],
    })


@public_bp.route("/<slug>/pay-mock", methods=["POST"])
def mock_payment(slug: str):
    """
    Development/Demo payment endpoint.
    Simulates successful UPI payment capture without third-party webhooks.
    """
    tenant = get_tenant_by_slug(slug)
    data = request.form if request.form else (request.get_json() or {})

    booking_id_str = data.get("booking_id")
    amount_paise_str = data.get("amount_paise")

    if not booking_id_str:
        return jsonify({"error": "booking_id is required"}), 400

    try:
        booking_id = uuid.UUID(booking_id_str)
        amount_paise = int(amount_paise_str) if amount_paise_str else 0
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid booking_id or amount"}), 400

    booking = db.session.execute(
        select(Booking).filter(
            Booking.tenant_id == tenant.id,
            Booking.id == booking_id,
        )
    ).scalar_one_or_none()

    if not booking:
        return jsonify({"error": "Booking not found"}), 404

    if amount_paise == 0:
        amount_paise = booking.total_amount_paise

    # Record payment and confirm booking
    payment = record_successful_payment(
        tenant_id=tenant.id,
        booking_id=booking.id,
        amount_paise=amount_paise,
        mode=PaymentMode.UPI_ONLINE,
        razorpay_order_id=f"order_mock_{booking.id.hex[:10]}",
        razorpay_payment_id=f"pay_mock_{uuid.uuid4().hex[:10]}",
        payload={"gateway": "mock_upi", "status": "captured"},
    )

    if request.headers.get("HX-Request"):
        # Redirect or render confirmation
        return f"""
        <script>
            window.location.href = "{url_for('public.confirmation_page', slug=tenant.slug, booking_id=booking.id)}";
        </script>
        """

    return jsonify({
        "status": "success",
        "booking_id": str(booking.id),
        "payment_id": str(payment.id),
        "redirect_url": url_for("public.confirmation_page", slug=tenant.slug, booking_id=booking.id),
    })


@public_bp.route("/<slug>/confirmation/<uuid:booking_id>")
def confirmation_page(slug: str, booking_id: uuid.UUID):
    """Renders booking pass and confirmation ticket."""
    tenant = get_tenant_by_slug(slug)

    booking = db.session.execute(
        select(Booking).filter(
            Booking.tenant_id == tenant.id,
            Booking.id == booking_id,
        )
    ).scalar_one_or_none()

    if not booking:
        abort(404, description="Booking not found.")

    # QR code pointing back to venue public page
    qr_data_uri = generate_qr_code_data_uri(
        f"{request.host_url}b/{tenant.slug}"
    )

    balance_paise = max(0, booking.total_amount_paise - booking.paid_amount_paise)

    return render_template(
        "public/confirmation.html",
        tenant=tenant,
        booking=booking,
        balance_paise=balance_paise,
        qr_data_uri=qr_data_uri,
    )
