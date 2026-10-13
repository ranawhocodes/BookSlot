from dataclasses import dataclass
from datetime import date, time
from typing import List, Optional, Sequence
from app.models import Resource, AvailabilityRule, Tenant


@dataclass(frozen=True)
class SlotPriceItem:
    start_time: time
    end_time: time
    price_paise: int
    is_peak: bool
    is_break: bool
    applied_rule_id: Optional[str] = None


@dataclass(frozen=True)
class BookingQuote:
    total_amount_paise: int
    advance_amount_paise: int
    balance_amount_paise: int
    advance_percentage: int
    slots: List[SlotPriceItem]


def calculate_slot_price(
    resource: Resource,
    slot_date: date,
    start_time: time,
    end_time: time,
    rules: Optional[Sequence[AvailabilityRule]] = None,
) -> SlotPriceItem:
    """
    Calculates the price for a specific slot on a given date and time range.

    Precedence:
    1. If a matching rule on that day_of_week spans the slot and has is_break=True:
       marked as break.
    2. If a matching rule on that day_of_week spans the slot and has price_override_paise:
       use override price (peak or special rate).
    3. Otherwise: fallback to resource.base_price_paise.
    """
    day_of_week = slot_date.weekday()  # 0=Monday, 6=Sunday

    if rules is None:
        rules = resource.availability_rules

    # Filter rules for the specific day of week
    day_rules = [r for r in rules if r.day_of_week == day_of_week]

    # Look for a specific rule covering this slot window
    matching_rule: Optional[AvailabilityRule] = None
    for rule in day_rules:
        # A slot falls within a rule if rule.start_time <= start_time and rule.end_time >= end_time
        if rule.start_time <= start_time and rule.end_time >= end_time:
            matching_rule = rule
            break

    if matching_rule and matching_rule.is_break:
        return SlotPriceItem(
            start_time=start_time,
            end_time=end_time,
            price_paise=0,
            is_peak=False,
            is_break=True,
            applied_rule_id=str(matching_rule.id) if matching_rule else None,
        )

    if matching_rule and matching_rule.price_override_paise is not None:
        price_paise = matching_rule.price_override_paise
        is_peak = price_paise > resource.base_price_paise
        return SlotPriceItem(
            start_time=start_time,
            end_time=end_time,
            price_paise=price_paise,
            is_peak=is_peak,
            is_break=False,
            applied_rule_id=str(matching_rule.id),
        )

    # Default to resource base price
    return SlotPriceItem(
        start_time=start_time,
        end_time=end_time,
        price_paise=resource.base_price_paise,
        is_peak=False,
        is_break=False,
        applied_rule_id=str(matching_rule.id) if matching_rule else None,
    )


def calculate_booking_quote(
    resource: Resource,
    slot_date: date,
    slot_intervals: Sequence[tuple[time, time]],
    tenant: Optional[Tenant] = None,
    rules: Optional[Sequence[AvailabilityRule]] = None,
) -> BookingQuote:
    """
    Calculates total price, advance payable amount, and balance due for a booking.

    - slot_intervals: sequence of (start_time, end_time)
    - tenant: optional tenant to inspect advance_percentage (default 100%)
    """
    if not slot_intervals:
        raise ValueError("At least one slot interval must be provided.")

    slot_items: List[SlotPriceItem] = []
    total_paise = 0

    for start_t, end_t in slot_intervals:
        item = calculate_slot_price(resource, slot_date, start_t, end_t, rules=rules)
        if item.is_break:
            raise ValueError(f"Cannot book during break window: {start_t} - {end_t}")
        slot_items.append(item)
        total_paise += item.price_paise

    advance_pct = tenant.advance_percentage if tenant else 100
    # Ensure advance_percentage is clamped between 0 and 100
    advance_pct = max(0, min(100, advance_pct))

    if advance_pct == 100:
        advance_paise = total_paise
    elif advance_pct == 0:
        advance_paise = 0
    else:
        advance_paise = int(round(total_paise * (advance_pct / 100.0)))

    balance_paise = max(0, total_paise - advance_paise)

    return BookingQuote(
        total_amount_paise=total_paise,
        advance_amount_paise=advance_paise,
        balance_amount_paise=balance_paise,
        advance_percentage=advance_pct,
        slots=slot_items,
    )
