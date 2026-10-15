# app/blueprints/dashboard/routes.py
import base64
import io
import uuid
from datetime import date, datetime, time, timedelta, timezone
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    abort,
    session,
    g,
)
import qrcode
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Tenant,
    VerticalType,
    User,
    UserRole,
    Resource,
    AvailabilityRule,
    Customer,
    Booking,
    BookingStatus,
    Payment,
    PaymentMode,
)
from app.services.pricing import calculate_slot_price
from app.services.payment_gw import encrypt_secret
from app.services.notifier import NotifierService
from app.utils.validators import (
    sanitize_and_validate_name,
    sanitize_and_validate_phone,
)

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.before_request
def require_dashboard_auth():
    """Enforce session authentication, role authorization, and strict tenant isolation on all dashboard routes."""
    if request.endpoint in ("dashboard.onboard", "dashboard.onboard_tenant"):
        return None

    user_role = session.get("user_role")
    session_tenant_id = session.get("tenant_id")

    if not user_role or not session_tenant_id:
        return redirect(url_for("auth.login", next=request.full_path))

    allowed_roles = {
        UserRole.OWNER.value,
        UserRole.STAFF.value,
        UserRole.DOCTOR.value,
        UserRole.RECEPTIONIST.value,
    }
    if user_role not in allowed_roles:
        abort(403)

    try:
        session_tenant_uuid = uuid.UUID(str(session_tenant_id))
    except (ValueError, TypeError):
        session.clear()
        return redirect(url_for("auth.login"))

    tenant = db.session.get(Tenant, session_tenant_uuid)
    if not tenant or not tenant.is_active:
        session.clear()
        return redirect(url_for("auth.login"))

    # Prevent cross-tenant access if a slug is passed in query/form params
    requested_slug = (request.args.get("slug") or request.form.get("slug") or "").strip()
    if requested_slug and requested_slug != tenant.slug:
        abort(403, description="Cross-tenant access is forbidden.")

    g.current_tenant = tenant


def _resolve_tenant(resource_id: uuid.UUID | None = None) -> Tenant:
    if hasattr(g, "current_tenant") and g.current_tenant:
        return g.current_tenant

    sess_tid = session.get("tenant_id")
    if sess_tid:
        try:
            tenant = db.session.get(Tenant, uuid.UUID(str(sess_tid)))
            if tenant and tenant.is_active:
                g.current_tenant = tenant
                return tenant
        except (ValueError, TypeError):
            pass

    abort(403, description="Tenant context not found or unauthorized.")


@dashboard_bp.route("/dashboard", methods=["GET"])
@dashboard_bp.route("/dashboard/", methods=["GET"])
@dashboard_bp.route("/dashboard/turf", methods=["GET"], endpoint="turf_dashboard")
def dashboard_home():
    tenant = _resolve_tenant()
    if tenant.vertical == VerticalType.CLINIC:
        return redirect(url_for("clinic.queue_board", slug=tenant.slug))

    date_str = request.args.get("date")
    selected_date = date.today()
    if date_str:
        try:
            selected_date = date.fromisoformat(date_str)
        except ValueError:
            pass

    resources = list(
        db.session.execute(
            select(Resource).filter_by(tenant_id=tenant.id, is_active=True)
        ).scalars().all()
    )

    bookings = list(
        db.session.execute(
            select(Booking)
            .filter_by(tenant_id=tenant.id, slot_date=selected_date)
            .order_by(Booking.start_time)
        ).scalars().all()
    )

    # All active bookings across all dates for instant conflict detection in modals
    all_active_bookings = list(
        db.session.execute(
            select(Booking)
            .filter(
                Booking.tenant_id == tenant.id,
                Booking.status.in_([
                    BookingStatus.CONFIRMED,
                    BookingStatus.BLOCKED,
                    BookingStatus.PENDING_PAYMENT,
                    BookingStatus.IN_CONSULTATION,
                ]),
            )
            .order_by(Booking.slot_date, Booking.start_time)
        ).scalars().all()
    )

    confirmed_bookings = [b for b in bookings if b.status == BookingStatus.CONFIRMED]
    blocked_bookings = [b for b in bookings if b.status == BookingStatus.BLOCKED]

    confirmed_count = len(confirmed_bookings)
    blocked_count = len(blocked_bookings)

    booking_ids = [b.id for b in confirmed_bookings]
    payments = []
    if booking_ids:
        payments = list(
            db.session.execute(
                select(Payment).filter(
                    Payment.tenant_id == tenant.id,
                    Payment.booking_id.in_(booking_ids),
                    Payment.status == "CAPTURED",
                )
            ).scalars().all()
        )

    online_upi_collected_paise = sum(
        p.amount_paise for p in payments if p.mode == PaymentMode.UPI_ONLINE
    )
    cash_collected_paise = sum(
        p.amount_paise for p in payments if p.mode in (PaymentMode.CASH, PaymentMode.UPI_VENUE, PaymentMode.CARD)
    )
    total_collected_paise = sum(b.paid_amount_paise for b in confirmed_bookings)
    if payments and total_collected_paise == 0:
        total_collected_paise = online_upi_collected_paise + cash_collected_paise

    pending_venue_balance_paise = sum(
        max(0, b.total_amount_paise - b.paid_amount_paise) for b in confirmed_bookings
    )

    total_capacity_slots = max(1, len(resources) * 17)
    occupancy_pct = min(100, int(round(((confirmed_count + blocked_count) / total_capacity_slots) * 100)))

    return render_template(
        "dashboard/turf_dashboard.html",
        tenant=tenant,
        resources=resources,
        selected_date=selected_date,
        bookings=bookings,
        all_active_bookings=all_active_bookings,
        confirmed_count=confirmed_count,
        blocked_count=blocked_count,
        total_collected_paise=total_collected_paise,
        online_upi_collected_paise=online_upi_collected_paise,
        cash_collected_paise=cash_collected_paise,
        pending_venue_balance_paise=pending_venue_balance_paise,
        occupancy_pct=occupancy_pct,
        total_capacity_slots=total_capacity_slots,
    )


