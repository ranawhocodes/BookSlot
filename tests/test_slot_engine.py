import pytest
import uuid
from datetime import date, datetime, time, timedelta, timezone
from app.models import (
    Booking,
    BookingStatus,
    Tenant,
    VerticalType,
    Resource,
    AvailabilityRule,
    Customer,
)
from app.services.slot_engine import (
    get_available_slots,
    create_slot_hold,
    confirm_booking,
    release_slot_hold,
    expire_stale_holds,
    SlotUnavailableError,
)


def test_get_available_slots_break_and_operating_windows(clinic_setup):
    """Calculates available slots while properly marking break windows."""
    tenant = clinic_setup["tenant"]
    doctor = clinic_setup["doctor"]
    thursday = date(2026, 10, 15)

    slots = get_available_slots(tenant.id, doctor.id, thursday)
    assert len(slots) > 0

    # 09:00 - 09:20 is in the morning operating window
    morning_slot = next((s for s in slots if s.start_time == time(9, 0)), None)
    assert morning_slot is not None
    assert morning_slot.status == "AVAILABLE"
    assert morning_slot.end_time == time(9, 20)

    # 13:30 - 13:50 is the start of the afternoon break window (13:30 - 16:30)
    break_slot = next((s for s in slots if s.start_time == time(13, 30)), None)
    assert break_slot is not None
    assert break_slot.status == "BREAK"
    assert break_slot.end_time == time(13, 50)


def test_atomic_7_minute_slot_hold_blocks_duplicate(turf_setup):
    """Creating a 7-minute hold locks the slot and blocks any duplicate concurrent hold."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]
    customer = turf_setup["customer"]
    wednesday = date(2026, 10, 14)
    start_t = time(18, 0)
    end_t = time(19, 0)

    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)

    # 1. First customer creates hold
    hold1 = create_slot_hold(
        tenant_id=tenant.id,
        resource_id=football.id,
        slot_date=wednesday,
        start_time=start_t,
        end_time=end_t,
        customer_id=customer.id,
        hold_duration_minutes=7,
        now=t0,
    )
    assert hold1.status == BookingStatus.PENDING_PAYMENT
    assert hold1.locked_until == t0 + timedelta(minutes=7)

    # Slot availability should now reflect LOCKED
    slots = get_available_slots(tenant.id, football.id, wednesday, now=t0 + timedelta(minutes=2))
    slot_18 = next(s for s in slots if s.start_time == start_t)
    assert slot_18.status == "LOCKED"

    # 2. Second customer attempts hold at t0 + 3 minutes (within 7-min window) -> MUST FAIL
    t_attempt = t0 + timedelta(minutes=3)
    with pytest.raises(SlotUnavailableError, match="temporarily held by another customer"):
        create_slot_hold(
            tenant_id=tenant.id,
            resource_id=football.id,
            slot_date=wednesday,
            start_time=start_t,
            end_time=end_t,
            now=t_attempt,
        )


def test_expired_hold_allows_rebooking(turf_setup, db_session):
    """An expired hold (>7 minutes elapsed) frees the slot and allows re-booking."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]
    customer = turf_setup["customer"]
    wednesday = date(2026, 10, 14)
    start_t = time(18, 0)
    end_t = time(19, 0)

    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Customer 1 holds slot at t0
    hold1 = create_slot_hold(
        tenant_id=tenant.id,
        resource_id=football.id,
        slot_date=wednesday,
        start_time=start_t,
        end_time=end_t,
        customer_id=customer.id,
        hold_duration_minutes=7,
        now=t0,
    )
    hold1_id = hold1.id

    # 2. Advance time to 8 minutes later (t0 + 8 mins) -> hold expired
    t_after_expiry = t0 + timedelta(minutes=8)

    # Dynamic check in get_available_slots must show AVAILABLE
    slots = get_available_slots(tenant.id, football.id, wednesday, now=t_after_expiry)
    slot_18 = next(s for s in slots if s.start_time == start_t)
    assert slot_18.status == "AVAILABLE"

    # 3. Customer 2 can now successfully hold the slot!
    hold2 = create_slot_hold(
        tenant_id=tenant.id,
        resource_id=football.id,
        slot_date=wednesday,
        start_time=start_t,
        end_time=end_t,
        hold_duration_minutes=7,
        now=t_after_expiry,
    )

    assert hold2.id != hold1_id
    assert hold2.status == BookingStatus.PENDING_PAYMENT
    assert hold2.locked_until == t_after_expiry + timedelta(minutes=7)

    # 4. Old hold1 must have been transitioned to EXPIRED
    db_session.refresh(hold1)
    assert hold1.status == BookingStatus.EXPIRED


