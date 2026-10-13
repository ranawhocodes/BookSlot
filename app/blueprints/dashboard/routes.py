import uuid
from datetime import date, datetime, time, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from sqlalchemy import select, func, and_
from app.extensions import db
from app.models import (
    Tenant,
    Resource,
    Booking,
    BookingStatus,
    Customer,
    Payment,
    PaymentMode,
    UserRole,
)
from app.middleware.tenant import resolve_current_tenant
from app.middleware.auth import require_role
from app.services.slot_engine import get_available_slots
from app.services.pricing import calculate_booking_quote
from app.services.qr_generator import generate_qr_code_data_uri

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


@dashboard_bp.route("/")
@require_role(UserRole.OWNER, UserRole.STAFF)
def turf_dashboard():
    """Unified Turf Operations Dashboard."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404, description="No active tenant found.")

    # Date selector
    date_str = request.args.get("date")
    try:
        selected_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        selected_date = date.today()

    # Active resources
    resources = db.session.execute(
        select(Resource).filter(
            Resource.tenant_id == tenant.id,
            Resource.is_active.is_(True)
        )
    ).scalars().all()

    # Today's bookings
    bookings = db.session.execute(
        select(Booking).filter(
            Booking.tenant_id == tenant.id,
            Booking.slot_date == selected_date,
        ).order_by(Booking.start_time.asc())
    ).scalars().all()

    # Metric calculations
    confirmed_bookings = [
        b for b in bookings
        if b.status in (BookingStatus.CONFIRMED, BookingStatus.COMPLETED)
    ]
    blocked_slots = [b for b in bookings if b.status == BookingStatus.BLOCKED]
    active_holds = [
        b for b in bookings
        if b.status == BookingStatus.PENDING_PAYMENT
        and b.locked_until
        and (b.locked_until if b.locked_until.tzinfo else b.locked_until.replace(tzinfo=timezone.utc)) > datetime.now(timezone.utc)
    ]

    total_confirmed_count = len(confirmed_bookings)

    # Cash vs Online UPI payments for selected date
    payments_today = db.session.execute(
        select(Payment).join(Booking, Payment.booking_id == Booking.id).filter(
            Payment.tenant_id == tenant.id,
            Booking.slot_date == selected_date,
            Payment.status == "CAPTURED"
        )
    ).scalars().all()

    cash_collected_paise = sum(p.amount_paise for p in payments_today if p.mode == PaymentMode.CASH)
    online_upi_collected_paise = sum(
        p.amount_paise for p in payments_today
        if p.mode in (PaymentMode.UPI_ONLINE, PaymentMode.UPI_VENUE)
    )
    total_collected_paise = cash_collected_paise + online_upi_collected_paise

    total_booked_value_paise = sum(b.total_amount_paise for b in confirmed_bookings)
    pending_venue_balance_paise = max(0, total_booked_value_paise - total_collected_paise)

    # Occupancy calculation across all active resources
    total_capacity_slots = 0
    for res in resources:
        try:
            day_slots = get_available_slots(tenant.id, res.id, selected_date)
            total_capacity_slots += len([s for s in day_slots if s.status != "BREAK"])
        except Exception:
            pass

    occupied_count = len(confirmed_bookings) + len(blocked_slots)
    occupancy_pct = (
        round((occupied_count / total_capacity_slots) * 100, 1)
        if total_capacity_slots > 0 else 0.0
    )

    return render_template(
        "dashboard/turf_dashboard.html",
        tenant=tenant,
        resources=resources,
        selected_date=selected_date,
        bookings=bookings,
        confirmed_count=total_confirmed_count,
        blocked_count=len(blocked_slots),
        active_holds_count=len(active_holds),
        cash_collected_paise=cash_collected_paise,
        online_upi_collected_paise=online_upi_collected_paise,
        total_collected_paise=total_collected_paise,
        pending_venue_balance_paise=pending_venue_balance_paise,
        occupancy_pct=occupancy_pct,
        total_capacity_slots=total_capacity_slots,
    )


@dashboard_bp.route("/manual-booking", methods=["POST"])
@require_role(UserRole.OWNER, UserRole.STAFF)
def create_manual_booking():
    """
    2-tap modal on the owner's mobile dashboard to record phone callers/walk-ins.
    E.g. Ramesh - 9876543210 - Cash/UPI Direct - ₹200 Advance Paid, ₹800 Pending.
    """
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    resource_id_str = request.form.get("resource_id")
    slot_date_str = request.form.get("slot_date")
    start_time_str = request.form.get("start_time")
    end_time_str = request.form.get("end_time")
    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip().replace(" ", "").replace("-", "")
    mode_str = request.form.get("payment_mode", "CASH")
    advance_paise_str = request.form.get("advance_paid_paise", "0")
    booking_notes = request.form.get("booking_notes")

    try:
        resource_id = uuid.UUID(resource_id_str)
        slot_date = date.fromisoformat(slot_date_str)
        start_time = time.fromisoformat(start_time_str)
        end_time = time.fromisoformat(end_time_str)
        advance_paid_paise = int(advance_paise_str)
        payment_mode = PaymentMode(mode_str)
    except (ValueError, TypeError) as err:
        flash(f"Invalid input: {err}", "error")
        return redirect(url_for("dashboard.turf_dashboard", slug=tenant.slug))

    resource = db.session.execute(
        select(Resource).filter_by(tenant_id=tenant.id, id=resource_id)
    ).scalar_one_or_none()

    if not resource:
        flash("Resource not found.", "error")
        return redirect(url_for("dashboard.turf_dashboard", slug=tenant.slug))

    # 1. Upsert Customer
    customer = db.session.execute(
        select(Customer).filter_by(tenant_id=tenant.id, phone=phone)
    ).scalar_one_or_none()

    now_utc = datetime.now(timezone.utc)

    if customer:
        customer.name = name
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

    # 2. Quote Calculation
    quote = calculate_booking_quote(
        resource=resource,
        slot_date=slot_date,
        slot_intervals=[(start_time, end_time)],
        tenant=tenant,
    )

    # 3. Create Confirmed Booking (Walk-In / Manual)
    booking = Booking(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        resource_id=resource.id,
        customer_id=customer.id,
        slot_date=slot_date,
        start_time=start_time,
        end_time=end_time,
        status=BookingStatus.CONFIRMED,
        locked_until=None,
        is_walkin=True,
        total_amount_paise=quote.total_amount_paise,
        paid_amount_paise=advance_paid_paise,
        booking_notes=booking_notes or f"Manual phone booking ({name})",
    )
    db.session.add(booking)
    db.session.flush()

    # 4. Record Payment if advance was collected
    if advance_paid_paise > 0:
        payment = Payment(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            booking_id=booking.id,
            amount_paise=advance_paid_paise,
            mode=payment_mode,
            status="CAPTURED",
            razorpay_payment_id=f"venue_{payment_mode.value.lower()}_{uuid.uuid4().hex[:8]}",
        )
        db.session.add(payment)

    db.session.commit()
    flash(f"Booking confirmed for {name} ({start_time.strftime('%I:%M %p')})!", "success")
    return redirect(url_for("dashboard.turf_dashboard", slug=tenant.slug, date=slot_date.isoformat()))


@dashboard_bp.route("/block-slot", methods=["POST"])
@require_role(UserRole.OWNER, UserRole.STAFF)
def block_slot():
    """Blocks a slot for maintenance or tournament."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    resource_id_str = request.form.get("resource_id")
    slot_date_str = request.form.get("slot_date")
    start_time_str = request.form.get("start_time")
    end_time_str = request.form.get("end_time")
    reason = request.form.get("reason", "Maintenance / Tournament")

    try:
        resource_id = uuid.UUID(resource_id_str)
        slot_date = date.fromisoformat(slot_date_str)
        start_time = time.fromisoformat(start_time_str)
        end_time = time.fromisoformat(end_time_str)
    except (ValueError, TypeError) as err:
        flash(f"Invalid input: {err}", "error")
        return redirect(url_for("dashboard.turf_dashboard", slug=tenant.slug))

    block_booking = Booking(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        resource_id=resource_id,
        slot_date=slot_date,
        start_time=start_time,
        end_time=end_time,
        status=BookingStatus.BLOCKED,
        booking_notes=reason,
    )
    db.session.add(block_booking)
    db.session.commit()

    flash(f"Slot {start_time.strftime('%I:%M %p')} blocked for {reason}.", "info")
    return redirect(url_for("dashboard.turf_dashboard", slug=tenant.slug, date=slot_date.isoformat()))


