# app/blueprints/clinic/routes.py
import csv
import io
import urllib.parse
import uuid
from datetime import date, datetime, time, timezone
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    abort,
    session,
    Response,
    current_app,
)
from sqlalchemy import select, func, or_
from app.extensions import db
from app.models import (
    Tenant,
    Resource,
    Customer,
    Booking,
    BookingStatus,
    ClinicVisit,
    Invoice,
    AuditLog,
    UserRole,
    PaymentMode,
)
from app.middleware.tenant import resolve_current_tenant
from app.middleware.auth import require_role
from app.services.notifier import NotifierService
from app.utils.validators import sanitize_and_validate_name, sanitize_and_validate_phone, sanitize_csv_row

clinic_bp = Blueprint("clinic", __name__, url_prefix="/dashboard/clinic")


def _get_current_role() -> str:
    """Returns the effective role for the current user/session."""
    role = session.get("user_role")
    if not role and current_app.config.get("DEBUG"):
        return UserRole.OWNER.value
    return role or "RECEPTIONIST"


@clinic_bp.route("/queue")
@require_role(UserRole.OWNER, UserRole.DOCTOR, UserRole.RECEPTIONIST)
def queue_board():
    """Hybrid Appointment + Walk-In Token Queue Board."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404, description="No active clinic tenant found.")

    date_str = request.args.get("date")
    try:
        selected_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        selected_date = date.today()

    doctor_id_str = request.args.get("doctor_id")
    selected_doctor_id = None
    if doctor_id_str:
        try:
            selected_doctor_id = uuid.UUID(doctor_id_str)
        except ValueError:
            pass

    doctors = db.session.execute(
        select(Resource).filter(
            Resource.tenant_id == tenant.id,
            Resource.is_active.is_(True)
        )
    ).scalars().all()

    query = select(Booking).filter(
        Booking.tenant_id == tenant.id,
        Booking.slot_date == selected_date,
        Booking.status.in_([
            BookingStatus.CONFIRMED,
            BookingStatus.IN_CONSULTATION,
            BookingStatus.COMPLETED,
            BookingStatus.NO_SHOW,
        ])
    )
    if selected_doctor_id:
        query = query.filter(Booking.resource_id == selected_doctor_id)

    all_queue = db.session.execute(query).scalars().all()

    def queue_sort_key(b: Booking):
        status_rank = {
            BookingStatus.IN_CONSULTATION: 0,
            BookingStatus.CONFIRMED: 1,
            BookingStatus.COMPLETED: 2,
            BookingStatus.NO_SHOW: 3,
        }.get(b.status, 4)
        token_rank = b.token_number if b.token_number is not None else 9999
        time_minutes = b.start_time.hour * 60 + b.start_time.minute
        return (status_rank, token_rank, time_minutes)

    queue_entries = sorted(all_queue, key=queue_sort_key)

    waiting_count = sum(1 for b in queue_entries if b.status == BookingStatus.CONFIRMED)
    in_consultation_count = sum(1 for b in queue_entries if b.status == BookingStatus.IN_CONSULTATION)
    completed_count = sum(1 for b in queue_entries if b.status == BookingStatus.COMPLETED)
    no_show_count = sum(1 for b in queue_entries if b.status == BookingStatus.NO_SHOW)

    current_role = _get_current_role()

    return render_template(
        "clinic/queue_board.html",
        tenant=tenant,
        doctors=doctors,
        selected_doctor_id=selected_doctor_id,
        selected_date=selected_date,
        queue_entries=queue_entries,
        waiting_count=waiting_count,
        in_consultation_count=in_consultation_count,
        completed_count=completed_count,
        no_show_count=no_show_count,
        current_role=current_role,
    )


def _find_next_walkin_slot(tenant_id: uuid.UUID, doctor: Resource, slot_date: date) -> tuple[time, time]:
    """Finds the next unassigned time slot for a walk-in token on slot_date."""
    occupied_times = set(
        db.session.execute(
            select(Booking.start_time).filter(
                Booking.tenant_id == tenant_id,
                Booking.resource_id == doctor.id,
                Booking.slot_date == slot_date,
                Booking.status.in_([
                    BookingStatus.CONFIRMED,
                    BookingStatus.IN_CONSULTATION,
                    BookingStatus.BLOCKED,
                ])
            )
        ).scalars().all()
    )

    now_time = datetime.now().time()
    if slot_date == date.today():
        start_min_total = (now_time.hour * 60) + ((now_time.minute // 5) * 5)
    else:
        start_min_total = 9 * 60

    duration = doctor.slot_duration_mins or 20
    while start_min_total < 23 * 60 + 55:
        h = (start_min_total // 60) % 24
        m = start_min_total % 60
        cand_start = time(h, m)
        if cand_start not in occupied_times:
            end_min_total = start_min_total + duration
            end_h = (end_min_total // 60) % 24
            end_m = end_min_total % 60
            cand_end = time(end_h, end_m)
            return cand_start, cand_end
        start_min_total += duration

    return time(23, 50), time(23, 59)


@clinic_bp.route("/queue/walkin-token", methods=["POST"])
@require_role(UserRole.OWNER, UserRole.DOCTOR, UserRole.RECEPTIONIST)
def issue_walkin_token():
    """Generates the next sequential daily walk-in token (Token #1, #2, #3...)."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    doctor_id_str = request.form.get("doctor_resource_id")
    slot_date_str = request.form.get("slot_date")
    patient_name, name_err = sanitize_and_validate_name(request.form.get("patient_name"))
    patient_phone, phone_err = sanitize_and_validate_phone(request.form.get("patient_phone"))
    if name_err or phone_err:
        flash(name_err or phone_err, "error")
        return redirect(url_for("clinic.queue_board", slug=tenant.slug))

    booking_notes = request.form.get("booking_notes")

    try:
        doctor_resource_id = uuid.UUID(doctor_id_str)
        slot_date = date.fromisoformat(slot_date_str) if slot_date_str else date.today()
    except (ValueError, TypeError) as err:
        flash(f"Invalid input: {err}", "error")
        return redirect(url_for("clinic.queue_board", slug=tenant.slug))

    doctor = db.session.execute(
        select(Resource).filter_by(tenant_id=tenant.id, id=doctor_resource_id)
    ).scalar_one_or_none()
    if not doctor:
        flash("Doctor resource not found.", "error")
        return redirect(url_for("clinic.queue_board", slug=tenant.slug))

    customer = db.session.execute(
        select(Customer).filter_by(tenant_id=tenant.id, phone=patient_phone)
    ).scalar_one_or_none()

    now_utc = datetime.now(timezone.utc)
    if customer:
        customer.name = patient_name
    else:
        customer = Customer(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            name=patient_name,
            phone=patient_phone,
            dpdp_consent_given=True,
            dpdp_consent_at=now_utc,
        )
        db.session.add(customer)
    db.session.flush()

    token_number = Booking.generate_next_walkin_token(tenant.id, doctor.id, slot_date)

    start_t, end_t = _find_next_walkin_slot(tenant.id, doctor, slot_date)
    booking = Booking(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        resource_id=doctor.id,
        customer_id=customer.id,
        slot_date=slot_date,
        start_time=start_t,
        end_time=end_t,
        status=BookingStatus.CONFIRMED,
        locked_until=None,
        is_walkin=True,
        token_number=token_number,
        total_amount_paise=doctor.base_price_paise,
        paid_amount_paise=0,
        booking_notes=booking_notes or f"Walk-in Token #{token_number}",
    )
    db.session.add(booking)
    db.session.commit()

    flash(f"Token #{token_number} issued for {patient_name} with {doctor.name}!", "success")
    return redirect(url_for("clinic.queue_board", slug=tenant.slug, date=slot_date.isoformat(), doctor_id=str(doctor.id)))


@clinic_bp.route("/queue/<uuid:booking_id>/status", methods=["POST"])
@require_role(UserRole.OWNER, UserRole.DOCTOR, UserRole.RECEPTIONIST)
def update_queue_status(booking_id: uuid.UUID):
    """HTMX live transition endpoint: WAITING -> IN_CONSULTATION -> COMPLETED / NO_SHOW."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    booking = db.session.execute(
        select(Booking).filter_by(tenant_id=tenant.id, id=booking_id)
    ).scalar_one_or_none()
    if not booking:
        return "<p class='text-red-500'>Booking not found.</p>", 404

    new_status_str = request.form.get("status")
    status_map = {
        "WAITING": BookingStatus.CONFIRMED,
        "CONFIRMED": BookingStatus.CONFIRMED,
        "IN_CONSULTATION": BookingStatus.IN_CONSULTATION,
        "COMPLETED": BookingStatus.COMPLETED,
        "NO_SHOW": BookingStatus.NO_SHOW,
    }
    if new_status_str in status_map:
        booking.status = status_map[new_status_str]
        db.session.commit()

    if request.headers.get("HX-Request"):
        query = select(Booking).filter(
            Booking.tenant_id == tenant.id,
            Booking.slot_date == booking.slot_date,
            Booking.status.in_([
                BookingStatus.CONFIRMED,
                BookingStatus.IN_CONSULTATION,
                BookingStatus.COMPLETED,
                BookingStatus.NO_SHOW,
            ])
        )
        all_queue = db.session.execute(query).scalars().all()

        def queue_sort_key(b: Booking):
            status_rank = {
                BookingStatus.IN_CONSULTATION: 0,
                BookingStatus.CONFIRMED: 1,
                BookingStatus.COMPLETED: 2,
                BookingStatus.NO_SHOW: 3,
            }.get(b.status, 4)
            token_rank = b.token_number if b.token_number is not None else 9999
            time_minutes = b.start_time.hour * 60 + b.start_time.minute
            return (status_rank, token_rank, time_minutes)

        queue_entries = sorted(all_queue, key=queue_sort_key)
        return render_template(
            "clinic/_queue_table.html",
            tenant=tenant,
            queue_entries=queue_entries,
            current_role=_get_current_role(),
        )

    return redirect(url_for("clinic.queue_board", slug=tenant.slug, date=booking.slot_date.isoformat()))


@clinic_bp.route("/patients")
@require_role(UserRole.OWNER, UserRole.DOCTOR, UserRole.RECEPTIONIST)
def patients_list():
    """Searchable patient directory by phone or name."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    search_query = (request.args.get("q") or "").strip()
    stmt = select(Customer).filter(Customer.tenant_id == tenant.id)
    if search_query:
        stmt = stmt.filter(
            or_(
                Customer.name.ilike(f"%{search_query}%"),
                Customer.phone.ilike(f"%{search_query}%")
            )
        )
    stmt = stmt.order_by(Customer.created_at.desc())
    patients = db.session.execute(stmt).scalars().all()

    return render_template(
        "clinic/patients.html",
        tenant=tenant,
        patients=patients,
        search_query=search_query,
    )


@clinic_bp.route("/patients", methods=["POST"])
@require_role(UserRole.OWNER, UserRole.DOCTOR, UserRole.RECEPTIONIST)
def create_patient():
    """Registers a new patient with explicit DPDP consent audit and strict validation."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    name, name_err = sanitize_and_validate_name(request.form.get("name"))
    phone, phone_err = sanitize_and_validate_phone(request.form.get("phone"))
    age = request.form.get("age")
    gender = request.form.get("gender")
    notes = request.form.get("notes")

    if name_err or phone_err:
        flash(name_err or phone_err, "error")
        return redirect(url_for("clinic.patients_list", slug=tenant.slug))

    existing = db.session.execute(
        select(Customer).filter_by(tenant_id=tenant.id, phone=phone)
    ).scalar_one_or_none()

    if existing:
        flash(f"Patient with phone {phone} already exists.", "info")
        return redirect(url_for("clinic.patient_profile", customer_id=existing.id, slug=tenant.slug))

    now_utc = datetime.now(timezone.utc)
    patient = Customer(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name=name,
        phone=phone,
        age=int(age) if age and age.isdigit() else None,
        gender=gender,
        dpdp_consent_given=True,
        dpdp_consent_at=now_utc,
        notes=notes,
    )
    db.session.add(patient)
    db.session.commit()

    flash(f"Patient {name} registered with DPDP consent.", "success")
    return redirect(url_for("clinic.patient_profile", customer_id=patient.id, slug=tenant.slug))


@clinic_bp.route("/patients/<uuid:customer_id>")
@require_role(UserRole.OWNER, UserRole.DOCTOR, UserRole.RECEPTIONIST)
def patient_profile(customer_id: uuid.UUID):
    """Patient Profile (EMR-Lite) with RBAC masking and VIEW_PATIENT_RECORD audit logging."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    patient = db.session.execute(
        select(Customer).filter_by(tenant_id=tenant.id, id=customer_id)
    ).scalar_one_or_none()
    if not patient:
        abort(404, description="Patient record not found.")

    current_role = _get_current_role()
    can_view_clinical_notes = current_role in (UserRole.OWNER.value, UserRole.DOCTOR.value, "OWNER", "DOCTOR")

    user_id_val = uuid.UUID(session["user_id"]) if session.get("user_id") else None
    audit = AuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        user_id=user_id_val,
        action="VIEW_PATIENT_RECORD",
        target_id=patient.id,
        ip_address=request.remote_addr,
    )
    db.session.add(audit)
    db.session.commit()

    visits = db.session.execute(
        select(ClinicVisit).filter_by(tenant_id=tenant.id, customer_id=patient.id).order_by(ClinicVisit.created_at.desc())
    ).scalars().all()

    doctors = db.session.execute(
        select(Resource).filter_by(tenant_id=tenant.id, is_active=True)
    ).scalars().all()

    return render_template(
        "clinic/patient_profile.html",
        tenant=tenant,
        patient=patient,
        visits=visits,
        doctors=doctors,
        can_view_clinical_notes=can_view_clinical_notes,
        current_role=current_role,
    )


