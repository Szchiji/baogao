from __future__ import annotations

import random
import string
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.admin_otp import AdminLoginOtp


def generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))


def generate_browser_token() -> str:
    return str(uuid.uuid4())


async def create_otp(db: AsyncSession) -> AdminLoginOtp:
    otp_code = generate_otp()
    browser_token = generate_browser_token()
    now = datetime.now(tz=timezone.utc)
    expires_at = now + timedelta(minutes=settings.OTP_EXPIRE_MINUTES)

    otp = AdminLoginOtp(
        otp_code=otp_code,
        browser_token=browser_token,
        status="pending",
        expires_at=expires_at,
    )
    db.add(otp)
    await db.flush()
    return otp


async def verify_otp(db: AsyncSession, otp_code: str, telegram_user_id: int) -> bool:
    """Mark an OTP as verified by the admin. Returns True if successful."""
    now = datetime.now(tz=timezone.utc)
    result = await db.execute(
        select(AdminLoginOtp).where(
            AdminLoginOtp.otp_code == otp_code,
            AdminLoginOtp.status == "pending",
            AdminLoginOtp.expires_at > now,
        )
    )
    otp = result.scalar_one_or_none()
    if otp is None:
        return False

    otp.status = "verified"
    otp.telegram_user_id = telegram_user_id
    await db.flush()
    return True


async def get_otp_by_browser_token(
    db: AsyncSession, browser_token: str
) -> AdminLoginOtp | None:
    result = await db.execute(
        select(AdminLoginOtp).where(AdminLoginOtp.browser_token == browser_token)
    )
    return result.scalar_one_or_none()


async def expire_old_otps(db: AsyncSession) -> None:
    now = datetime.now(tz=timezone.utc)
    await db.execute(
        update(AdminLoginOtp)
        .where(AdminLoginOtp.status == "pending", AdminLoginOtp.expires_at <= now)
        .values(status="expired")
    )
