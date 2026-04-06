from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.report import Report
from app.schemas.report import ReportOut

router = APIRouter(prefix="/reports", tags=["reports"])

DbDep = Annotated[AsyncSession, Depends(get_db)]


@router.get("/", response_model=list[ReportOut])
async def list_reports(
    db: DbDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = None,
):
    q = select(Report).order_by(Report.report_number.desc())
    if status:
        q = q.where(Report.status == status)
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/{report_id}", response_model=ReportOut)
async def get_report(report_id: uuid.UUID, db: DbDep):
    result = await db.execute(select(Report).where(Report.id == report_id))
    report = result.scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report