@clinic_bp.route("/patients/<uuid:customer_id>/visits", methods=["POST"])
@require_role(UserRole.OWNER, UserRole.DOCTOR)
def record_visit(customer_id: uuid.UUID):
    """Records a clinical consultation visit and follow-up date (OWNER/DOCTOR only)."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    current_role = _get_current_role()
    if current_role not in (UserRole.OWNER.value, UserRole.DOCTOR.value, "OWNER", "DOCTOR"):
        abort(403, description="Receptionists are not authorized to view or edit clinical notes.")

    patient = db.session.execute(
        select(Customer).filter_by(tenant_id=tenant.id, id=customer_id)
    ).scalar_one_or_none()
    if not patient:
        abort(404)

    doctor_id_str = request.form.get("doctor_resource_id")
    chief_complaint = request.form.get("chief_complaint")
    clinical_notes = request.form.get("clinical_notes")
    procedures_done = request.form.get("procedures_done")
    next_followup_str = request.form.get("next_followup_date")

    try:
        doctor_resource_id = uuid.UUID(doctor_id_str)
        next_followup_date = date.fromisoformat(next_followup_str) if next_followup_str else None
    except (ValueError, TypeError) as err:
        flash(f"Invalid input: {err}", "error")
        return redirect(url_for("clinic.patient_profile", customer_id=customer_id, slug=tenant.slug))

    visit = ClinicVisit(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        customer_id=patient.id,
        doctor_resource_id=doctor_resource_id,
        chief_complaint=chief_complaint,
        clinical_notes=clinical_notes,
        procedures_done=procedures_done,
        next_followup_date=next_followup_date,
    )
    db.session.add(visit)

    user_id_val = uuid.UUID(session["user_id"]) if session.get("user_id") else None
    audit = AuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        user_id=user_id_val,
        action="UPDATE_CLINICAL_NOTES",
        target_id=patient.id,
        ip_address=request.remote_addr,
    )
    db.session.add(audit)
    db.session.commit()

    flash("Clinical visit and follow-up scheduled successfully.", "success")
    return redirect(url_for("clinic.patient_profile", customer_id=customer_id, slug=tenant.slug))


@clinic_bp.route("/invoices")
@require_role(UserRole.OWNER, UserRole.DOCTOR, UserRole.RECEPTIONIST)
def invoices_list():
    """Financial invoice ledger and pending balance tracker."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    invoices = db.session.execute(
        select(Invoice).filter_by(tenant_id=tenant.id).order_by(Invoice.created_at.desc())
    ).scalars().all()

    total_billed_paise = sum(inv.total_paise for inv in invoices)
    total_paid_paise = sum(inv.paid_paise for inv in invoices)
    total_balance_paise = sum(inv.balance_paise for inv in invoices)

    patients = db.session.execute(
        select(Customer).filter_by(tenant_id=tenant.id).order_by(Customer.name.asc())
    ).scalars().all()

    return render_template(
        "clinic/invoices.html",
        tenant=tenant,
        invoices=invoices,
        patients=patients,
        total_billed_paise=total_billed_paise,
        total_paid_paise=total_paid_paise,
        total_balance_paise=total_balance_paise,
    )


