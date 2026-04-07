from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton  # type: ignore
from aiogram.utils.keyboard import InlineKeyboardBuilder  # type: ignore


def build_select_keyboard(options: list[dict], field_key: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for opt in options:
        builder.button(
            text=opt.get("label", opt.get("value", "?")),
            callback_data=f"select:{field_key}:{opt.get('value', '')}",
        )
    builder.adjust(2)
    return builder.as_markup()


def build_done_keyboard(callback_data: str = "media_done") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ 完成", callback_data=callback_data)
    return builder.as_markup()


def build_admin_review_keyboard(report_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ 批准", callback_data=f"approve_{report_id}")
    builder.button(text="❌ 拒绝", callback_data=f"reject_{report_id}")
    builder.button(text="ℹ️ 需补充", callback_data=f"needmore_{report_id}")
    builder.adjust(3)
    return builder.as_markup()
