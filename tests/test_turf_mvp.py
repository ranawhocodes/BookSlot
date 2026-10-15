import json
import uuid
import hmac
import hashlib
from datetime import date, time
from flask import url_for
from app.models import (
    Tenant,
    Resource,
    Booking,
    BookingStatus,
    Payment,
    PaymentMode,
    Customer,
)
from app.extensions import db


def test_public_booking_page_renders(app, client, turf_setup):
    """GET /b/<slug> loads public customer page with venue details and resources."""
    tenant = turf_setup["tenant"]
    resp = client.get(f"/b/{tenant.slug}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Test Pondy Arena" in html
    assert "5v5 Turf A" in html
    assert "Badminton Court 1" in html
    assert "Pay 30% Advance To Book" in html


def test_htmx_slots_grid_partial(app, client, turf_setup):
    """GET /b/<slug>/slots returns refreshed slot cards for selected resource and date."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]
    wednesday = date(2026, 10, 14)

    resp = client.get(f"/b/{tenant.slug}/slots?resource_id={football.id}&date={wednesday.isoformat()}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "06:00 PM" in html
    assert "Floodlight Peak Slots" in html
    assert "₹1200" in html


def test_create_hold_requires_dpdp_consent(app, client, turf_setup):
    """POST /b/<slug>/hold without DPDP consent fails with 400 Bad Request."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]

    resp = client.post(
        f"/b/{tenant.slug}/hold",
        data={
            "resource_id": str(football.id),
            "date": "2026-10-14",
            "start_time": "18:00",
            "end_time": "19:00",
            "name": "Arun Test",
            "phone": "9876543210",
            "dpdp_consent": "false",
        }
    )
    assert resp.status_code == 400
    assert "DPDP" in resp.get_data(as_text=True)


def test_create_hold_and_mock_payment_flow(app, client, turf_setup):
    """Full customer checkout flow: create 7-min hold -> mock payment -> confirmation."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]
    wednesday = "2026-10-14"

    # 1. Create Hold via HTMX
    hold_resp = client.post(
        f"/b/{tenant.slug}/hold",
        headers={"HX-Request": "true"},
        data={
            "resource_id": str(football.id),
            "date": wednesday,
            "start_time": "18:00",
            "end_time": "19:00",
            "name": "Karthik Vijay",
            "phone": "9876500001",
            "dpdp_consent": "on",
            "payment_choice": "advance",
        }
    )
    assert hold_resp.status_code == 200
    hold_html = hold_resp.get_data(as_text=True)
    assert "7-Minute Lock Active" in hold_html

    # Verify booking in database
    booking = db.session.execute(
        db.select(Booking).filter(
            Booking.tenant_id == tenant.id,
            Booking.slot_date == date(2026, 10, 14),
            Booking.start_time == time(18, 0)
        )
    ).scalar_one()
    assert booking.status == BookingStatus.PENDING_PAYMENT
    assert booking.locked_until is not None

    # 2. Complete Mock UPI Payment
    pay_resp = client.post(
        f"/b/{tenant.slug}/pay-mock",
        data={
            "booking_id": str(booking.id),
            "amount_paise": "36000",
        }
    )
    assert pay_resp.status_code in (200, 302)

    # 3. Verify Booking is CONFIRMED and lock is cleared
    db.session.refresh(booking)
    assert booking.status == BookingStatus.CONFIRMED
    assert booking.locked_until is None
    assert booking.paid_amount_paise == 36000

    # 4. Confirmation Ticket Page
    conf_resp = client.get(f"/b/{tenant.slug}/confirmation/{booking.id}")
    assert conf_resp.status_code == 200
    conf_html = conf_resp.get_data(as_text=True)
    assert "Booking Confirmed!" in conf_html
    assert "5v5 Turf A" in conf_html
    assert "data:image/png;base64," in conf_html


def test_razorpay_webhook_hmac_verification(app, client, turf_setup):
    """POST /webhooks/razorpay verifies HMAC signature and confirms booking."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]

    # Create a pending booking
    booking = Booking(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        resource_id=football.id,
        slot_date=date(2026, 10, 14),
        start_time=time(19, 0),
        end_time=time(20, 0),
        status=BookingStatus.PENDING_PAYMENT,
        total_amount_paise=120000,
    )
    db.session.add(booking)
    db.session.commit()

    secret = app.config.get("RAZORPAY_WEBHOOK_SECRET", "dev_razorpay_secret_change_in_prod")
    payload_dict = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test_rzp12345",
                    "order_id": "order_test_rzp9876",
                    "amount": 36000,
                    "notes": {
                        "booking_id": str(booking.id),
                        "tenant_id": str(tenant.id),
                    }
                }
            }
        }
    }
    raw_payload = json.dumps(payload_dict).encode("utf-8")
    valid_signature = hmac.new(secret.encode("utf-8"), raw_payload, hashlib.sha256).hexdigest()

    # 1. Invalid signature should be rejected (400)
    bad_resp = client.post(
        "/webhooks/razorpay",
        data=raw_payload,
        headers={"X-Razorpay-Signature": "invalid_sig", "Content-Type": "application/json"}
    )
    assert bad_resp.status_code == 400

    # 2. Valid signature should confirm booking
    good_resp = client.post(
        "/webhooks/razorpay",
        data=raw_payload,
        headers={"X-Razorpay-Signature": valid_signature, "Content-Type": "application/json"}
    )
    assert good_resp.status_code == 200
    assert good_resp.get_json()["status"] == "ok"

    db.session.refresh(booking)
    assert booking.status == BookingStatus.CONFIRMED
    assert booking.paid_amount_paise == 36000