@clinic_bp.route("/invoices", methods=["POST"])
@require_role(UserRole.OWNER, UserRole.DOCTOR, UserRole.RECEPTIONIST)
def create_invoice():
    """Generates an itemized clinic bill and dispatches a WhatsApp receipt link."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    customer_id_str = request.form.get("customer_id")
    booking_id_str = request.form.get("booking_id")
    payment_mode_str = request.form.get("payment_mode", "CASH")
    paid_paise_str = request.form.get("paid_paise", "0")

    item_descriptions = request.form.getlist("item_desc[]")
    item_amounts = request.form.getlist("item_amount[]")

    try:
        customer_id = uuid.UUID(customer_id_str)
        booking_id = uuid.UUID(booking_id_str) if booking_id_str else None
        paid_paise = int(paid_paise_str)
        payment_mode = PaymentMode(payment_mode_str)
    except (ValueError, TypeError) as err:
        flash(f"Invalid input: {err}", "error")
        return redirect(url_for("clinic.invoices_list", slug=tenant.slug))

    line_items = []
    total_paise = 0
    for desc, amt_str in zip(item_descriptions, item_amounts):
        if desc and amt_str:
            try:
                amt = int(amt_str)
                line_items.append({"desc": desc.strip(), "amount_paise": amt})
                total_paise += amt
            except ValueError:
                pass

    if not line_items:
        line_items = [{"desc": "Medical Consultation", "amount_paise": 30000}]
        total_paise = 30000

    balance_paise = max(0, total_paise - paid_paise)

    invoice_number = Invoice.generate_next_invoice_number(tenant.id)

    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        customer_id=customer_id,
        booking_id=booking_id,
        invoice_number=invoice_number,
        line_items=line_items,
        total_paise=total_paise,
        paid_paise=paid_paise,
        balance_paise=balance_paise,
        payment_mode=payment_mode,
    )
    db.session.add(invoice)
    db.session.commit()

    if invoice.customer:
        NotifierService().send_notification(
            tenant=tenant,
            recipient_phone=invoice.customer.phone,
            template_name="invoice_receipt_v1",
            parameters={
                "patient_name": invoice.customer.name,
                "clinic_name": tenant.name,
                "invoice_number": invoice.invoice_number,
                "total_inr": invoice.total_paise // 100,
                "paid_inr": invoice.paid_paise // 100,
                "balance_inr": invoice.balance_paise // 100,
                "receipt_url": f"{request.host_url.rstrip('/')}/i/{invoice.id}",
            },
            customer_id=invoice.customer.id,
        )

    flash(f"Invoice {invoice_number} created successfully.", "success")
    return redirect(url_for("clinic.invoice_receipt", invoice_id=invoice.id, slug=tenant.slug))


@clinic_bp.route("/invoices/<uuid:invoice_id>/receipt")
@require_role(UserRole.OWNER, UserRole.DOCTOR, UserRole.RECEPTIONIST)
def invoice_receipt(invoice_id: uuid.UUID):
    """Clean, printable A4/A5 PDF-ready invoice receipt view with WhatsApp share link."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    invoice = db.session.execute(
        select(Invoice).filter_by(tenant_id=tenant.id, id=invoice_id)
    ).scalar_one_or_none()
    if not invoice:
        abort(404, description="Invoice not found.")

    public_receipt_url = f"{request.host_url.rstrip('/')}/i/{invoice.id}"
    wa_text = (
        f"Hello {invoice.customer.name}, here is your medical receipt *{invoice.invoice_number}* "
        f"from *{tenant.name}*. Total: ₹{invoice.total_paise // 100} | Paid: ₹{invoice.paid_paise // 100} "
        f"| Balance: ₹{invoice.balance_paise // 100}. View/Print PDF: {public_receipt_url}"
    )
    wa_share_url = f"https://wa.me/91{invoice.customer.phone}?text={urllib.parse.quote(wa_text)}"

    return render_template(
        "clinic/invoice_receipt.html",
        tenant=tenant,
        invoice=invoice,
        public_receipt_url=public_receipt_url,
        wa_share_url=wa_share_url,
    )