@dashboard_bp.route("/dashboard/manual-booking", methods=["POST"])
def manual_booking():
    try:
        resource_id = uuid.UUID(request.form.get("resource_id", ""))
        slot_date = date.fromisoformat(request.form.get("slot_date", ""))
        start_t = time.fromisoformat(request.form.get("start_time", ""))
        end_t = time.fromisoformat(request.form.get("end_time", ""))
    except ValueError:
        flash("Invalid booking date or time parameters.", "error")
        return redirect(url_for("dashboard.dashboard_home"))

    tenant = _resolve_tenant(resource_id=resource_id)

    if end_t <= start_t:
        flash("End time must be after start time. Please choose a valid slot.", "error")
        return redirect(url_for("dashboard.dashboard_home", slug=tenant.slug, date=slot_date.isoformat()))

    cleaned_name, name_err = sanitize_and_validate_name(request.form.get("name"))
    if name_err:
        flash(name_err, "error")
        return redirect(url_for("dashboard.dashboard_home", slug=tenant.slug, date=slot_date.isoformat()))

    cleaned_phone, phone_err = sanitize_and_validate_phone(request.form.get("phone"))
    if phone_err:
        flash(phone_err, "error")
        return redirect(url_for("dashboard.dashboard_home", slug=tenant.slug, date=slot_date.isoformat()))

    raw_inr = request.form.get("advance_paid_inr", "").strip()
    if raw_inr != "":
        try:
            advance_paid_paise = max(0, int(float(raw_inr) * 100))
        except ValueError:
            advance_paid_paise = 0
    else:
        try:
            advance_paid_paise = max(0, int(request.form.get("advance_paid_paise", "0") or 0))
        except ValueError:
            advance_paid_paise = 0

    payment_mode_str = request.form.get("payment_mode", "CASH")
    booking_notes = request.form.get("booking_notes", "").strip()

    resource = db.session.execute(
        select(Resource).filter_by(id=resource_id, tenant_id=tenant.id)
    ).scalar_one_or_none()
    if not resource:
        abort(404, description="Selected court/resource not found or does not belong to this tenant.")

    now_utc = datetime.now(timezone.utc)
    existing_conflicts = list(
        db.session.execute(
            select(Booking).filter(
                Booking.tenant_id == tenant.id,
                Booking.resource_id == resource.id,
                Booking.slot_date == slot_date,
                Booking.start_time < end_t,
                Booking.end_time > start_t,
            )
        ).scalars().all()
    )

    for b in existing_conflicts:
        if b.status in (BookingStatus.CONFIRMED, BookingStatus.BLOCKED, BookingStatus.IN_CONSULTATION):
            flash(
                f"Slot Unavailable: {resource.name} ({b.start_time.strftime('%H:%M')} - {b.end_time.strftime('%H:%M')}) "
                f"is already {b.status.value}. Please select a different slot.",
                "error",
            )
            return redirect(url_for("dashboard.dashboard_home", slug=tenant.slug, date=slot_date.isoformat()))
        if b.status == BookingStatus.PENDING_PAYMENT and b.locked_until:
            locked_until = b.locked_until if b.locked_until.tzinfo else b.locked_until.replace(tzinfo=timezone.utc)
            if locked_until > now_utc:
                flash(
                    f"Slot Unavailable: {resource.name} ({b.start_time.strftime('%H:%M')}) is currently locked by an online customer.",
                    "error",
                )
                return redirect(url_for("dashboard.dashboard_home", slug=tenant.slug, date=slot_date.isoformat()))
            else:
                b.status = BookingStatus.EXPIRED
                b.locked_until = None

    customer = db.session.execute(
        select(Customer).filter_by(tenant_id=tenant.id, phone=cleaned_phone)
    ).scalar_one_or_none()
    if not customer:
        customer = Customer(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name=cleaned_name,
            phone=cleaned_phone,
            dpdp_consent_given=True,
            dpdp_consent_at=now_utc,
        )
        db.session.add(customer)
        db.session.flush()
    else:
        customer.name = cleaned_name

    price_info = calculate_slot_price(
        resource=resource,
        slot_date=slot_date,
        start_time=start_t,
        end_time=end_t,
    )

    new_booking = Booking(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        resource_id=resource.id,
        customer_id=customer.id,
        slot_date=slot_date,
        start_time=start_t,
        end_time=end_t,
        status=BookingStatus.CONFIRMED,
        is_walkin=True,
        total_amount_paise=price_info.price_paise,
        paid_amount_paise=advance_paid_paise,
        booking_notes=booking_notes or f"Manual phone booking ({cleaned_name})",
    )
    db.session.add(new_booking)
    db.session.flush()

    if advance_paid_paise > 0:
        payment = Payment(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            booking_id=new_booking.id,
            razorpay_order_id=f"venue_cash_{uuid.uuid4().hex[:8]}",
            amount_paise=advance_paid_paise,
            mode=PaymentMode[payment_mode_str] if payment_mode_str in PaymentMode.__members__ else PaymentMode.CASH,
            status="CAPTURED",
        )
        db.session.add(payment)

    try:
        db.session.commit()
        # Send WhatsApp booking confirmation notification
        NotifierService().send_notification(
            tenant=tenant,
            recipient_phone=customer.phone,
            template_name="booking_confirmed",
            parameters={
                "customer_name": customer.name,
                "resource_name": resource.name,
                "slot_date": slot_date.strftime("%d %b %Y"),
                "start_time": f"{start_t.strftime('%H:%M')} - {end_t.strftime('%H:%M')}",
                "venue_name": tenant.name,
                "balance_inr": max(0, (new_booking.total_amount_paise - new_booking.paid_amount_paise) // 100),
            },
            booking_id=new_booking.id,
            customer_id=customer.id,
        )
        flash(
            f"Manual booking confirmed for {cleaned_name} ({start_t.strftime('%H:%M')} - {end_t.strftime('%H:%M')}) & WhatsApp confirmation sent.",
            "success",
        )
    except IntegrityError:
        db.session.rollback()
        flash(
            f"Slot {start_t.strftime('%H:%M')} - {end_t.strftime('%H:%M')} on {resource.name} is already occupied. Please choose another slot.",
            "error",
        )

    return redirect(url_for("dashboard.dashboard_home", slug=tenant.slug, date=slot_date.isoformat()))


@dashboard_bp.route("/dashboard/block-slot", methods=["POST"])
def block_slot():
    try:
        resource_id = uuid.UUID(request.form.get("resource_id", ""))
        slot_date = date.fromisoformat(request.form.get("slot_date", ""))
        start_t = time.fromisoformat(request.form.get("start_time", ""))
        end_t = time.fromisoformat(request.form.get("end_time", ""))
    except ValueError:
        flash("Invalid block date or time parameters.", "error")
        return redirect(url_for("dashboard.dashboard_home"))

    tenant = _resolve_tenant(resource_id=resource_id)
    reason = request.form.get("reason", "Maintenance Block").strip() or "Maintenance Block"
    force_cancel = request.form.get("force_cancel_and_block", "").lower() == "true"

    if end_t <= start_t:
        flash("Block end time must be after start time.", "error")
        return redirect(url_for("dashboard.dashboard_home", slug=tenant.slug, date=slot_date.isoformat()))

    resource = db.session.execute(
        select(Resource).filter_by(id=resource_id, tenant_id=tenant.id)
    ).scalar_one_or_none()
    if not resource:
        abort(404, description="Selected court/resource not found or does not belong to this tenant.")

    # Find any overlapping CONFIRMED or PENDING_PAYMENT customer bookings in this window
    overlapping_customer_bookings = list(
        db.session.execute(
            select(Booking).filter(
                Booking.tenant_id == tenant.id,
                Booking.resource_id == resource.id,
                Booking.slot_date == slot_date,
                Booking.start_time < end_t,
                Booking.end_time > start_t,
                Booking.status.in_([
                    BookingStatus.CONFIRMED,
                    BookingStatus.PENDING_PAYMENT,
                    BookingStatus.IN_CONSULTATION,
                ]),
            )
        ).scalars().all()
    )

    if overlapping_customer_bookings and not force_cancel:
        first_b = overlapping_customer_bookings[0]
        c_name = first_b.customer.name if first_b.customer else "a customer"
        c_phone = f" (+91 {first_b.customer.phone})" if first_b.customer else ""
        flash(
            f"Slot {first_b.start_time.strftime('%H:%M')} - {first_b.end_time.strftime('%H:%M')} is already booked by {c_name}{c_phone}. "
            f"Please contact the client and use the Master Slide in the Block Slot modal to cancel & block.",
            "error",
        )
        return redirect(url_for("dashboard.dashboard_home", slug=tenant.slug, date=slot_date.isoformat()))

    cancelled_customers_notified = []
    if overlapping_customer_bookings and force_cancel:
        notifier = NotifierService()
        for cb in overlapping_customer_bookings:
            cb.status = BookingStatus.CANCELLED
            cb.locked_until = None
            cb.booking_notes = f"{cb.booking_notes or ''} | Cancelled by venue for blockout: {reason}".strip(" |")
            if cb.customer:
                notifier.send_notification(
                    tenant=tenant,
                    recipient_phone=cb.customer.phone,
                    template_name="booking_cancelled",
                    parameters={
                        "customer_name": cb.customer.name,
                        "resource_name": resource.name,
                        "slot_date": slot_date.strftime("%d %b %Y"),
                        "start_time": f"{cb.start_time.strftime('%H:%M')} - {cb.end_time.strftime('%H:%M')}",
                        "reason": reason,
                        "venue_name": tenant.name,
                        "refund_inr": cb.paid_amount_paise // 100,
                    },
                    booking_id=cb.id,
                    customer_id=cb.customer.id,
                )
                cancelled_customers_notified.append(f"{cb.customer.name} (+91 {cb.customer.phone})")
        # Flush cancellations immediately so idx_unique_confirmed_slot is freed before inserting BLOCKED rows
        db.session.flush()

    # Remove any existing BLOCKED rows in this window so multi-hour blocks split cleanly
    existing_blocks = list(
        db.session.execute(
            select(Booking).filter(
                Booking.tenant_id == tenant.id,
                Booking.resource_id == resource.id,
                Booking.slot_date == slot_date,
                Booking.start_time < end_t,
                Booking.end_time > start_t,
                Booking.status == BookingStatus.BLOCKED,
            )
        ).scalars().all()
    )
    for eb in existing_blocks:
        db.session.delete(eb)
    db.session.flush()

    step_mins = resource.slot_duration_mins or 60
    curr_t = start_t
    blocked_slots_created = 0

    while curr_t < end_t:
        next_dt = datetime.combine(slot_date, curr_t) + timedelta(minutes=step_mins)
        next_t = min(next_dt.time(), end_t) if next_dt.date() == slot_date else end_t
        if next_t <= curr_t:
            break

        b_block = Booking(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            resource_id=resource.id,
            slot_date=slot_date,
            start_time=curr_t,
            end_time=next_t,
            status=BookingStatus.BLOCKED,
            is_walkin=False,
            total_amount_paise=0,
            paid_amount_paise=0,
            booking_notes=reason,
        )
        db.session.add(b_block)
        blocked_slots_created += 1
        curr_t = next_t

    try:
        db.session.commit()
        if cancelled_customers_notified:
            names_str = ", ".join(cancelled_customers_notified)
            flash(
                f"Cancelled booking for {names_str} (WhatsApp cancellation alert sent) and blocked {blocked_slots_created} slot(s) for: {reason}",
                "success",
            )
        elif blocked_slots_created > 0:
            flash(
                f"Blocked {blocked_slots_created} slot(s) ({start_t.strftime('%H:%M')} - {end_t.strftime('%H:%M')}) for: {reason}",
                "success",
            )
        else:
            flash("No slots were blocked.", "error")
    except IntegrityError:
        db.session.rollback()
        flash("Could not block slot because one or more slots in that range are already occupied.", "error")

    return redirect(url_for("dashboard.dashboard_home", slug=tenant.slug, date=slot_date.isoformat()))


@dashboard_bp.route("/dashboard/unblock-slot/<uuid:booking_id>", methods=["POST"])
def unblock_slot(booking_id: uuid.UUID):
    tenant = _resolve_tenant()
    booking = db.session.execute(
        select(Booking).filter_by(id=booking_id, tenant_id=tenant.id)
    ).scalar_one_or_none()
    if not booking:
        abort(404, description="Blocked slot not found or access denied.")

    slot_date_str = booking.slot_date.isoformat()
    db.session.delete(booking)
    db.session.commit()
    flash("Slot unblocked successfully", "success")
    return redirect(url_for("dashboard.dashboard_home", slug=tenant.slug, date=slot_date_str))


@dashboard_bp.route("/dashboard/qr-poster", methods=["GET"])
def qr_poster():
    tenant = _resolve_tenant()
    booking_url = request.host_url.rstrip("/") + url_for("public.booking_page", slug=tenant.slug)

    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(booking_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf)
    qr_data_uri = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"

    return render_template(
        "dashboard/qr_poster.html",
        tenant=tenant,
        qr_data_uri=qr_data_uri,
        booking_url=booking_url,
    )


@dashboard_bp.route("/dashboard/onboard", methods=["GET", "POST"])
def onboard_tenant():
    if request.method == "GET":
        return render_template("dashboard/onboard.html")

    vertical_str = request.form.get("vertical", "TURF")
    slug = request.form.get("slug", "").strip().lower()
    business_name = request.form.get("business_name", "").strip()
    owner_name, name_err = sanitize_and_validate_name(request.form.get("owner_name"))
    phone, phone_err = sanitize_and_validate_phone(request.form.get("phone"))
    address = request.form.get("address", "").strip()
    resource_name = request.form.get("resource_name", "").strip()
    base_price_inr = int(request.form.get("base_price_inr", "800") or 800)
    peak_price_inr = int(request.form.get("peak_price_inr", "1200") or 1200)
    advance_pct = int(request.form.get("advance_percentage", "30") or 30)
    rzp_key_id = request.form.get("razorpay_key_id", "").strip() or None
    rzp_secret = request.form.get("razorpay_key_secret", "").strip()

    if name_err or phone_err or not slug or not business_name:
        flash(name_err or phone_err or "Please fill all required onboarding fields.", "error")
        return redirect(url_for("dashboard.onboard_tenant"))

    existing = db.session.execute(select(Tenant).filter_by(slug=slug)).scalar_one_or_none()
    if existing:
        flash(f"Slug '{slug}' is already taken. Please choose another URL slug.", "error")
        return redirect(url_for("dashboard.onboard_tenant"))

    vertical = VerticalType.CLINIC if vertical_str == "CLINIC" else VerticalType.TURF
    tenant = Tenant(
        id=uuid.uuid4(),
        slug=slug,
        name=business_name,
        vertical=vertical,
        phone=phone,
        address=address,
        payment_mode_config="RAZORPAY_OWN" if (rzp_key_id and rzp_secret) else "VENUE_ONLY",
        razorpay_key_id=rzp_key_id,
        razorpay_key_secret_enc=encrypt_secret(rzp_secret) if rzp_secret else None,
        advance_percentage=advance_pct,
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
    owner.set_password("password123")
    db.session.add(owner)

    res = Resource(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name=resource_name,
        category="Consultation" if vertical == VerticalType.CLINIC else "Turf",
        slot_duration_mins=20 if vertical == VerticalType.CLINIC else 60,
        base_price_paise=base_price_inr * 100,
    )
    db.session.add(res)
    db.session.flush()

    for day in range(7):
        db.session.add(
            AvailabilityRule(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                resource_id=res.id,
                day_of_week=day,
                start_time=time(6, 0) if vertical == VerticalType.TURF else time(9, 0),
                end_time=time(17, 0) if vertical == VerticalType.TURF else time(20, 0),
                is_break=False,
                price_override_paise=base_price_inr * 100,
            )
        )
        if vertical == VerticalType.TURF:
            db.session.add(
                AvailabilityRule(
                    id=uuid.uuid4(),
                    tenant_id=tenant.id,
                    resource_id=res.id,
                    day_of_week=day,
                    start_time=time(17, 0),
                    end_time=time(23, 0),
                    is_break=False,
                    price_override_paise=peak_price_inr * 100,
                )
            )

    db.session.commit()
    flash(f"Business '{business_name}' launched successfully!", "success")
    return redirect(url_for("dashboard.qr_poster", slug=tenant.slug))