def test_confirmed_booking_permanently_blocks_rebooking(turf_setup):
    """A confirmed booking clears locked_until and permanently blocks future holds."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]
    customer = turf_setup["customer"]
    wednesday = date(2026, 10, 14)
    start_t = time(19, 0)
    end_t = time(20, 0)

    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)

    hold = create_slot_hold(
        tenant_id=tenant.id,
        resource_id=football.id,
        slot_date=wednesday,
        start_time=start_t,
        end_time=end_t,
        customer_id=customer.id,
        now=t0,
    )

    # Confirm booking
    confirmed = confirm_booking(
        tenant_id=tenant.id,
        booking_id=hold.id,
        paid_amount_paise=120000,
    )
    assert confirmed.status == BookingStatus.CONFIRMED
    assert confirmed.locked_until is None

    # Even 2 hours later, slot cannot be re-booked
    t_later = t0 + timedelta(hours=2)
    with pytest.raises(SlotUnavailableError, match="already booked or blocked"):
        create_slot_hold(
            tenant_id=tenant.id,
            resource_id=football.id,
            slot_date=wednesday,
            start_time=start_t,
            end_time=end_t,
            now=t_later,
        )


def test_release_slot_hold_frees_slot_immediately(turf_setup):
    """Releasing a hold cancels it and frees the slot immediately."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]
    customer = turf_setup["customer"]
    wednesday = date(2026, 10, 14)
    start_t = time(20, 0)
    end_t = time(21, 0)

    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)

    hold = create_slot_hold(
        tenant_id=tenant.id,
        resource_id=football.id,
        slot_date=wednesday,
        start_time=start_t,
        end_time=end_t,
        customer_id=customer.id,
        now=t0,
    )

    # Cancel hold
    released = release_slot_hold(tenant_id=tenant.id, booking_id=hold.id)
    assert released.status == BookingStatus.CANCELLED
    assert released.locked_until is None

    # Slot is immediately available again
    slots = get_available_slots(tenant.id, football.id, wednesday, now=t0 + timedelta(minutes=1))
    slot_20 = next(s for s in slots if s.start_time == start_t)
    assert slot_20.status == "AVAILABLE"


def test_multi_tenant_isolation(turf_setup, db_session):
    """Holds and bookings on Tenant A never block Tenant B on the same date and time."""
    tenant_a = turf_setup["tenant"]
    res_a = turf_setup["football"]

    # Create Tenant B with its own football turf
    tenant_b = Tenant(
        id=uuid.uuid4(),
        slug="rival-turf",
        name="Rival Arena",
        vertical=VerticalType.TURF,
        phone="9876543999",
        advance_percentage=30,
    )
    db_session.add(tenant_b)
    db_session.flush()

    res_b = Resource(
        id=uuid.uuid4(),
        tenant_id=tenant_b.id,
        name="Rival Turf 1",
        category="Football",
        slot_duration_mins=60,
        base_price_paise=80000,
    )
    db_session.add(res_b)
    db_session.flush()

    # Rule for Tenant B
    db_session.add(
        AvailabilityRule(
            id=uuid.uuid4(),
            tenant_id=tenant_b.id,
            resource_id=res_b.id,
            day_of_week=2,  # Wednesday
            start_time=time(6, 0),
            end_time=time(23, 0),
            is_break=False,
            price_override_paise=80000,
        )
    )
    db_session.commit()

    wednesday = date(2026, 10, 14)
    start_t = time(18, 0)
    end_t = time(19, 0)
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Hold slot on Tenant A
    create_slot_hold(
        tenant_id=tenant_a.id,
        resource_id=res_a.id,
        slot_date=wednesday,
        start_time=start_t,
        end_time=end_t,
        now=t0,
    )

    # 2. Tenant B holds exact same slot time -> MUST SUCCEED (tenant isolation)
    hold_b = create_slot_hold(
        tenant_id=tenant_b.id,
        resource_id=res_b.id,
        slot_date=wednesday,
        start_time=start_t,
        end_time=end_t,
        now=t0,
    )
    assert hold_b.tenant_id == tenant_b.id
    assert hold_b.status == BookingStatus.PENDING_PAYMENT