@dashboard_bp.route("/unblock-slot/<uuid:booking_id>", methods=["POST"])
@require_role(UserRole.OWNER, UserRole.STAFF)
def unblock_slot(booking_id: uuid.UUID):
    """Unblocks a maintenance/tournament blocked slot."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    booking = db.session.execute(
        select(Booking).filter_by(tenant_id=tenant.id, id=booking_id, status=BookingStatus.BLOCKED)
    ).scalar_one_or_none()

    if booking:
        slot_date_str = booking.slot_date.isoformat()
        db.session.delete(booking)
        db.session.commit()
        flash("Slot unblocked successfully.", "success")
        return redirect(url_for("dashboard.turf_dashboard", slug=tenant.slug, date=slot_date_str))

    flash("Blocked slot not found.", "error")
    return redirect(url_for("dashboard.turf_dashboard", slug=tenant.slug))


@dashboard_bp.route("/qr-poster")
@require_role(UserRole.OWNER, UserRole.STAFF)
def qr_poster():
    """Generates high-contrast printable A4/A5 QR poster for venue counter."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    booking_url = f"{request.host_url}b/{tenant.slug}"
    qr_data_uri = generate_qr_code_data_uri(booking_url)

    return render_template(
        "dashboard/qr_poster.html",
        tenant=tenant,
        booking_url=booking_url,
        qr_data_uri=qr_data_uri,
    )
