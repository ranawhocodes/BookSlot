import uuid
from datetime import date, time, datetime, timedelta, timezone
import pytest
from app.extensions import db
from app.models import (
    Tenant,
    VerticalType,
    Resource,
    Customer,
    Booking,
    BookingStatus,
    ClinicVisit,
    NotificationLog,
    NotificationChannel,
    NotificationStatus,
)
from app.services.notifier import (
    NotifierService,
    MetaWhatsAppProvider,
    SMSFallbackProvider,
    AiSensyProvider,
    send_reminders,
    NotificationResult,
)
from app.translations import TRANSLATIONS, get_text


def test_translations_dictionary_keys_match():
    """All English translation keys must have an authentic Tamil translation counterpart."""
    en_keys = set(TRANSLATIONS["en"].keys())
    ta_keys = set(TRANSLATIONS["ta"].keys())
    assert en_keys == ta_keys, f"Missing keys in translations: {en_keys ^ ta_keys}"


def test_get_text_formatting():
    """Verify get_text formats placeholders properly in both English and Tamil."""
    en_txt = get_text("pay_advance_upi", lang="en", pct=30)
    assert "Pay 30% Advance via UPI" in en_txt

    ta_txt = get_text("pay_advance_upi", lang="ta", pct=30)
    assert "30%" in ta_txt
    assert "முன்பணம்" in ta_txt

    # Fallback to key if unknown
    assert get_text("unknown_random_key", lang="ta") == "unknown_random_key"


def test_notifier_quota_enforcement(app, db_session, turf_setup):
    """When a tenant reaches or exceeds their monthly_msg_quota, further sends are blocked."""
    tenant = turf_setup["tenant"]
    tenant.monthly_msg_quota = 5
    tenant.monthly_msg_used = 5
    db_session.commit()

    service = NotifierService()
    res = service.send_notification(
        tenant=tenant,
        recipient_phone="9876543210",
        template_name="booking_confirmed",
        parameters={"test": "quota"},
    )

    assert not res.success
    assert res.status == NotificationStatus.QUOTA_EXCEEDED
    assert "quota exceeded" in res.error.lower()

    # Verify quota counter did NOT increment
    db_session.refresh(tenant)
    assert tenant.monthly_msg_used == 5

    # Verify audit log was recorded with QUOTA_EXCEEDED status
    log = db_session.execute(
        db.select(NotificationLog).filter_by(tenant_id=tenant.id, status=NotificationStatus.QUOTA_EXCEEDED)
    ).scalar_one_or_none()
    assert log is not None
    assert log.recipient_phone == "9876543210"
    assert log.template_name == "booking_confirmed"


def test_meta_whatsapp_unconfigured_falls_back_to_sms(app, db_session, turf_setup):
    """When Meta WhatsApp provider is unconfigured, notifier falls back seamlessly to SMS logger."""
    tenant = turf_setup["tenant"]
    tenant.monthly_msg_quota = 500
    tenant.monthly_msg_used = 0
    db_session.commit()

    # With no API token configured, MetaWhatsAppProvider raises RuntimeError and triggers SMS fallback
    service = NotifierService(whatsapp_provider=MetaWhatsAppProvider(api_token=None, phone_number_id=None))
    res = service.send_notification(
        tenant=tenant,
        recipient_phone="9840112233",
        template_name="reminder_24h",
        parameters={
            "customer_name": "Ramesh",
            "resource_name": "5v5 Turf A",
            "slot_date": "15 Oct 2026",
            "start_time": "06:00 PM",
            "venue_name": tenant.name,
        },
    )

    assert res.success
    assert res.channel == NotificationChannel.SMS
    assert res.provider == "SMS_FALLBACK"
    assert "SMS fallback" in res.error

    # Verify message counter incremented
    db_session.refresh(tenant)
    assert tenant.monthly_msg_used == 1

    # Verify notification log
    log = db_session.execute(
        db.select(NotificationLog).filter_by(tenant_id=tenant.id, channel=NotificationChannel.SMS)
    ).scalar_one_or_none()
    assert log is not None
    assert log.status == NotificationStatus.SENT
    assert log.recipient_phone == "9840112233"


def test_configured_whatsapp_provider_succeeds(app, db_session, turf_setup):
    """When WhatsApp credentials are configured, sends via WHATSAPP channel."""
    tenant = turf_setup["tenant"]
    service = NotifierService(whatsapp_provider=MetaWhatsAppProvider(api_token="test_token_123", phone_number_id="123456789"))

    res = service.send_notification(
        tenant=tenant,
        recipient_phone="9840112233",
        template_name="booking_confirmed",
        parameters={"resource_name": "5v5 Turf A"},
    )

    assert res.success
    assert res.channel == NotificationChannel.WHATSAPP
    assert res.provider == "META"
    assert res.message_id.startswith("meta_wamid_")


