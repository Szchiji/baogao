import secrets
import time

# In-memory state for admin panel verification (resets on restart, by design)
_verify_codes: dict[str, float] = {}    # code -> expiry_timestamp
_verify_code_otps: dict[str, str] = {}  # code -> otp_token (set after Telegram verification)
# Each OTP token value is {"expiry": float, "owner_user_id": int | None}.
# owner_user_id is set when the OTP was generated for a child-bot sub-admin so
# the web layer can create a restricted session instead of granting full access.
_otp_tokens: dict[str, dict] = {}       # token -> {"expiry": float, "owner_user_id": int | None}
_verify_attempts: dict[int, list[float]] = {}  # user_id -> list of recent attempt timestamps

# Child-admin sessions: sub-admins who logged in via a child-bot OTP receive a
# short-lived session token instead of the full admin_panel_token cookie.
_child_admin_sessions: dict[str, int] = {}   # session_token -> owner_user_id
_child_session_expiry: dict[str, float] = {} # session_token -> expiry_timestamp

_VERIFY_CODE_TTL = 600   # 10 minutes
_OTP_TOKEN_TTL = 300     # 5 minutes
_MAX_VERIFY_ATTEMPTS = 5
_VERIFY_ATTEMPT_WINDOW = 300  # 5 minutes
_CHILD_SESSION_TTL = 3600    # 1 hour


def _cleanup_verify_state() -> None:
    now = time.time()
    # Use list() copy to avoid "dictionary changed size during iteration" in concurrent access
    for k in list(_verify_codes):
        if _verify_codes.get(k, now + 1) < now:
            _verify_codes.pop(k, None)
            _verify_code_otps.pop(k, None)
    for k in list(_otp_tokens):
        entry = _otp_tokens.get(k)
        expiry = entry["expiry"] if isinstance(entry, dict) else (entry or now + 1)
        if expiry < now:
            _otp_tokens.pop(k, None)
    for k in list(_child_session_expiry):
        if _child_session_expiry.get(k, now + 1) < now:
            _child_session_expiry.pop(k, None)
            _child_admin_sessions.pop(k, None)
    for uid in list(_verify_attempts):
        recent = [t for t in _verify_attempts.get(uid, []) if t > now - _VERIFY_ATTEMPT_WINDOW]
        if recent:
            _verify_attempts[uid] = recent
        else:
            _verify_attempts.pop(uid, None)


def create_child_admin_session(owner_user_id: int) -> str:
    """Create a short-lived session token for a child-bot sub-admin.

    Returns the session token which should be stored in the browser as an
    ``admin_child_session`` cookie.
    """
    token = secrets.token_urlsafe(24)
    _child_admin_sessions[token] = owner_user_id
    _child_session_expiry[token] = time.time() + _CHILD_SESSION_TTL
    return token


def get_child_admin_id(session_token: str) -> int | None:
    """Return the owner_user_id for a valid child-admin session, or None."""
    expiry = _child_session_expiry.get(session_token)
    if expiry is None or time.time() > expiry:
        return None
    return _child_admin_sessions.get(session_token)


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