@clinic_bp.route("/export/patients")
@require_role(UserRole.OWNER, UserRole.DOCTOR, UserRole.RECEPTIONIST)
def export_patients_csv():
    """One-click CSV export of patients with DPDP consent timestamps."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    user_id_val = uuid.UUID(session["user_id"]) if session.get("user_id") else None
    audit = AuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        user_id=user_id_val,
        action="EXPORT_DATA",
        ip_address=request.remote_addr,
    )
    db.session.add(audit)
    db.session.commit()

    patients = db.session.execute(
        select(Customer).filter_by(tenant_id=tenant.id).order_by(Customer.created_at.asc())
    ).scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Patient ID", "Name", "Phone", "Age", "Gender", "DPDP Consent", "Consent Timestamp", "Notes"])
    for p in patients:
        writer.writerow(
            sanitize_csv_row([
                str(p.id),
                p.name,
                p.phone,
                p.age if p.age is not None else "",
                p.gender or "",
                "Yes" if p.dpdp_consent_given else "No",
                p.dpdp_consent_at.isoformat() if p.dpdp_consent_at else "",
                p.notes or "",
            ])
        )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={tenant.slug}_patients.csv",
            "X-Content-Type-Options": "nosniff",
        }
    )


@clinic_bp.route("/export/visits")
@require_role(UserRole.OWNER, UserRole.DOCTOR)
def export_visits_csv():
    """One-click CSV export of clinical visits. Restricted to OWNER/DOCTOR."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    user_id_val = uuid.UUID(session["user_id"]) if session.get("user_id") else None
    audit = AuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        user_id=user_id_val,
        action="EXPORT_DATA",
        ip_address=request.remote_addr,
    )
    db.session.add(audit)
    db.session.commit()

    visits = db.session.execute(
        select(ClinicVisit).filter_by(tenant_id=tenant.id).order_by(ClinicVisit.created_at.asc())
    ).scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Visit ID", "Date", "Patient Name", "Phone", "Doctor", "Chief Complaint", "Clinical Notes", "Procedures", "Next Follow-up Date"])
    for v in visits:
        writer.writerow(
            sanitize_csv_row([
                str(v.id),
                v.created_at.strftime('%Y-%m-%d %H:%M') if v.created_at else "",
                v.customer.name,
                v.customer.phone,
                v.doctor_resource.name if v.doctor_resource else "",
                v.chief_complaint or "",
                v.clinical_notes or "",
                v.procedures_done or "",
                v.next_followup_date.isoformat() if v.next_followup_date else "",
            ])
        )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={tenant.slug}_visits.csv",
            "X-Content-Type-Options": "nosniff",
        }
    )