def test_dashboard_metrics_and_manual_phone_booking(app, client, turf_setup):
    """Owner dashboard loads metrics and performs a 2-tap manual walk-in booking."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]

    # 1. Load Dashboard
    dash_resp = client.get(f"/dashboard/?slug={tenant.slug}")
    assert dash_resp.status_code == 200
    assert "Total Bookings" in dash_resp.get_data(as_text=True)

    # 2. Submit Manual Walk-In Booking
    manual_resp = client.post(
        "/dashboard/manual-booking",
        data={
            "resource_id": str(football.id),
            "slot_date": "2026-10-14",
            "start_time": "20:00",
            "end_time": "21:00",
            "name": "Phone Caller Ramesh",
            "phone": "9876543210",
            "payment_mode": "CASH",
            "advance_paid_paise": "20000",
            "booking_notes": "Phone booking advance paid in cash",
        },
        follow_redirects=True,
    )
    assert manual_resp.status_code == 200
    html = manual_resp.get_data(as_text=True)
    assert "Phone Caller Ramesh" in html
    assert "Walk-In" in html


def test_slot_blockout_and_unblock(app, client, turf_setup):
    """Owner blocks a slot for floodlight maintenance and subsequently unblocks it."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]

    # 1. Block slot
    block_resp = client.post(
        "/dashboard/block-slot",
        data={
            "resource_id": str(football.id),
            "slot_date": "2026-10-14",
            "start_time": "21:00",
            "end_time": "22:00",
            "reason": "Floodlight bulb replacement",
        },
        follow_redirects=True,
    )
    assert block_resp.status_code == 200
    assert "Floodlight bulb replacement" in block_resp.get_data(as_text=True)

    blocked_b = db.session.execute(
        db.select(Booking).filter(
            Booking.tenant_id == tenant.id,
            Booking.slot_date == date(2026, 10, 14),
            Booking.start_time == time(21, 0),
            Booking.status == BookingStatus.BLOCKED,
        )
    ).scalar_one()

    # 2. Unblock slot
    unblock_resp = client.post(
        f"/dashboard/unblock-slot/{blocked_b.id}",
        follow_redirects=True,
    )
    assert unblock_resp.status_code == 200
    assert "Slot unblocked successfully" in unblock_resp.get_data(as_text=True)


def test_venue_qr_poster_page(app, client, turf_setup):
    """GET /dashboard/qr-poster generates printable venue poster with high-contrast QR code."""
    tenant = turf_setup["tenant"]
    resp = client.get(f"/dashboard/qr-poster?slug={tenant.slug}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Printable QR Poster" in html
    assert "data:image/png;base64," in html
    assert "Scan with any UPI / Camera App" in html


def test_pay_mock_ignores_tampered_amount_and_blocks_production(app, client, turf_setup):
    """POST /b/<slug>/pay-mock ignores client amount_paise tampering and returns 403 in production."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]
    wednesday = "2026-10-14"

    # 1. Create Hold for 18:00-19:00 Peak Slot (Total: 120000 paise, 30% Advance: 36000 paise)
    hold_resp = client.post(
        f"/b/{tenant.slug}/hold",
        headers={"HX-Request": "true"},
        data={
            "resource_id": str(football.id),
            "date": wednesday,
            "start_time": "18:00",
            "end_time": "19:00",
            "name": "Tamper Tester",
            "phone": "9876500009",
            "dpdp_consent": "on",
            "payment_choice": "advance",
        },
    )
    assert hold_resp.status_code == 200

    booking = db.session.execute(
        db.select(Booking).filter_by(
            tenant_id=tenant.id,
            slot_date=date(2026, 10, 14),
            start_time=time(18, 0),
        )
    ).scalar_one()

    # 2. Simulate production flags (DEBUG=False, TESTING=False) -> MUST return 403 Forbidden
    app.config["DEBUG"] = False
    app.config["TESTING"] = False
    try:
        prod_resp = client.post(
            f"/b/{tenant.slug}/pay-mock",
            data={"booking_id": str(booking.id), "amount_paise": "1"},
        )
        assert prod_resp.status_code == 403
    finally:
        app.config["DEBUG"] = True
        app.config["TESTING"] = True

    # 3. Attempt to tamper with amount_paise (sending 1 paisa instead of 36000 paise)
    pay_resp = client.post(
        f"/b/{tenant.slug}/pay-mock",
        data={
            "booking_id": str(booking.id),
            "amount_paise": "1",
        },
    )
    assert pay_resp.status_code in (200, 302)

    db.session.refresh(booking)
    assert booking.status == BookingStatus.CONFIRMED
    # Must record authoritative 36000 paise, NOT the tampered 1 paisa
    assert booking.paid_amount_paise == 36000

    # 4. Attempt to pay already confirmed booking -> 400
    repeat_resp = client.post(
        f"/b/{tenant.slug}/pay-mock",
        data={"booking_id": str(booking.id)},
    )
    assert repeat_resp.status_code == 400

    # 5. Invalid UUID -> 400
    invalid_resp = client.post(
        f"/b/{tenant.slug}/pay-mock",
        data={"booking_id": "not-a-uuid"},
    )
    assert invalid_resp.status_code == 400
