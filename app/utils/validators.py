# app/utils/validators.py
import re
import time
from collections import defaultdict
from functools import wraps
from typing import Tuple, Optional
from flask import request, jsonify, current_app

# Strict 10-digit Indian mobile number starting with 6, 7, 8, or 9
INDIAN_PHONE_REGEX = re.compile(r"^[6-9][0-9]{9}$")

# Clean person name: 2 to 60 chars, letters, spaces, dots, apostrophes, hyphens
CLEAN_NAME_REGEX = re.compile(r"^[A-Za-z][A-Za-z\s\.\'\-]{1,59}$")

# Detect obvious keyboard mashing (e.g., 5+ consecutive consonants or repeated chars)
REPEATED_CHAR_REGEX = re.compile(r"(.)\1{3,}", re.IGNORECASE)

# In-memory sliding window store for IP rate limiting: {ip_key: [timestamps]}
_RATE_LIMIT_STORE = defaultdict(list)


def sanitize_and_validate_phone(raw_phone: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Strips +91, spaces, and hyphens, then validates strict 10-digit Indian mobile format.
    Returns (cleaned_phone, error_message).
    """
    if not raw_phone:
        return None, "Mobile number is required."
    cleaned = re.sub(r"[\s\-\(\)]", "", str(raw_phone).strip())
    if cleaned.startswith("+91"):
        cleaned = cleaned[3:]
    elif cleaned.startswith("91") and len(cleaned) == 12:
        cleaned = cleaned[2:]
    elif cleaned.startswith("0") and len(cleaned) == 11:
        cleaned = cleaned[1:]

    if not INDIAN_PHONE_REGEX.match(cleaned):
        return None, "Enter a valid 10-digit Indian WhatsApp mobile number (starting with 6, 7, 8, or 9)."
    return cleaned, None


def sanitize_and_validate_name(raw_name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Validates customer/patient name (2-60 chars, alphabetic, no script tags or numbers).
    Returns (cleaned_name, error_message).
    """
    if not raw_name:
        return None, "Full name is required."
    cleaned = " ".join(str(raw_name).strip().split())

    if len(cleaned) < 2 or len(cleaned) > 60:
        return None, "Name must be between 2 and 60 characters."

    if not CLEAN_NAME_REGEX.match(cleaned):
        return None, "Name can only contain letters, spaces, and periods (no numbers or special symbols)."

    if REPEATED_CHAR_REGEX.search(cleaned):
        return None, "Please enter a valid real name."

    return cleaned, None


def check_honeypot() -> bool:
    """
    Returns True if a bot filled out the hidden 'website_url' honeypot input.
    """
    honeypot_val = request.form.get("website_url", "").strip()
    return bool(honeypot_val)


def rate_limit(max_requests: int = 5, window_seconds: int = 60, key_prefix: str = "rl"):
    """
    Zero-dependency sliding-window IP rate limiter decorator for public endpoints.
    Automatically bypassed when app.config['TESTING'] is True unless explicitly enabled.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if current_app.config.get("TESTING") and not current_app.config.get("ENFORCE_RATE_LIMIT_IN_TESTS"):
                return f(*args, **kwargs)

            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1").split(",")[0].strip()
            bucket_key = f"{key_prefix}:{ip}"
            now = time.time()

            # Prune expired timestamps outside window
            _RATE_LIMIT_STORE[bucket_key] = [
                ts for ts in _RATE_LIMIT_STORE[bucket_key] if now - ts < window_seconds
            ]

            if len(_RATE_LIMIT_STORE[bucket_key]) >= max_requests:
                msg = "Too many booking requests from your network. Please wait 1 minute and try again."
                if request.headers.get("HX-Request"):
                    return (
                        f'<div class="p-4 rounded-xl bg-rose-950/90 border border-rose-500/40 text-rose-200 text-xs font-semibold">'
                        f'{msg}</div>',
                        429,
                    )
                return jsonify({"error": msg}), 429

            _RATE_LIMIT_STORE[bucket_key].append(now)
            return f(*args, **kwargs)
        return wrapped
    return decorator