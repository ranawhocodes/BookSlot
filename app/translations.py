from typing import Dict, Any

TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "en": {
        "court_turf_select": "Select Court / Turf",
        "doctor_select": "Select Doctor / Consultant",
        "select_date": "Select Date",
        "today": "Today",
        "tomorrow": "Tomorrow",
        "slots_available": "Slots Available",
        "duration": "duration",
        "day_slots_offpeak": "Day Slots (Off-Peak)",
        "floodlight_peak": "Evening Floodlight Peak Slots",
        "book_arrow": "Book →",
        "held_7m": "Held (7m)",
        "booked": "Booked",
        "pay_advance_upi": "Pay {pct}% Advance via UPI",
        "pay_rest_venue": "Pay rest at venue",
        "advance_pct": "Advance ({pct}%)",
        "pay_full": "Pay Full",
        "online_100": "100% Online",
        "balance_venue": "Balance at venue",
        "full_name": "Full Name",
        "mobile_number": "WhatsApp Mobile Number",
        "dpdp_consent_label": "I agree to receive booking updates via WhatsApp/SMS and consent to processing my booking details under the DPDP Act 2023.",
        "hold_and_pay": "Hold Slot & Proceed to Pay",
        "secure_7m_lock": "7-Minute Lock Active",
        "complete_payment": "Complete Payment via UPI",
        "confirmed": "Booking Confirmed!",
        "no_slots_date": "No slots available for this date.",
        "slots_heading": "Available Booking Slots",
        "call_venue": "Call Venue",
    },
    "ta": {
        "court_turf_select": "மைதானம் / தளத்தைத் தேர்ந்தெடுக்கவும்",
        "doctor_select": "மருத்துவர் / சேவையைத் தேர்ந்தெடுக்கவும்",
        "select_date": "தேதியைத் தேர்ந்தெடுக்கவும்",
        "today": "இன்று",
        "tomorrow": "நாளை",
        "slots_available": "கிடைக்கும் முன்பதிவு நேரங்கள்",
        "duration": "நேர அளவு",
        "day_slots_offpeak": "பகல் நேரங்கள் (சாதாரண கட்டணம்)",
        "floodlight_peak": "மின்விளக்கு இரவு நேரங்கள் (பீக் ஹவர்ஸ்)",
        "book_arrow": "முன்பதிவு →",
        "held_7m": "முன்பதிவு பூட்டப்பட்டுள்ளது (7 நிமி)",
        "booked": "முன்பதிவு செய்யப்பட்டது",
        "pay_advance_upi": "UPI மூலம் {pct}% முன்பணம் செலுத்தவும்",
        "pay_rest_venue": "மீதியை மைதானத்தில் செலுத்தவும்",
        "advance_pct": "முன்பணம் ({pct}%)",
        "pay_full": "முழுத் தொகை",
        "online_100": "100% ஆன்லைன்",
        "balance_venue": "மைதானத்தில் செலுத்த வேண்டியது",
        "full_name": "முழுப் பெயர்",
        "mobile_number": "வாட்ஸ்அப் கைபேசி எண்",
        "dpdp_consent_label": "வாட்ஸ்அப்/எஸ்எம்எஸ் மூலம் முன்பதிவு தகவல்களைப் பெற ஒப்புக்கொள்கிறேன் (DPDP சட்டம் 2023).",
        "hold_and_pay": "இடத்தைப் பூட்டி பணம் செலுத்த தொடரவும்",
        "secure_7m_lock": "7 நிமிட முன்பதிவு பூட்டு செயலில் உள்ளது",
        "complete_payment": "UPI மூலம் பணம் செலுத்துங்கள்",
        "confirmed": "முன்பதிவு உறுதிசெய்யப்பட்டது!",
        "no_slots_date": "இந்த தேதியில் முன்பதிவு நேரங்கள் இல்லை.",
        "slots_heading": "கிடைக்கும் முன்பதிவு நேரங்கள்",
        "call_venue": "தொடர்பு கொள்ள",
    }
}

TAMIL_DAYS = {
    "Mon": "திங்கள்",
    "Tue": "செவ்வாய்",
    "Wed": "புதன்",
    "Thu": "வியாழன்",
    "Fri": "வெள்ளி",
    "Sat": "சனி",
    "Sun": "ஞாயிறு",
}

TAMIL_MONTHS = {
    "Jan": "ஜன",
    "Feb": "பிப்",
    "Mar": "மார்ச்",
    "Apr": "ஏப்",
    "May": "மே",
    "Jun": "ஜூன்",
    "Jul": "ஜூலை",
    "Aug": "ஆக",
    "Sep": "செப்",
    "Oct": "அக்",
    "Nov": "நவ",
    "Dec": "டிச",
}


def get_text(key: str, lang: str = "en", **kwargs: Any) -> str:
    """Retrieves localized text for given key and language code."""
    lang_code = lang if lang in TRANSLATIONS else "en"
    template = TRANSLATIONS[lang_code].get(key, TRANSLATIONS["en"].get(key, key))
    if kwargs:
        try:
            return template.format(**kwargs)
        except Exception:
            return template
    return template
