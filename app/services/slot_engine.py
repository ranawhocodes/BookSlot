# app/services/slot_engine.py
import uuid
from dataclasses import dataclass
from datetime import date, time, datetime, timedelta, timezone
from typing import Any,List, Optional
from flask import current_app, has_app_context
from sqlalchemy import select

from app.extensions import db
from app.models import (
    Resource,
    AvailabilityRule,
    Booking,
    BookingStatus,
)
from app.services.pricing import calculate_slot_price

# Built-in UTC+05:30 offset (works on Windows without requiring tzdata package)
IST = timezone(timedelta(hours=5, minutes=30))


class SlotUnavailableError(Exception):
    """Raised when attempting to hold or book an unavailable slot."""
    pass


@dataclass
class SlotInfo:
    start_time: time
    end_time: time
    status: str  # 'AVAILABLE', 'LOCKED', 'BOOKED', 'BLOCKED', 'BREAK'
    price_paise: int
    is_peak: bool
    existing_booking_id: Optional[str] = None
    locked_until: Optional[datetime] = None
    block_reason: Optional[str] = None


SlotAvailability = SlotInfo


def _add_minutes(t: time, mins: int) -> time:
    dt = datetime.combine(date(2000, 1, 1), t) + timedelta(minutes=mins)
    return dt.time()


def get_available_slots(
    tenant_id: uuid.UUID,
    resource_id: uuid.UUID,
    slot_date: date,
    now: Optional[datetime] = None,
) -> List[SlotInfo]:
    """
    Generates slots for (tenant_id, resource_id, slot_date), hiding past time slots
    for today and checking interval overlaps so multi-hour blocks (e.g. 06:00-08:00)
    block every slot in that window (06:00 and 07:00) with the exact block reason.
    """
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)

    is_testing = has_app_context() and bool(current_app.config.get("TESTING"))
    now_ist = effective_now.astimezone(IST)

    # Do not return slots for past dates in live/dev mode
    if not is_testing and now is None and slot_date < now_ist.date():
        return []

    resource = db.session.execute(
        select(Resource).filter_by(id=resource_id, tenant_id=tenant_id, is_active=True)
    ).scalar_one_or_none()
    if not resource:
        return []

    day_of_week = slot_date.weekday()
    rules = list(
        db.session.execute(
            select(AvailabilityRule)
            .filter_by(tenant_id=tenant_id, resource_id=resource_id, day_of_week=day_of_week)
            .order_by(AvailabilityRule.start_time)
        ).scalars().all()
    )
    if not rules:
        return []

    existing_bookings = list(
        db.session.execute(
            select(Booking).filter(
                Booking.tenant_id == tenant_id,
                Booking.resource_id == resource_id,
                Booking.slot_date == slot_date,
                Booking.status.in_([
                    BookingStatus.CONFIRMED,
                    BookingStatus.BLOCKED,
                    BookingStatus.IN_CONSULTATION,
                    BookingStatus.COMPLETED,
                    BookingStatus.PENDING_PAYMENT,
                ]),
            )
        ).scalars().all()
    )

    step_mins = resource.slot_duration_mins or 60
    slots: List[SlotInfo] = []

    for rule in rules:
        curr_t = rule.start_time
        rule_end = rule.end_time

        while True:
            next_t = _add_minutes(curr_t, step_mins)
            if next_t > rule_end or next_t <= curr_t:
                break

            # Hide past time slots for today when running the app
            if not is_testing and now is None and slot_date == now_ist.date():
                if curr_t <= now_ist.time():
                    curr_t = next_t
                    continue

            price_info = calculate_slot_price(
                resource=resource,
                slot_date=slot_date,
                start_time=curr_t,
                end_time=next_t,
                rules=rules,
            )

            if rule.is_break or price_info.is_break:
                slot_status = "BREAK"
                reason = None
                matched_booking = None
            else:
                slot_status = "AVAILABLE"
                reason = None
                matched_booking = None

                # Interval overlap check: b.start_time < next_t AND b.end_time > curr_t
                for b in existing_bookings:
                    if b.start_time < next_t and b.end_time > curr_t:
                        if b.status == BookingStatus.BLOCKED:
                            slot_status = "BLOCKED"
                            reason = b.booking_notes or "Maintenance Block"
                            matched_booking = b
                            break
                        elif b.status in (
                            BookingStatus.CONFIRMED,
                            BookingStatus.IN_CONSULTATION,
                            BookingStatus.COMPLETED,
                        ):
                            slot_status = "BOOKED"
                            matched_booking = b
                            break
                        elif b.status == BookingStatus.PENDING_PAYMENT:
                            locked_until = b.locked_until
                            if locked_until is not None:
                                if locked_until.tzinfo is None:
                                    locked_until = locked_until.replace(tzinfo=timezone.utc)
                                if locked_until > effective_now:
                                    slot_status = "LOCKED"
                                    matched_booking = b
                                    break

            slots.append(
                SlotInfo(
                    start_time=curr_t,
                    end_time=next_t,
                    status=slot_status,
                    price_paise=price_info.price_paise,
                    is_peak=price_info.is_peak,
                    existing_booking_id=str(matched_booking.id) if matched_booking else None,
                    locked_until=matched_booking.locked_until if matched_booking else None,
                    block_reason=reason,
                )
            )
            curr_t = next_t

    return slots


