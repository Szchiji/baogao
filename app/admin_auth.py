import time

# In-memory state for admin panel verification (resets on restart, by design)
_verify_codes: dict[str, float] = {}    # code -> expiry_timestamp
_verify_code_otps: dict[str, str] = {}  # code -> otp_token (set after Telegram verification)
_otp_tokens: dict[str, float] = {}      # token -> expiry_timestamp
_verify_attempts: dict[int, list[float]] = {}  # user_id -> list of recent attempt timestamps

_VERIFY_CODE_TTL = 600   # 10 minutes
_OTP_TOKEN_TTL = 300     # 5 minutes
_MAX_VERIFY_ATTEMPTS = 5
_VERIFY_ATTEMPT_WINDOW = 300  # 5 minutes


def _cleanup_verify_state() -> None:
    now = time.time()
    # Use list() copy to avoid "dictionary changed size during iteration" in concurrent access
    for k in list(_verify_codes):
        if _verify_codes.get(k, now + 1) < now:
            _verify_codes.pop(k, None)
            _verify_code_otps.pop(k, None)
    for k in list(_otp_tokens):
        if _otp_tokens.get(k, now + 1) < now:
            _otp_tokens.pop(k, None)
    for uid in list(_verify_attempts):
        recent = [t for t in _verify_attempts.get(uid, []) if t > now - _VERIFY_ATTEMPT_WINDOW]
        if recent:
            _verify_attempts[uid] = recent
        else:
            _verify_attempts.pop(uid, None)


def _is_rate_limited(user_id: int) -> bool:
    now = time.time()
    recent = [t for t in _verify_attempts.get(user_id, []) if t > now - _VERIFY_ATTEMPT_WINDOW]
    return len(recent) >= _MAX_VERIFY_ATTEMPTS


def _record_verify_attempt(user_id: int) -> None:
    now = time.time()
    attempts = _verify_attempts.get(user_id, [])
    attempts = [t for t in attempts if t > now - _VERIFY_ATTEMPT_WINDOW]
    attempts.append(now)
    _verify_attempts[user_id] = attempts


def _is_verify_code(text: str) -> bool:
    """Return True if text looks like an admin verify code (12 uppercase hex chars)."""
    if len(text) != 12:
        return False
    return all(c in "0123456789ABCDEF" for c in text)
