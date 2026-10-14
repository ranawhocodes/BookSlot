# app/blueprints/public/routes.py
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
    abort,
    jsonify,
    make_response,
)
import qrcode
from sqlalchemy import select
from app.extensions import db
from app.models import (
    Tenant,
    Resource,
    Customer,
    Booking,
    BookingStatus,
    Payment,
    PaymentMode,
    Invoice,
)
from app.services.slot_engine import (
    get_available_slots,
    create_slot_hold,
    confirm_booking,
    SlotUnavailableError,
)
from app.services.pricing import calculate_booking_quote
from app.services.payment_gw import (
    create_payment_order,
    resolve_tenant_razorpay_credentials,
    verify_razorpay_checkout_signature,
)
from app.services.notifier import NotifierService
from app.translations import get_text, TAMIL_DAYS, TAMIL_MONTHS
from app.utils.validators import (
    sanitize_and_validate_phone,
    sanitize_and_validate_name,
    check_honeypot,
    rate_limit,
)

public_bp = Blueprint("public", __name__)


def _get_tenant_or_404(slug: str) -> Tenant:
    tenant = db.session.execute(
        select(Tenant).filter_by(slug=slug, is_active=True)
    ).scalar_one_or_none()
    if not tenant:
        abort(404, description=f"Venue '{slug}' not found.")
    return tenant


def _build_upcoming_dates(selected_date: date, lang: str = "en") -> list[dict]:
    today = date.today()
    start_base = min(today, selected_date)
    dates = []
    for offset in range(7):
        d = start_base + timedelta(days=offset)
        en_day = d.strftime("%a")
        en_month = d.strftime("%b")
        if d == today:
            sub_label = get_text("today", lang)
        elif d == today + timedelta(days=1):
            sub_label = get_text("tomorrow", lang)
        else:
            sub_label = TAMIL_DAYS.get(en_day, en_day) if lang == "ta" else en_day

        dates.append(
            {
                "date": d.isoformat(),
                "day_name": TAMIL_DAYS.get(en_day, en_day) if lang == "ta" else en_day,
                "day_number": d.strftime("%d"),
                "month_name": TAMIL_MONTHS.get(en_month, en_month) if lang == "ta" else en_month,
                "sub_label": sub_label,
            }
        )
    return dates


@public_bp.route("/b/<slug>", methods=["GET"])
def booking_page(slug: str):
    tenant = _get_tenant_or_404(slug)
    lang = request.args.get("lang", "en")
    if lang not in ("en", "ta"):
        lang = "en"

    resources = db.session.execute(
        select(Resource).filter_by(tenant_id=tenant.id, is_active=True)
    ).scalars().all()

    if not resources:
        return render_template("public/no_resources.html", tenant=tenant)

    res_id_str = request.args.get("resource_id")
    selected_resource = resources[0]
    if res_id_str:
        try:
            res_uuid = uuid.UUID(res_id_str)
            matched = next((r for r in resources if r.id == res_uuid), None)
            if matched:
                selected_resource = matched
        except ValueError:
            pass

    date_str = request.args.get("date")
    selected_date = date.today()
    if date_str:
        try:
            selected_date = date.fromisoformat(date_str)
        except ValueError:
            pass

    slots = get_available_slots(tenant.id, selected_resource.id, selected_date)
    upcoming_dates = _build_upcoming_dates(selected_date, lang=lang)

    return render_template(
        "public/booking.html",
        tenant=tenant,
        resources=resources,
        selected_resource=selected_resource,
        selected_date=selected_date,
        upcoming_dates=upcoming_dates,
        slots=slots,
        resource=selected_resource,
        lang=lang,
        t=lambda key, **kw: get_text(key, lang=lang, **kw),
    )


