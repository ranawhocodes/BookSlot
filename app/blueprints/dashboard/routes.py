# app/blueprints/dashboard/routes.py
import re
import uuid
from datetime import date, datetime, time, timezone
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session
from sqlalchemy import select
from app.extensions import db
from app.models import (
    Tenant,
    VerticalType,
    User,
    Resource,
    AvailabilityRule,
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
from app.services.payment_gw import encrypt_gateway_secret
from app.utils.validators import sanitize_and_validate_name, sanitize_and_validate_phone

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


@dashboard_bp.route("/")
@require_role(UserRole.OWNER, UserRole.STAFF)
def turf_dashboard():
    """Unified Turf Operations Dashboard."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404, description="No active tenant found.")

    date_str = request.args.get("date")
    try:
        selected_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        selected_date = date.today()

    resources = db.session.execute(
        select(Resource).filter(
            Resource.tenant_id == tenant.id,
            Resource.is_active.is_(True)
        )
    ).scalars().all()

    bookings = db.session.execute(
        select(Booking).filter(
            Booking.tenant_id == tenant.id,
            Booking.slot_date == selected_date,
        ).order_by(Booking.start_time.asc())
    ).scalars().all()

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
    """2-tap modal on the owner's mobile dashboard to record phone callers/walk-ins."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    resource_id_str = request.form.get("resource_id")
    slot_date_str = request.form.get("slot_date")
    start_time_str = request.form.get("start_time")
    end_time_str = request.form.get("end_time")

    name, name_err = sanitize_and_validate_name(request.form.get("name"))
    phone, phone_err = sanitize_and_validate_phone(request.form.get("phone"))
    if name_err or phone_err:
        flash(name_err or phone_err, "error")
        return redirect(url_for("dashboard.turf_dashboard", slug=tenant.slug))

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

    quote = calculate_booking_quote(
        resource=resource,
        slot_date=slot_date,
        slot_intervals=[(start_time, end_time)],
        tenant=tenant,
    )

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


@dashboard_bp.route("/onboard", methods=["GET", "POST"])
def onboard_tenant_wizard():
    """Self-Service Client Onboarding Wizard (Gap 6)."""
    if request.method == "POST":
        biz_name = request.form.get("business_name", "").strip()
        raw_slug = request.form.get("slug", "").strip().lower()
        slug = re.sub(r"[^a-z0-9\-]", "-", raw_slug).strip("-")
        vertical_str = request.form.get("vertical", "TURF")
        owner_name, name_err = sanitize_and_validate_name(request.form.get("owner_name"))
        phone, phone_err = sanitize_and_validate_phone(request.form.get("phone"))
        password = request.form.get("password", "password123")
        address = request.form.get("address", "").strip()
        advance_pct = int(request.form.get("advance_percentage", "30" if vertical_str == "TURF" else "100"))
        upi_vpa = request.form.get("upi_vpa", "").strip()
        rzp_key_id = request.form.get("razorpay_key_id", "").strip() or None
        rzp_secret = request.form.get("razorpay_key_secret", "").strip()

        if name_err or phone_err or not biz_name or not slug:
            flash(name_err or phone_err or "Business name and URL slug are required.", "error")
            return render_template("dashboard/onboard.html")

        existing = db.session.execute(select(Tenant).filter_by(slug=slug)).scalar_one_or_none()
        if existing:
            flash(f"URL slug '{slug}' is already taken. Please pick another.", "error")
            return render_template("dashboard/onboard.html")

        vertical = VerticalType(vertical_str)
        tenant = Tenant(
            id=uuid.uuid4(),
            slug=slug,
            name=biz_name,
            vertical=vertical,
            phone=phone,
            address=address,
            payment_mode_config="RAZORPAY_OWN" if rzp_key_id else "UPI_INTENT",
            razorpay_account_id=upi_vpa or None,
            razorpay_key_id=rzp_key_id,
            razorpay_key_secret_enc=encrypt_gateway_secret(rzp_secret) if rzp_secret else None,
            advance_percentage=advance_pct,
            monthly_msg_quota=500,
        )
        db.session.add(tenant)
        db.session.flush()

        owner = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name=owner_name,
            phone=phone,
            role=UserRole.OWNER,
        )
        owner.set_password(password)
        db.session.add(owner)

        res_name = request.form.get("resource_name", "").strip() or (
            "5v5 Main Turf" if vertical == VerticalType.TURF else f"Dr. {owner_name}"
        )
        slot_mins = int(request.form.get("slot_duration_mins", "60" if vertical == VerticalType.TURF else "20"))
        base_inr = int(request.form.get("base_price_inr", "800" if vertical == VerticalType.TURF else "300"))
        peak_inr = int(request.form.get("peak_price_inr", str(base_inr)))

        resource = Resource(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name=res_name,
            category="Sports Venue" if vertical == VerticalType.TURF else "Consultation",
            slot_duration_mins=slot_mins,
            base_price_paise=base_inr * 100,
        )
        db.session.add(resource)
        db.session.flush()

        for day in range(0, 7):
            if vertical == VerticalType.TURF:
                db.session.add(
                    AvailabilityRule(
                        id=uuid.uuid4(),
                        tenant_id=tenant.id,
                        resource_id=resource.id,
                        day_of_week=day,
                        start_time=time(6, 0),
                        end_time=time(17, 0),
                        is_break=False,
                        price_override_paise=base_inr * 100,
                    )
                )
                db.session.add(
                    AvailabilityRule(
                        id=uuid.uuid4(),
                        tenant_id=tenant.id,
                        resource_id=resource.id,
                        day_of_week=day,
                        start_time=time(17, 0),
                        end_time=time(23, 0),
                        is_break=False,
                        price_override_paise=peak_inr * 100,
                    )
                )
            else:
                db.session.add(
                    AvailabilityRule(
                        id=uuid.uuid4(),
                        tenant_id=tenant.id,
                        resource_id=resource.id,
                        day_of_week=day,
                        start_time=time(9, 0),
                        end_time=time(13, 30),
                        is_break=False,
                        price_override_paise=base_inr * 100,
                    )
                )
                db.session.add(
                    AvailabilityRule(
                        id=uuid.uuid4(),
                        tenant_id=tenant.id,
                        resource_id=resource.id,
                        day_of_week=day,
                        start_time=time(16, 30),
                        end_time=time(20, 30),
                        is_break=False,
                        price_override_paise=base_inr * 100,
                    )
                )

        db.session.commit()
        session["tenant_id"] = str(tenant.id)
        session["user_role"] = UserRole.OWNER.value
        flash(f"Welcome! {biz_name} is live at /b/{slug}", "success")
        if vertical == VerticalType.CLINIC:
            return redirect(url_for("clinic.queue_board", slug=slug))
        return redirect(url_for("dashboard.turf_dashboard", slug=slug))

    return render_template("dashboard/onboard.html")