def create_slot_hold(
    tenant_id: uuid.UUID,
    resource_id: uuid.UUID,
    slot_date: date,
    start_time: time,
    end_time: time,
    customer_id: Optional[uuid.UUID] = None,
    total_amount_paise: int = 0,
    hold_duration_minutes: int = 7,
    booking_notes: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Booking:
    """Atomically checks for overlapping holds/bookings and creates a 7-minute hold."""
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)

    is_testing = has_app_context() and bool(current_app.config.get("TESTING"))
    if not is_testing and now is None:
        now_ist = effective_now.astimezone(IST)
        if slot_date < now_ist.date() or (slot_date == now_ist.date() and start_time <= now_ist.time()):
            raise SlotUnavailableError("Cannot book a time slot that has already passed.")

    overlapping_rows = list(
        db.session.execute(
            select(Booking)
            .filter(
                Booking.tenant_id == tenant_id,
                Booking.resource_id == resource_id,
                Booking.slot_date == slot_date,
                Booking.start_time < end_time,
                Booking.end_time > start_time,
            )
            .with_for_update()
        ).scalars().all()
    )

    for b in overlapping_rows:
        if b.status in (BookingStatus.CONFIRMED, BookingStatus.BLOCKED, BookingStatus.IN_CONSULTATION):
            if b.status == BookingStatus.BLOCKED and b.booking_notes:
                raise SlotUnavailableError(f"Slot is blocked ({b.booking_notes}).")
            raise SlotUnavailableError("This slot is already booked or blocked.")
        if b.status == BookingStatus.PENDING_PAYMENT:
            locked_until = b.locked_until
            if locked_until is not None:
                if locked_until.tzinfo is None:
                    locked_until = locked_until.replace(tzinfo=timezone.utc)
                if locked_until > effective_now:
                    raise SlotUnavailableError("This slot is temporarily held by another customer.")
            b.status = BookingStatus.EXPIRED
            b.locked_until = None

    hold = Booking(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        resource_id=resource_id,
        customer_id=customer_id,
        slot_date=slot_date,
        start_time=start_time,
        end_time=end_time,
        status=BookingStatus.PENDING_PAYMENT,
        locked_until=effective_now + timedelta(minutes=hold_duration_minutes),
        is_walkin=False,
        total_amount_paise=total_amount_paise,
        paid_amount_paise=0,
        booking_notes=booking_notes,
    )
    db.session.add(hold)
    db.session.commit()
    return hold


def confirm_booking(
    tenant_id: uuid.UUID,
    booking_id: uuid.UUID,
    paid_amount_paise: int,
    payment_mode: Optional[Any] = None,
    razorpay_payment_id: Optional[str] = None,
) -> Booking:
    """Confirms a pending hold and clears locked_until."""
    booking = db.session.execute(
        select(Booking)
        .filter_by(id=booking_id, tenant_id=tenant_id)
        .with_for_update()
    ).scalar_one_or_none()

    if not booking:
        raise ValueError("Booking not found.")

    booking.status = BookingStatus.CONFIRMED
    booking.locked_until = None
    booking.paid_amount_paise = paid_amount_paise
    db.session.commit()
    return booking


def release_slot_hold(tenant_id: uuid.UUID, booking_id: uuid.UUID) -> Booking:
    """Cancels an active hold and frees the slot immediately."""
    booking = db.session.execute(
        select(Booking)
        .filter_by(id=booking_id, tenant_id=tenant_id)
        .with_for_update()
    ).scalar_one_or_none()

    if not booking:
        raise ValueError("Booking not found.")

    booking.status = BookingStatus.CANCELLED
    booking.locked_until = None
    db.session.commit()
    return booking


def expire_stale_holds(
    tenant_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> int:
    """Marks all past-due PENDING_PAYMENT holds as EXPIRED."""
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)

    stmt = select(Booking).filter(
        Booking.status == BookingStatus.PENDING_PAYMENT,
        Booking.locked_until.is_not(None),
        Booking.locked_until <= effective_now,
    )
    if tenant_id is not None:
        stmt = stmt.filter(Booking.tenant_id == tenant_id)

    stale_list = list(db.session.execute(stmt).scalars().all())
    for b in stale_list:
        b.status = BookingStatus.EXPIRED
        b.locked_until = None

    if stale_list:
        db.session.commit()
    return len(stale_list)