@clinic_bp.route("/export/invoices")
@require_role(UserRole.OWNER, UserRole.DOCTOR, UserRole.RECEPTIONIST)
def export_invoices_csv():
    """One-click CSV export of clinic invoices."""
    tenant = resolve_current_tenant()
    if not tenant:
        abort(404)

    user_id_val = uuid.UUID(session["user_id"]) if session.get("user_id") else None
    audit = AuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        user_id=user_id_val,
        action="EXPORT_DATA",
        ip_address=request.remote_addr,
    )
    db.session.add(audit)
    db.session.commit()

    invoices = db.session.execute(
        select(Invoice).filter_by(tenant_id=tenant.id).order_by(Invoice.created_at.asc())
    ).scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Invoice Number", "Date", "Patient Name", "Phone", "Total (INR)", "Paid (INR)", "Balance Due (INR)", "Payment Mode"])
    for inv in invoices:
        writer.writerow(
            sanitize_csv_row([
                inv.invoice_number,
                inv.created_at.strftime('%Y-%m-%d %H:%M') if inv.created_at else "",
                inv.customer.name,
                inv.customer.phone,
                f"{inv.total_paise / 100:.2f}",
                f"{inv.paid_paise / 100:.2f}",
                f"{inv.balance_paise / 100:.2f}",
                inv.payment_mode.value if inv.payment_mode else "",
            ])
        )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={tenant.slug}_invoices.csv",
            "X-Content-Type-Options": "nosniff",
        }
    )