@public_bp.route("/b/<slug>/slots", methods=["GET"])
def slots_partial(slug: str):
    tenant = _get_tenant_or_404(slug)
    lang = request.args.get("lang", "en")
    res_id_str = request.args.get("resource_id")
    date_str = request.args.get("date")

    resources = db.session.execute(
        select(Resource).filter_by(tenant_id=tenant.id, is_active=True)
    ).scalars().all()
    if not resources:
        return "", 200

    selected_resource = resources[0]
    if res_id_str:
        try:
            res_uuid = uuid.UUID(res_id_str)
            matched = next((r for r in resources if r.id == res_uuid), None)
            if matched:
                selected_resource = matched
        except ValueError:
            pass

    selected_date = date.today()
    if date_str:
        try:
            selected_date = date.fromisoformat(date_str)
        except ValueError:
            pass

    slots = get_available_slots(tenant.id, selected_resource.id, selected_date)

    return render_template(
        "public/_slots_grid.html",
        tenant=tenant,
        resource=selected_resource,
        selected_date=selected_date,
        slots=slots,
        lang=lang,
        t=lambda key, **kw: get_text(key, lang=lang, **kw),
    )


@public_bp.route("/b/<slug>/hold", methods=["POST"])
@rate_limit(max_requests=5, window_seconds=60, key_prefix="public_hold")
def hold_slot_and_checkout(slug: str):
    tenant = _get_tenant_or_404(slug)

    # 1. Anti-bot Honeypot check
    if check_honeypot():
        return "Bot submission blocked.", 400

    # 2. Enforce explicit DPDP Act 2023 consent
    dpdp_consent = request.form.get("dpdp_consent", "").lower()
    if dpdp_consent not in ("on", "true", "1", "yes"):
        return (
            '<div class="p-4 rounded-xl bg-rose-950/90 border border-rose-500/40 text-rose-200 text-xs font-semibold">'
            'Explicit DPDP Act 2023 consent is required to book a slot.</div>',
            400,
        )

    # 3. Validate & Sanitize Customer Name and 10-digit Indian Mobile Number
    clean_name, name_err = sanitize_and_validate_name(request.form.get("name"))
    if name_err:
        return (
            f'<div class="p-4 rounded-xl bg-rose-950/90 border border-rose-500/40 text-rose-200 text-xs font-semibold">'
            f'{name_err}</div>',
            400,
        )

    clean_phone, phone_err = sanitize_and_validate_phone(request.form.get("phone"))
    if phone_err:
        return (
            f'<div class="p-4 rounded-xl bg-rose-950/90 border border-rose-500/40 text-rose-200 text-xs font-semibold">'
            f'{phone_err}</div>',
            400,
        )

    try:
        resource_id = uuid.UUID(request.form.get("resource_id", ""))
        slot_date = date.fromisoformat(request.form.get("date", ""))
        start_t = time.fromisoformat(request.form.get("start_time", ""))
        end_t = time.fromisoformat(request.form.get("end_time", ""))
    except ValueError:
        return "Invalid slot parameters.", 400

    resource = db.session.execute(
        select(Resource).filter_by(id=resource_id, tenant_id=tenant.id)
    ).scalar_one_or_none()
    if not resource:
        return "Resource not found.", 404

    # 4. Upsert Customer with DPDP Consent timestamp
    customer = db.session.execute(
        select(Customer).filter_by(tenant_id=tenant.id, phone=clean_phone)
    ).scalar_one_or_none()

    now_utc = datetime.now(timezone.utc)
    if not customer:
        customer = Customer(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name=clean_name,
            phone=clean_phone,
            dpdp_consent_given=True,
            dpdp_consent_at=now_utc,
        )
        db.session.add(customer)
        db.session.flush()
    else:
        customer.name = clean_name
        customer.dpdp_consent_given = True
        customer.dpdp_consent_at = now_utc
        db.session.flush()

    # 5. Calculate Quote & Create 7-Minute Atomic Hold
    quote = calculate_booking_quote(
        resource=resource,
        slot_date=slot_date,
        slot_intervals=[(start_t, end_t)],
        tenant=tenant,
    )
    payment_choice = request.form.get("payment_choice", "advance")
    payable_paise = (
        quote.advance_amount_paise if payment_choice == "advance" else quote.total_amount_paise
    )

    try:
        booking = create_slot_hold(
            tenant_id=tenant.id,
            resource_id=resource.id,
            slot_date=slot_date,
            start_time=start_t,
            end_time=end_t,
            customer_id=customer.id,
            total_amount_paise=quote.total_amount_paise,
            hold_duration_minutes=7,
        )
    except SlotUnavailableError as exc:
        return (
            f'<div class="p-4 rounded-xl bg-amber-950/90 border border-amber-500/40 text-amber-200 text-xs font-semibold">'
            f'{str(exc)}</div>',
            409,
        )

    # 6. Create Payment Order (Razorpay Live / UPI Intent Deep-Links)
    order_data = create_payment_order(
        tenant=tenant,
        booking=booking,
        amount_paise=payable_paise,
    )

    payment_record = Payment(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        booking_id=booking.id,
        razorpay_order_id=order_data.order_id,
        amount_paise=payable_paise,
        mode=PaymentMode.UPI_ONLINE,
        status="CREATED",
    )
    db.session.add(payment_record)
    db.session.commit()

    return render_template(
        "public/_checkout_payment_step.html",
        tenant=tenant,
        resource=resource,
        booking=booking,
        customer=customer,
        quote=quote,
        payable_paise=payable_paise,
        order_data=order_data,
    )


