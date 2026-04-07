from __future__ import annotations

from fastapi import APIRouter, Request, HTTPException
from app.bot.main import get_dispatcher, get_bot

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post("/webhook")
async def telegram_webhook(request: Request):
    from aiogram.types import Update  # type: ignore

    dp = get_dispatcher()
    bot = get_bot()
    if dp is None or bot is None:
        raise HTTPException(status_code=503, detail="Bot not initialised")

    body = await request.json()
    update = Update.model_validate(body)
    await dp.feed_update(bot=bot, update=update)
    return {"ok": True}
