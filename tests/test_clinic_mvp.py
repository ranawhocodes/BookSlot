import uuid
from datetime import date, time, datetime, timezone
import pytest
from flask import session
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


def test_clinic_queue_board_renders(app, client, clinic_setup):
    """GET /dashboard/clinic/queue loads hybrid queue board with doctor details and stats."""
    tenant = clinic_setup["tenant"]
    doctor = clinic_setup["doctor"]

    with client.session_transaction() as sess:
        sess["user_role"] = UserRole.OWNER.value
        sess["tenant_id"] = str(tenant.id)

    resp = client.get(f"/dashboard/clinic/queue?slug={tenant.slug}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Queue Board" in html
    assert doctor.name in html
    assert "Waiting in Queue" in html
    assert "In Consultation" in html
    assert "Completed Today" in html
    assert "Issue Walk-In Token" in html


def test_issue_walkin_token_sequential(app, client, clinic_setup):
    """POST /dashboard/clinic/queue/walkin-token creates sequential tokens for a doctor."""
    tenant = clinic_setup["tenant"]
    doctor = clinic_setup["doctor"]
    today = date.today()

    with client.session_transaction() as sess:
        sess["user_role"] = UserRole.RECEPTIONIST.value
        sess["tenant_id"] = str(tenant.id)

    # 1. Issue Token #1
    resp1 = client.post(
        "/dashboard/clinic/queue/walkin-token",
        data={
            "slug": tenant.slug,
            "doctor_resource_id": str(doctor.id),
            "slot_date": today.isoformat(),
            "patient_name": "Arun Kumar",
            "patient_phone": "9840112233",
            "booking_notes": "Mild toothache",
        },
        follow_redirects=True,
    )
    assert resp1.status_code == 200
    assert "Token #1 issued for Arun Kumar" in resp1.get_data(as_text=True)

    # Verify first token booking in DB
    b1 = db.session.execute(
        db.select(Booking).filter_by(tenant_id=tenant.id, token_number=1)
    ).scalar_one()
    assert b1.token_number == 1
    assert b1.is_walkin is True
    assert b1.status == BookingStatus.CONFIRMED

    # Verify customer was created with DPDP consent
    c1 = db.session.execute(
        db.select(Customer).filter_by(tenant_id=tenant.id, phone="9840112233")
    ).scalar_one()
    assert c1.name == "Arun Kumar"
    assert c1.dpdp_consent_given is True
    assert c1.dpdp_consent_at is not None

    # 2. Issue Token #2 for a different patient
    resp2 = client.post(
        "/dashboard/clinic/queue/walkin-token",
        data={
            "slug": tenant.slug,
            "doctor_resource_id": str(doctor.id),
            "slot_date": today.isoformat(),
            "patient_name": "Meena Devi",
            "patient_phone": "9840998877",
            "booking_notes": "Routine checkup",
        },
        follow_redirects=True,
    )
    assert resp2.status_code == 200
    assert "Token #2 issued for Meena Devi" in resp2.get_data(as_text=True)

    b2 = db.session.execute(
        db.select(Booking).filter_by(tenant_id=tenant.id, token_number=2)
    ).scalar_one()
    assert b2.token_number == 2
    assert b2.is_walkin is True
    # Ensure start_times are distinct to satisfy idx_unique_confirmed_slot
    assert b1.start_time != b2.start_time


def test_queue_status_transitions_htmx(app, client, clinic_setup):
    """HTMX status updates: WAITING -> IN_CONSULTATION -> COMPLETED returns partial table."""
    tenant = clinic_setup["tenant"]
    doctor = clinic_setup["doctor"]
    patient = clinic_setup["patient"]
    today = date.today()

    with client.session_transaction() as sess:
        sess["user_role"] = UserRole.DOCTOR.value
        sess["tenant_id"] = str(tenant.id)

    # Create initial waiting booking
    booking = Booking(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        resource_id=doctor.id,
        customer_id=patient.id,
        slot_date=today,
        start_time=time(10, 0),
        end_time=time(10, 20),
        status=BookingStatus.CONFIRMED,
        is_walkin=False,
        token_number=None,
        total_amount_paise=doctor.base_price_paise,
    )
    db.session.add(booking)
    db.session.commit()

    # 1. Transition to IN_CONSULTATION via HTMX
    resp_call = client.post(
        f"/dashboard/clinic/queue/{booking.id}/status",
        headers={"HX-Request": "true"},
        data={"status": "IN_CONSULTATION", "slug": tenant.slug},
    )
    assert resp_call.status_code == 200
    html_call = resp_call.get_data(as_text=True)
    assert "IN CONSULTATION" in html_call
    assert "Finish Consultation" in html_call

    # Verify status in DB
    db.session.refresh(booking)
    assert booking.status == BookingStatus.IN_CONSULTATION

    # 2. Transition to COMPLETED via HTMX
    resp_done = client.post(
        f"/dashboard/clinic/queue/{booking.id}/status",
        headers={"HX-Request": "true"},
        data={"status": "COMPLETED", "slug": tenant.slug},
    )
    assert resp_done.status_code == 200
    html_done = resp_done.get_data(as_text=True)
    assert "COMPLETED" in html_done

    db.session.refresh(booking)
    assert booking.status == BookingStatus.COMPLETED


def test_patient_search_and_registration(app, client, clinic_setup):
    """Search patient directory and register new patient with DPDP consent."""
    tenant = clinic_setup["tenant"]

    with client.session_transaction() as sess:
        sess["user_role"] = UserRole.RECEPTIONIST.value
        sess["tenant_id"] = str(tenant.id)

    # Register new patient
    resp = client.post(
        "/dashboard/clinic/patients",
        data={
            "slug": tenant.slug,
            "name": "Kavitha Rajan",
            "phone": "9443322110",
            "age": "34",
            "gender": "Female",
            "notes": "Allergic to penicillin",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "registered with DPDP consent" in resp.get_data(as_text=True)

    # Verify in DB
    new_patient = db.session.execute(
        db.select(Customer).filter_by(tenant_id=tenant.id, phone="9443322110")
    ).scalar_one()
    assert new_patient.name == "Kavitha Rajan"
    assert new_patient.age == 34
    assert new_patient.dpdp_consent_given is True

    # Search directory by query
    search_resp = client.get(f"/dashboard/clinic/patients?slug={tenant.slug}&q=Kavitha")
    assert search_resp.status_code == 200
    assert "Kavitha Rajan" in search_resp.get_data(as_text=True)


def test_patient_profile_and_rbac_audit_logging(app, client, clinic_setup):
    """
    EMR-Lite RBAC & DPDP audit logging:
    - OWNER/DOCTOR viewing logs VIEW_PATIENT_RECORD and sees clinical notes.
    - RECEPTIONIST viewing logs VIEW_PATIENT_RECORD but clinical notes are masked.
    - RECEPTIONIST posting to /visits receives 403 Forbidden.
    - DOCTOR posting to /visits creates ClinicVisit and logs UPDATE_CLINICAL_NOTES.
    """
    tenant = clinic_setup["tenant"]
    doctor = clinic_setup["doctor"]
    patient = clinic_setup["patient"]

    # --- 1. Doctor View ---
    with client.session_transaction() as sess:
        sess["user_role"] = UserRole.DOCTOR.value
        sess["tenant_id"] = str(tenant.id)

    resp_doctor = client.get(f"/dashboard/clinic/patients/{patient.id}?slug={tenant.slug}")
    assert resp_doctor.status_code == 200
    doc_html = resp_doctor.get_data(as_text=True)
    assert "EMR-Lite" in doc_html
    assert "Record Consultation Visit" in doc_html

    # Check AuditLog for VIEW_PATIENT_RECORD
    view_audit = db.session.execute(
        db.select(AuditLog).filter_by(
            tenant_id=tenant.id,
            action="VIEW_PATIENT_RECORD",
            target_id=patient.id,
        )
    ).scalar_one_or_none()
    assert view_audit is not None

    # --- 2. Receptionist View & Restriction ---
    with client.session_transaction() as sess:
        sess["user_role"] = UserRole.RECEPTIONIST.value
        sess["tenant_id"] = str(tenant.id)

    resp_rec = client.get(f"/dashboard/clinic/patients/{patient.id}?slug={tenant.slug}")
    assert resp_rec.status_code == 200
    rec_html = resp_rec.get_data(as_text=True)
    assert "Confidential Clinical Notes" in rec_html
    assert "Record Consultation Visit" not in rec_html

    # Receptionist attempts to post to /visits -> 403 Forbidden
    resp_rec_post = client.post(
        f"/dashboard/clinic/patients/{patient.id}/visits",
        data={
            "slug": tenant.slug,
            "doctor_resource_id": str(doctor.id),
            "chief_complaint": "Attempt by receptionist",
            "clinical_notes": "Unauthorized",
        },
    )
    assert resp_rec_post.status_code == 403

    # --- 3. Doctor Consultation Entry & Follow-Up Date ---
    with client.session_transaction() as sess:
        sess["user_role"] = UserRole.DOCTOR.value
        sess["tenant_id"] = str(tenant.id)

    next_date = date(2026, 10, 28)
    resp_doc_post = client.post(
        f"/dashboard/clinic/patients/{patient.id}/visits",
        data={
            "slug": tenant.slug,
            "doctor_resource_id": str(doctor.id),
            "chief_complaint": "Acute lower molar sensitivity",
            "clinical_notes": "Enamel erosion detected on 36. Applied fluoride varnish.",
            "procedures_done": "Topical Fluoride Application",
            "next_followup_date": next_date.isoformat(),
        },
        follow_redirects=True,
    )
    assert resp_doc_post.status_code == 200

    # Verify visit in DB
    visit = db.session.execute(
        db.select(ClinicVisit).filter_by(tenant_id=tenant.id, customer_id=patient.id)
    ).scalar_one()
    assert visit.chief_complaint == "Acute lower molar sensitivity"
    assert "fluoride varnish" in visit.clinical_notes
    assert visit.next_followup_date == next_date

    # Verify UPDATE_CLINICAL_NOTES audit log
    update_audit = db.session.execute(
        db.select(AuditLog).filter_by(
            tenant_id=tenant.id,
            action="UPDATE_CLINICAL_NOTES",
            target_id=patient.id,
        )
    ).scalar_one_or_none()
    assert update_audit is not None


def test_invoice_creation_and_balance_tracking(app, client, clinic_setup):
    """Create itemized clinic invoice, verify total, paid, balance paise, and receipt."""
    tenant = clinic_setup["tenant"]
    patient = clinic_setup["patient"]

    with client.session_transaction() as sess:
        sess["user_role"] = UserRole.RECEPTIONIST.value
        sess["tenant_id"] = str(tenant.id)

    # Create Invoice: Consultation (₹300) + Scaling (₹700) = ₹1,000. Paid: ₹600. Balance: ₹400.
    resp = client.post(
        "/dashboard/clinic/invoices",
        data={
            "slug": tenant.slug,
            "customer_id": str(patient.id),
            "payment_mode": "UPI_VENUE",
            "paid_paise": "60000",
            "item_desc[]": ["Doctor Consultation Fee", "Ultrasonic Dental Scaling"],
            "item_amount[]": ["30000", "70000"],
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    receipt_html = resp.get_data(as_text=True)
    assert "Medical Invoice &amp; Receipt" in receipt_html or "Medical Invoice & Receipt" in receipt_html
    assert "Ultrasonic Dental Scaling" in receipt_html
    assert "₹1000" in receipt_html
    assert "₹600" in receipt_html
    assert "₹400" in receipt_html

    # Verify in DB
    invoice = db.session.execute(
        db.select(Invoice).filter_by(tenant_id=tenant.id, customer_id=patient.id)
    ).scalar_one()
    assert invoice.total_paise == 100000
    assert invoice.paid_paise == 60000
    assert invoice.balance_paise == 40000
    assert invoice.payment_mode == PaymentMode.UPI_VENUE
    assert invoice.invoice_number.startswith("INV-SRI-")


def test_csv_data_exports_and_audit_logging(app, client, clinic_setup):
    """Test 1-click CSV exports for patients, invoices, and visits with audit logging."""
    tenant = clinic_setup["tenant"]
    patient = clinic_setup["patient"]

    # 1. Export Patients
    with client.session_transaction() as sess:
        sess["user_role"] = UserRole.RECEPTIONIST.value
        sess["tenant_id"] = str(tenant.id)

    p_resp = client.get(f"/dashboard/clinic/export/patients?slug={tenant.slug}")
    assert p_resp.status_code == 200
    assert p_resp.mimetype == "text/csv"
    p_csv = p_resp.get_data(as_text=True)
    assert "Patient ID,Name,Phone" in p_csv
    assert patient.name in p_csv

    # 2. Export Invoices
    inv_resp = client.get(f"/dashboard/clinic/export/invoices?slug={tenant.slug}")
    assert inv_resp.status_code == 200
    assert inv_resp.mimetype == "text/csv"
    assert "Invoice Number,Date,Patient Name" in inv_resp.get_data(as_text=True)

    # 3. Export Visits - Receptionist blocked with 403
    v_resp_blocked = client.get(f"/dashboard/clinic/export/visits?slug={tenant.slug}")
    assert v_resp_blocked.status_code == 403

    # Export Visits - Doctor allowed
    with client.session_transaction() as sess:
        sess["user_role"] = UserRole.DOCTOR.value
        sess["tenant_id"] = str(tenant.id)

    v_resp = client.get(f"/dashboard/clinic/export/visits?slug={tenant.slug}")
    assert v_resp.status_code == 200
    assert v_resp.mimetype == "text/csv"
    assert "Visit ID,Date,Patient Name" in v_resp.get_data(as_text=True)

    # Verify AuditLog for EXPORT_DATA
    export_audits = db.session.execute(
        db.select(AuditLog).filter_by(tenant_id=tenant.id, action="EXPORT_DATA")
    ).scalars().all()
    assert len(export_audits) >= 2
