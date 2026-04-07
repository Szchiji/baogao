from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.database import get_db
from app.models.subscription import Subscription

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

DbDep = Annotated[AsyncSession, Depends(get_db)]


class SubscriptionCreate(BaseModel):
    chat_id: int
    label: str | None = None


class SubscriptionOut(BaseModel):
    id: uuid.UUID
    chat_id: int
    label: str | None
    enabled: bool

    class Config:
        from_attributes = True


@router.get("/", response_model=list[SubscriptionOut])
async def list_subscriptions(db: DbDep):
    result = await db.execute(select(Subscription).order_by(Subscription.created_at))
    return result.scalars().all()


@router.post("/", response_model=SubscriptionOut, status_code=201)
async def create_subscription(body: SubscriptionCreate, db: DbDep):
    existing = await db.execute(
        select(Subscription).where(Subscription.chat_id == body.chat_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Subscription already exists")
    sub = Subscription(chat_id=body.chat_id, label=body.label)
    db.add(sub)
    await db.flush()
    await db.refresh(sub)
    await db.commit()
    return sub


@router.delete("/{sub_id}", status_code=204)
async def delete_subscription(sub_id: uuid.UUID, db: DbDep):
    result = await db.execute(select(Subscription).where(Subscription.id == sub_id))
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    await db.delete(sub)
    await db.commit()