@public_bp.route("/b/<slug>/verify-payment", methods=["POST"])
def verify_live_razorpay_payment(slug: str):
    """
    Verifies Razorpay Standard Checkout signature after live UPI/Card payment
    and confirms the booking idempotently.
    """
    tenant = _get_tenant_or_404(slug)
    booking_id_str = request.form.get("booking_id", "")
    razorpay_order_id = request.form.get("razorpay_order_id", "")
    razorpay_payment_id = request.form.get("razorpay_payment_id", "")
    razorpay_signature = request.form.get("razorpay_signature", "")

    _, key_secret = resolve_tenant_razorpay_credentials(tenant)
    if not key_secret:
        return jsonify({"error": "Gateway secret not configured"}), 400

    if not verify_razorpay_checkout_signature(
        order_id=razorpay_order_id,
        payment_id=razorpay_payment_id,
        signature=razorpay_signature,
        key_secret=key_secret,
    ):
        return jsonify({"error": "Invalid payment signature"}), 400

    booking = db.session.execute(
        select(Booking).filter_by(id=uuid.UUID(booking_id_str), tenant_id=tenant.id)
    ).scalar_one_or_none()
    if not booking:
        return jsonify({"error": "Booking not found"}), 404

    payment_rec = db.session.execute(
        select(Payment).filter_by(booking_id=booking.id, razorpay_order_id=razorpay_order_id)
    ).scalar_one_or_none()

    paid_paise = payment_rec.amount_paise if payment_rec else booking.total_amount_paise
    if payment_rec and payment_rec.status != "CAPTURED":
        payment_rec.status = "CAPTURED"
        payment_rec.razorpay_payment_id = razorpay_payment_id

    confirm_booking(
        tenant_id=tenant.id,
        booking_id=booking.id,
        paid_amount_paise=paid_paise,
    )

    # Send WhatsApp confirmation
    if booking.customer:
        NotifierService().send_notification(
            tenant=tenant,
            recipient_phone=booking.customer.phone,
            template_name="booking_confirmed",
            parameters={
                "customer_name": booking.customer.name,
                "resource_name": booking.resource.name,
                "slot_date": booking.slot_date.strftime("%d %b %Y"),
                "start_time": booking.start_time.strftime("%I:%M %p"),
                "venue_name": tenant.name,
                "balance_inr": max(0, (booking.total_amount_paise - booking.paid_amount_paise) // 100),
            },
            booking_id=booking.id,
            customer_id=booking.customer.id,
        )

    conf_url = url_for("public.confirmation_page", slug=tenant.slug, booking_id=booking.id)
    if request.headers.get("HX-Request"):
        resp = make_response("", 200)
        resp.headers["HX-Redirect"] = conf_url
        return resp
    return redirect(conf_url)


@public_bp.route("/b/<slug>/pay-mock", methods=["POST"])
def pay_mock(slug: str):
    """Completes UPI Intent / Sandbox payment confirmation and triggers WhatsApp alert."""
    tenant = _get_tenant_or_404(slug)
    booking_id = uuid.UUID(request.form.get("booking_id", ""))
    amount_paise = int(request.form.get("amount_paise", "0"))
    utr_ref = request.form.get("utr_number", "").strip()

    booking = confirm_booking(
        tenant_id=tenant.id,
        booking_id=booking_id,
        paid_amount_paise=amount_paise,
    )

    payment_rec = db.session.execute(
        select(Payment).filter_by(booking_id=booking.id, tenant_id=tenant.id)
    ).scalars().first()

    pay_id = f"pay_upi_{utr_ref}" if utr_ref else f"pay_mock_{uuid.uuid4().hex[:10]}"
    if payment_rec:
        payment_rec.status = "CAPTURED"
        payment_rec.razorpay_payment_id = pay_id
        payment_rec.webhook_payload = {"gateway": "upi_intent", "status": "captured", "utr": utr_ref}
    else:
        payment_rec = Payment(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            booking_id=booking.id,
            razorpay_order_id=f"order_mock_{booking.id.hex[:10]}",
            razorpay_payment_id=pay_id,
            amount_paise=amount_paise,
            mode=PaymentMode.UPI_ONLINE,
            status="CAPTURED",
            webhook_payload={"gateway": "upi_intent", "status": "captured"},
        )
        db.session.add(payment_rec)
    db.session.commit()

    if booking.customer:
        NotifierService().send_notification(
            tenant=tenant,
            recipient_phone=booking.customer.phone,
            template_name="booking_confirmed",
            parameters={
                "customer_name": booking.customer.name,
                "resource_name": booking.resource.name,
                "slot_date": booking.slot_date.strftime("%d %b %Y"),
                "start_time": booking.start_time.strftime("%I:%M %p"),
                "venue_name": tenant.name,
                "balance_inr": max(0, (booking.total_amount_paise - booking.paid_amount_paise) // 100),
            },
            booking_id=booking.id,
            customer_id=booking.customer.id,
        )

    conf_url = url_for("public.confirmation_page", slug=tenant.slug, booking_id=booking.id)
    if request.headers.get("HX-Request"):
        resp = make_response(f'<script>window.location.href = "{conf_url}";</script>', 200)
        resp.headers["HX-Redirect"] = conf_url
        return resp
    return jsonify({
        "status": "success",
        "booking_id": str(booking.id),
        "payment_id": str(payment_rec.id),
        "redirect_url": conf_url,
    }), 200


@public_bp.route("/b/<slug>/confirmation/<uuid:booking_id>", methods=["GET"])
def confirmation_page(slug: str, booking_id: uuid.UUID):
    tenant = _get_tenant_or_404(slug)
    booking = db.session.execute(
        select(Booking).filter_by(id=booking_id, tenant_id=tenant.id)
    ).scalar_one_or_none()
    if not booking:
        abort(404)

    balance_paise = max(0, booking.total_amount_paise - booking.paid_amount_paise)

    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(f"SLOTENGINE-PASS:{booking.id.hex[:8].upper()}:{tenant.slug}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf)
    qr_data_uri = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"

    return render_template(
        "public/confirmation.html",
        tenant=tenant,
        booking=booking,
        balance_paise=balance_paise,
        qr_data_uri=qr_data_uri,
    )


@public_bp.route("/i/<uuid:invoice_id>", methods=["GET"])
def public_invoice_receipt(invoice_id: uuid.UUID):
    """
    Public shareable invoice short-link (/i/<invoice_id>) so clinic patients can view
    and print their itemized PDF receipt directly from WhatsApp without logging in.
    """
    invoice = db.session.execute(
        select(Invoice).filter_by(id=invoice_id)
    ).scalar_one_or_none()
    if not invoice:
        abort(404, description="Invoice receipt not found.")

    return render_template(
        "clinic/invoice_receipt.html",
        tenant=invoice.tenant,
        invoice=invoice,
        is_public_link=True,
    )