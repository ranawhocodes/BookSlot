import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Tuple, Dict, Any
from sqlalchemy import select, and_, or_, func
from app.extensions import db
from app.models import (
    Booking,
    BookingStatus,
    Resource,
    AvailabilityRule,
    PaymentMode,
    Tenant,
)
from app.services.pricing import calculate_slot_price, calculate_booking_quote


class SlotEngineError(Exception):
    """Base exception for slot engine operations."""
    pass


class SlotUnavailableError(SlotEngineError):
    """Raised when a requested slot is already booked or currently locked."""
    pass


class BookingNotFoundError(SlotEngineError):
    """Raised when a booking record cannot be found within the tenant."""
    pass


class InvalidSlotError(SlotEngineError):
    """Raised when requested slot timing is invalid or falls on a break."""
    pass


@dataclass
class SlotAvailability:
    start_time: time
    end_time: time
    status: str  # 'AVAILABLE', 'LOCKED', 'BOOKED', 'BREAK'
    price_paise: int
    is_peak: bool
    existing_booking_id: Optional[str] = None
    locked_until: Optional[datetime] = None


def _get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _add_minutes_to_time(t: time, minutes: int) -> Tuple[time, bool]:
    """Adds minutes to a time object. Returns (new_time, next_day_overflow)."""
    total_minutes = t.hour * 60 + t.minute + minutes
    overflow = total_minutes >= 24 * 60
    total_minutes %= (24 * 60)
    return time(total_minutes // 60, total_minutes % 60), overflow


def _time_diff_minutes(t1: time, t2: time) -> int:
    """Returns minutes difference (t2 - t1), assuming same day."""
    return (t2.hour * 60 + t2.minute) - (t1.hour * 60 + t1.minute)


def get_available_slots(
    tenant_id: uuid.UUID,
    resource_id: uuid.UUID,
    slot_date: date,
    now: Optional[datetime] = None,
) -> List[SlotAvailability]:
    """
    Calculates all slots for a given date, determining whether each is
    AVAILABLE, LOCKED (active 7-min hold), BOOKED (confirmed/blocked), or BREAK.
    Always scopes queries by tenant_id.
    """
    if now is None:
        now = _get_utc_now()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # 1. Fetch resource and verify tenant scoping
    resource = db.session.execute(
        select(Resource).filter(
            Resource.tenant_id == tenant_id,
            Resource.id == resource_id,
            Resource.is_active.is_(True)
        )
    ).scalar_one_or_none()

    if not resource:
        raise ValueError(f"Resource {resource_id} not found for tenant {tenant_id}")

    # 2. Fetch availability rules for this day of week
    day_of_week = slot_date.weekday()
    rules = db.session.execute(
        select(AvailabilityRule).filter(
            AvailabilityRule.tenant_id == tenant_id,
            AvailabilityRule.resource_id == resource_id,
            AvailabilityRule.day_of_week == day_of_week
        ).order_by(AvailabilityRule.start_time.asc())
    ).scalars().all()

    if not rules:
        return []

    # 3. Fetch existing bookings for this resource and date (scoped by tenant_id)
    existing_bookings = db.session.execute(
        select(Booking).filter(
            Booking.tenant_id == tenant_id,
            Booking.resource_id == resource_id,
            Booking.slot_date == slot_date,
            Booking.status.in_([
                BookingStatus.CONFIRMED,
                BookingStatus.BLOCKED,
                BookingStatus.COMPLETED,
                BookingStatus.PENDING_PAYMENT
            ])
        )
    ).scalars().all()

    # Map bookings by start_time. For PENDING_PAYMENT, check if hold is active or expired.
    active_bookings_by_start: Dict[time, Booking] = {}
    for b in existing_bookings:
        if b.status in (BookingStatus.CONFIRMED, BookingStatus.BLOCKED, BookingStatus.COMPLETED):
            active_bookings_by_start[b.start_time] = b
        elif b.status == BookingStatus.PENDING_PAYMENT:
            # Check if hold is active: locked_until > now
            if b.locked_until:
                locked_until_utc = b.locked_until if b.locked_until.tzinfo else b.locked_until.replace(tzinfo=timezone.utc)
                if locked_until_utc > now:
                    active_bookings_by_start[b.start_time] = b
            else:
                # If no locked_until, treat as active hold or ignore
                active_bookings_by_start[b.start_time] = b

    # 4. Generate discrete slots across all rules for the day
    slot_duration = resource.slot_duration_mins
    slots: List[SlotAvailability] = []
    break_rules = [r for r in rules if r.is_break]

    for rule in rules:
        current_time = rule.start_time
        rule_end = rule.end_time

        while _time_diff_minutes(current_time, rule_end) >= slot_duration:
            next_time, _ = _add_minutes_to_time(current_time, slot_duration)

            if rule.is_break:
                slots.append(
                    SlotAvailability(
                        start_time=current_time,
                        end_time=next_time,
                        status="BREAK",
                        price_paise=0,
                        is_peak=False,
                    )
                )
            else:
                # Check if slot falls in any break window
                in_break = any(
                    br.start_time <= current_time and br.end_time >= next_time
                    for br in break_rules
                )

                if in_break:
                    slots.append(
                        SlotAvailability(
                            start_time=current_time,
                            end_time=next_time,
                            status="BREAK",
                            price_paise=0,
                            is_peak=False,
                        )
                    )
                else:
                    price_info = calculate_slot_price(resource, slot_date, current_time, next_time, rules=rules)
                    booking = active_bookings_by_start.get(current_time)

                    if booking:
                        if booking.status in (BookingStatus.CONFIRMED, BookingStatus.BLOCKED, BookingStatus.COMPLETED):
                            slot_status = "BOOKED"
                        else:
                            slot_status = "LOCKED"
                        slots.append(
                            SlotAvailability(
                                start_time=current_time,
                                end_time=next_time,
                                status=slot_status,
                                price_paise=price_info.price_paise,
                                is_peak=price_info.is_peak,
                                existing_booking_id=str(booking.id),
                                locked_until=booking.locked_until,
                            )
                        )
                    else:
                        slots.append(
                            SlotAvailability(
                                start_time=current_time,
                                end_time=next_time,
                                status="AVAILABLE",
                                price_paise=price_info.price_paise,
                                is_peak=price_info.is_peak,
                            )
                        )

            current_time = next_time

    return slots


def create_slot_hold(
    tenant_id: uuid.UUID,
    resource_id: uuid.UUID,
    slot_date: date,
    start_time: time,
    end_time: time,
    customer_id: Optional[uuid.UUID] = None,
    hold_duration_minutes: int = 7,
    created_by_user_id: Optional[uuid.UUID] = None,
    booking_notes: Optional[str] = None,
    total_amount_paise: Optional[int] = None,
    paid_amount_paise: int = 0,
    is_walkin: bool = False,
    token_number: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Booking:
    """
    Places an atomic 7-minute hold on a resource slot.

    Guarantees:
    - Scoped strictly to tenant_id.
    - If slot is CONFIRMED or BLOCKED, raises SlotUnavailableError.
    - If slot is PENDING_PAYMENT and locked_until > now, raises SlotUnavailableError.
    - If slot is PENDING_PAYMENT and locked_until <= now, hold has EXPIRED:
      marks the stale hold as EXPIRED and allows the re-booking hold.
    - Atomically persists the new hold with status='PENDING_PAYMENT'.
    """
    if now is None:
        now = _get_utc_now()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # 1. Fetch resource and verify tenant
    resource = db.session.execute(
        select(Resource).filter(
            Resource.tenant_id == tenant_id,
            Resource.id == resource_id,
            Resource.is_active.is_(True)
        )
    ).scalar_one_or_none()

    if not resource:
        raise ValueError(f"Resource {resource_id} not found for tenant {tenant_id}")

    # 2. Compute slot price if total_amount_paise not provided
    if total_amount_paise is None:
        quote = calculate_booking_quote(
            resource=resource,
            slot_date=slot_date,
            slot_intervals=[(start_time, end_time)],
        )
        total_amount_paise = quote.total_amount_paise

    # 3. Lock / query existing bookings for the exact slot
    # Use with_for_update if dialect supports it (PostgreSQL).
    stmt = (
        select(Booking)
        .filter(
            Booking.tenant_id == tenant_id,
            Booking.resource_id == resource_id,
            Booking.slot_date == slot_date,
            Booking.start_time == start_time,
            Booking.status.in_([
                BookingStatus.CONFIRMED,
                BookingStatus.BLOCKED,
                BookingStatus.PENDING_PAYMENT,
            ])
        )
    )

    # Apply with_for_update safely depending on database dialect
    bind = db.session.get_bind()
    if bind and bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()

    existing_bookings = db.session.execute(stmt).scalars().all()

    for existing in existing_bookings:
        if existing.status in (BookingStatus.CONFIRMED, BookingStatus.BLOCKED):
            raise SlotUnavailableError(
                f"Slot {slot_date} {start_time}-{end_time} is already booked or blocked."
            )

        if existing.status == BookingStatus.PENDING_PAYMENT:
            locked_until_utc = (
                existing.locked_until
                if (existing.locked_until and existing.locked_until.tzinfo)
                else (existing.locked_until.replace(tzinfo=timezone.utc) if existing.locked_until else None)
            )

            if locked_until_utc and locked_until_utc > now:
                raise SlotUnavailableError(
                    f"Slot {slot_date} {start_time}-{end_time} is temporarily held by another customer until {locked_until_utc.isoformat()}."
                )
            else:
                # Hold expired! Mark as EXPIRED so new hold can take over cleanly
                existing.status = BookingStatus.EXPIRED

    # 4. Create new hold
    locked_until = now + timedelta(minutes=hold_duration_minutes)

    new_booking = Booking(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        resource_id=resource_id,
        customer_id=customer_id,
        slot_date=slot_date,
        start_time=start_time,
        end_time=end_time,
        status=BookingStatus.PENDING_PAYMENT,
        locked_until=locked_until,
        is_walkin=is_walkin,
        token_number=token_number,
        total_amount_paise=total_amount_paise,
        paid_amount_paise=paid_amount_paise,
        created_by_user_id=created_by_user_id,
        booking_notes=booking_notes,
    )

    db.session.add(new_booking)
    db.session.commit()
    return new_booking


def confirm_booking(
    tenant_id: uuid.UUID,
    booking_id: uuid.UUID,
    paid_amount_paise: Optional[int] = None,
    payment_mode: Optional[PaymentMode] = None,
    razorpay_payment_id: Optional[str] = None,
) -> Booking:
    """
    Confirms a pending booking upon payment capture or manual venue override.
    Clears locked_until and updates paid amount.
    """
    booking = db.session.execute(
        select(Booking).filter(
            Booking.tenant_id == tenant_id,
            Booking.id == booking_id,
        )
    ).scalar_one_or_none()

    if not booking:
        raise BookingNotFoundError(f"Booking {booking_id} not found for tenant {tenant_id}")

    if booking.status == BookingStatus.CONFIRMED:
        return booking

    booking.status = BookingStatus.CONFIRMED
    booking.locked_until = None

    if paid_amount_paise is not None:
        booking.paid_amount_paise = paid_amount_paise

    db.session.commit()
    return booking


def release_slot_hold(
    tenant_id: uuid.UUID,
    booking_id: uuid.UUID,
) -> Booking:
    """Cancels or expires a pending slot hold, making the slot available immediately."""
    booking = db.session.execute(
        select(Booking).filter(
            Booking.tenant_id == tenant_id,
            Booking.id == booking_id,
        )
    ).scalar_one_or_none()

    if not booking:
        raise BookingNotFoundError(f"Booking {booking_id} not found for tenant {tenant_id}")

    if booking.status == BookingStatus.PENDING_PAYMENT:
        booking.status = BookingStatus.CANCELLED
        booking.locked_until = None
        db.session.commit()

    return booking


def expire_stale_holds(
    tenant_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> int:
    """
    Explicit bulk expiration maintenance function.
    Finds PENDING_PAYMENT bookings where locked_until <= now, updates to EXPIRED.
    """
    if now is None:
        now = _get_utc_now()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    filters = [
        Booking.status == BookingStatus.PENDING_PAYMENT,
        Booking.locked_until.is_not(None),
        Booking.locked_until <= now,
    ]
    if tenant_id:
        filters.append(Booking.tenant_id == tenant_id)

    stale_bookings = db.session.execute(
        select(Booking).filter(and_(*filters))
    ).scalars().all()

    count = len(stale_bookings)
    for b in stale_bookings:
        b.status = BookingStatus.EXPIRED

    if count > 0:
        db.session.commit()

    return count