def test_send_reminders_and_idempotency(app, db_session, turf_setup, clinic_setup):
    """
    Test send_reminders():
    - Dispatches 24h reminders for bookings occurring tomorrow.
    - Dispatches 2h reminders for bookings occurring within 1h-3h today.
    - Dispatches clinic follow-up alerts for appointments due in <= 2 days.
    - IDEMPOTENCY: Running a second time immediately afterwards dispatches ZERO duplicates.
    """
    turf_tenant = turf_setup["tenant"]
    football = turf_setup["football"]
    turf_customer = turf_setup["customer"]

    clinic_tenant = clinic_setup["tenant"]
    doctor = clinic_setup["doctor"]
    patient = clinic_setup["patient"]

    ref_time = datetime(2026, 10, 15, 14, 0, 0)  # 2:00 PM
    today = ref_time.date()
    tomorrow = today + timedelta(days=1)

    # 1. Seed Booking 1: 24h away (tomorrow at 2:00 PM)
    b_24h = Booking(
        id=uuid.uuid4(),
        tenant_id=turf_tenant.id,
        resource_id=football.id,
        customer_id=turf_customer.id,
        slot_date=tomorrow,
        start_time=time(14, 0),
        end_time=time(15, 0),
        status=BookingStatus.CONFIRMED,
        reminder_24h_sent=False,
        reminder_2h_sent=False,
        total_amount_paise=80000,
        paid_amount_paise=24000,
    )

    # 2. Seed Booking 2: 2h away (today at 4:00 PM, 2h after ref_time 14:00)
    b_2h = Booking(
        id=uuid.uuid4(),
        tenant_id=turf_tenant.id,
        resource_id=football.id,
        customer_id=turf_customer.id,
        slot_date=today,
        start_time=time(16, 0),
        end_time=time(17, 0),
        status=BookingStatus.CONFIRMED,
        reminder_24h_sent=True,  # 24h already sent previously
        reminder_2h_sent=False,
        total_amount_paise=80000,
        paid_amount_paise=80000,
    )

    # 3. Seed Clinic Visit: Follow-up scheduled 2 days from today (today + 2 days)
    visit = ClinicVisit(
        id=uuid.uuid4(),
        tenant_id=clinic_tenant.id,
        customer_id=patient.id,
        doctor_resource_id=doctor.id,
        chief_complaint="Severe root canal pain",
        next_followup_date=today + timedelta(days=2),
        followup_reminder_sent=False,
    )

    db_session.add_all([b_24h, b_2h, visit])
    db_session.commit()

    # --- FIRST RUN: Dispatches all three reminders ---
    summary1 = send_reminders(reference_time=ref_time)
    assert summary1["reminders_24h_sent"] == 1
    assert summary1["reminders_2h_sent"] == 1
    assert summary1["followups_sent"] == 1
    assert summary1["quota_exceeded_count"] == 0

    # Refresh records and verify boolean flags are updated to True
    db_session.refresh(b_24h)
    db_session.refresh(b_2h)
    db_session.refresh(visit)
    assert b_24h.reminder_24h_sent is True
    assert b_2h.reminder_2h_sent is True
    assert visit.followup_reminder_sent is True

    # --- SECOND RUN: IDEMPOTENCY CHECK ---
    # Running send_reminders again must do nothing and send ZERO duplicate messages
    summary2 = send_reminders(reference_time=ref_time)
    assert summary2["reminders_24h_sent"] == 0
    assert summary2["reminders_2h_sent"] == 0
    assert summary2["followups_sent"] == 0
    assert summary2["quota_exceeded_count"] == 0


def test_public_booking_page_tamil_ui_toggle(client, turf_setup):
    """GET /b/<slug>?lang=ta renders localized Tamil headers, date pickers, and checkout terms."""
    tenant = turf_setup["tenant"]

    # 1. Fetch in Tamil
    resp_ta = client.get(f"/b/{tenant.slug}?lang=ta")
    assert resp_ta.status_code == 200
    html_ta = resp_ta.get_data(as_text=True)

    # Check Tamil labels from translations
    assert "தேதியைத் தேர்ந்தெடுக்கவும்" in html_ta  # Select Date
    assert "இன்று" in html_ta  # Today
    assert "மைதானம் / தளத்தைத் தேர்ந்தெடுக்கவும்" in html_ta  # Select Resource
    assert "வாட்ஸ்அப் கைபேசி எண்" in html_ta  # WhatsApp Mobile Number
    assert "DPDP சட்டம் 2023" in html_ta  # DPDP consent
    assert "முழுப் பெயர்" in html_ta  # Full Name
    assert "இடத்தைப் பூட்டி பணம் செலுத்த தொடரவும்" in html_ta  # Hold and pay button
    assert "மைதானத்தில் செலுத்த வேண்டியது" in html_ta  # Balance at venue

    # Check Language toggle buttons
    assert 'id="lang-ta-toggle"' in html_ta
    assert 'id="lang-en-toggle"' in html_ta

    # 2. Fetch in English
    resp_en = client.get(f"/b/{tenant.slug}?lang=en")
    assert resp_en.status_code == 200
    html_en = resp_en.get_data(as_text=True)

    assert "Select Date" in html_en
    assert "Today" in html_en
    assert "Select Court / Turf" in html_en
    assert "WhatsApp Mobile Number" in html_en


def test_slots_grid_tamil_partial(client, turf_setup):
    """GET /b/<slug>/slots with lang=ta returns translated slot labels."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]
    today_str = date.today().isoformat()

    resp = client.get(
        f"/b/{tenant.slug}/slots?resource_id={football.id}&date={today_str}&lang=ta"
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "கிடைக்கும் முன்பதிவு நேரங்கள்" in html
    assert "முன்பதிவு →" in html