def test_expire_stale_holds_bulk_helper(turf_setup, db_session):
    """Maintenance job expire_stale_holds marks all past-due holds as EXPIRED."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]
    wednesday = date(2026, 10, 14)

    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)

    # Create 2 holds at t0
    hold1 = create_slot_hold(
        tenant_id=tenant.id,
        resource_id=football.id,
        slot_date=wednesday,
        start_time=time(7, 0),
        end_time=time(8, 0),
        hold_duration_minutes=7,
        now=t0,
    )
    hold2 = create_slot_hold(
        tenant_id=tenant.id,
        resource_id=football.id,
        slot_date=wednesday,
        start_time=time(8, 0),
        end_time=time(9, 0),
        hold_duration_minutes=7,
        now=t0,
    )

    # At t0 + 10 mins, both holds are past due
    count = expire_stale_holds(tenant_id=tenant.id, now=t0 + timedelta(minutes=10))
    assert count == 2

    db_session.refresh(hold1)
    db_session.refresh(hold2)
    assert hold1.status == BookingStatus.EXPIRED
    assert hold2.status == BookingStatus.EXPIRED


def test_unique_confirmed_slot_database_constraint(turf_setup, db_session):
    """The database index idx_unique_confirmed_slot guarantees no two CONFIRMED slots can coexist."""
    from sqlalchemy.exc import IntegrityError

    tenant = turf_setup["tenant"]
    football = turf_setup["football"]
    wednesday = date(2026, 10, 14)
    start_t = time(6, 0)
    end_t = time(7, 0)

    # First confirmed booking
    b1 = Booking(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        resource_id=football.id,
        slot_date=wednesday,
        start_time=start_t,
        end_time=end_t,
        status=BookingStatus.CONFIRMED,
        total_amount_paise=80000,
        paid_amount_paise=80000,
    )
    db_session.add(b1)
    db_session.commit()

    # Second confirmed booking for identical slot
    b2 = Booking(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        resource_id=football.id,
        slot_date=wednesday,
        start_time=start_t,
        end_time=end_t,
        status=BookingStatus.CONFIRMED,
        total_amount_paise=80000,
        paid_amount_paise=80000,
    )
    db_session.add(b2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_overlapping_multi_hour_slot_hold_is_blocked(turf_setup):
    """A 2-hour hold/booking (18:00-20:00) blocks an overlapping 1-hour hold (19:00-20:00)."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]
    customer = turf_setup["customer"]
    wednesday = date(2026, 10, 14)
    t0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Customer 1 holds 2 contiguous hours: 18:00 - 20:00
    hold_2h = create_slot_hold(
        tenant_id=tenant.id,
        resource_id=football.id,
        slot_date=wednesday,
        start_time=time(18, 0),
        end_time=time(20, 0),
        customer_id=customer.id,
        hold_duration_minutes=7,
        now=t0,
    )
    assert hold_2h.status == BookingStatus.PENDING_PAYMENT

    # Both 18:00 and 19:00 slots must show as LOCKED in get_available_slots
    slots = get_available_slots(tenant.id, football.id, wednesday, now=t0 + timedelta(minutes=1))
    slot_18 = next(s for s in slots if s.start_time == time(18, 0))
    slot_19 = next(s for s in slots if s.start_time == time(19, 0))
    assert slot_18.status == "LOCKED"
    assert slot_19.status == "LOCKED"

    # 2. Customer 2 attempts to hold 19:00 - 20:00 (different start_time, but overlaps!) -> MUST FAIL
    with pytest.raises(SlotUnavailableError):
        create_slot_hold(
            tenant_id=tenant.id,
            resource_id=football.id,
            slot_date=wednesday,
            start_time=time(19, 0),
            end_time=time(20, 0),
            now=t0 + timedelta(minutes=2),
        )


def test_non_contiguous_multi_slot_hold_rejected(client, turf_setup):
    """POST /b/<slug>/hold rejects non-contiguous multi-slot selections with 400 Bad Request."""
    tenant = turf_setup["tenant"]
    football = turf_setup["football"]

    resp = client.post(
        f"/b/{tenant.slug}/hold",
        headers={"HX-Request": "true"},
        data={
            "resource_id": str(football.id),
            "date": "2026-10-14",
            "start_time": "06:00",
            "end_time": "22:00",
            "selected_slots": "06:00-07:00,21:00-22:00",
            "name": "Gap Tester",
            "phone": "9876543210",
            "dpdp_consent": "on",
            "payment_choice": "advance",
        },
    )
    assert resp.status_code == 400


