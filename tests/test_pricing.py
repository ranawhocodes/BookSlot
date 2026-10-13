import pytest
from datetime import date, time
from app.services.pricing import calculate_slot_price, calculate_booking_quote
from app.models import Resource


def test_weekday_off_peak_pricing(turf_setup):
    """Mon-Fri between 06:00 and 17:00 should charge off-peak base price."""
    football = turf_setup["football"]
    wednesday = date(2026, 10, 14)  # Wednesday (weekday)

    price_item = calculate_slot_price(
        resource=football,
        slot_date=wednesday,
        start_time=time(10, 0),
        end_time=time(11, 0),
    )
    assert price_item.price_paise == 80000  # ₹800
    assert price_item.is_peak is False
    assert price_item.is_break is False


def test_weekday_peak_floodlight_pricing(turf_setup):
    """Mon-Fri between 17:00 and 23:00 should charge peak floodlight override (₹1,200)."""
    football = turf_setup["football"]
    wednesday = date(2026, 10, 14)  # Wednesday

    price_item = calculate_slot_price(
        resource=football,
        slot_date=wednesday,
        start_time=time(18, 0),
        end_time=time(19, 0),
    )
    assert price_item.price_paise == 120000  # ₹1,200
    assert price_item.is_peak is True
    assert price_item.is_break is False


def test_weekend_peak_pricing(turf_setup):
    """Sat-Sun all day should charge weekend override price (₹1,400)."""
    football = turf_setup["football"]
    saturday = date(2026, 10, 17)  # Saturday

    # Morning slot
    morning_item = calculate_slot_price(
        resource=football,
        slot_date=saturday,
        start_time=time(8, 0),
        end_time=time(9, 0),
    )
    assert morning_item.price_paise == 140000  # ₹1,400
    assert morning_item.is_peak is True

    # Evening slot
    evening_item = calculate_slot_price(
        resource=football,
        slot_date=saturday,
        start_time=time(19, 0),
        end_time=time(20, 0),
    )
    assert evening_item.price_paise == 140000  # ₹1,400
    assert evening_item.is_peak is True


def test_multi_slot_quote_with_30_percent_advance(turf_setup):
    """Multi-hour booking quote correctly splits 30% UPI advance and 70% venue balance."""
    football = turf_setup["football"]
    tenant = turf_setup["tenant"]  # advance_percentage = 30
    wednesday = date(2026, 10, 14)

    # 2 contiguous peak hours: 18:00 - 20:00 (₹1,200 + ₹1,200 = ₹2,400)
    quote = calculate_booking_quote(
        resource=football,
        slot_date=wednesday,
        slot_intervals=[(time(18, 0), time(19, 0)), (time(19, 0), time(20, 0))],
        tenant=tenant,
    )

    assert quote.total_amount_paise == 240000  # ₹2,400
    assert quote.advance_amount_paise == 72000  # 30% = ₹720
    assert quote.balance_amount_paise == 168000  # 70% = ₹1,680
    assert quote.advance_percentage == 30
    assert len(quote.slots) == 2


def test_full_advance_quote_for_clinic(clinic_setup):
    """Clinic 100% advance quote charges full amount and leaves 0 balance."""
    doctor = clinic_setup["doctor"]
    tenant = clinic_setup["tenant"]  # advance_percentage = 100
    thursday = date(2026, 10, 15)

    quote = calculate_booking_quote(
        resource=doctor,
        slot_date=thursday,
        slot_intervals=[(time(10, 0), time(10, 20))],
        tenant=tenant,
    )

    assert quote.total_amount_paise == 30000  # ₹300
    assert quote.advance_amount_paise == 30000
    assert quote.balance_amount_paise == 0


def test_quote_raises_error_on_break_window(clinic_setup):
    """Attempting to quote/book during a designated break window raises an error."""
    doctor = clinic_setup["doctor"]
    thursday = date(2026, 10, 15)

    # Break is from 13:30 to 16:30
    with pytest.raises(ValueError, match="Cannot book during break window"):
        calculate_booking_quote(
            resource=doctor,
            slot_date=thursday,
            slot_intervals=[(time(14, 0), time(14, 20))],
        )


def test_base_price_fallback_when_no_rules_configured(db_session, turf_setup):
    """If no rule overrides exist for a resource, it falls back to base_price_paise."""
    tenant = turf_setup["tenant"]
    unconfigured_res = Resource(
        tenant_id=tenant.id,
        name="Pickleball Court 1",
        category="Pickleball",
        slot_duration_mins=60,
        base_price_paise=50000,
    )
    db_session.add(unconfigured_res)
    db_session.flush()

    price_item = calculate_slot_price(
        resource=unconfigured_res,
        slot_date=date(2026, 10, 14),
        start_time=time(10, 0),
        end_time=time(11, 0),
        rules=[],
    )
    assert price_item.price_paise == 50000
    assert price_item.is_peak is False
