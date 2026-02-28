from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..db import supabase

router = APIRouter()


class NoticeReadPayload(BaseModel):
    student_id: str | None = None
    user_id: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _single_or_none(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


@router.get('')
def list_notices(student_id: str | None = None, is_admin: bool = False) -> list[dict[str, Any]]:
    now = _now_iso()
    result = (
        supabase.table('notices')
        .select('*')
        .eq('is_active', True)
        .or_(f'expires_at.is.null,expires_at.gt.{now}')
        .order('is_pinned', desc=True)
        .order('created_at', desc=True)
        .execute()
    )
    notices = result.data or []

    student = None
    if student_id:
        student_res = supabase.table('students').select('id,branch,batch_year').eq('id', student_id).limit(1).execute()
        student = _single_or_none(student_res.data)

    filtered: list[dict[str, Any]] = []
    for notice in notices:
        if student:
            target_branches = notice.get('target_branches') or []
            target_batches = notice.get('target_batches') or []
            if target_branches and student.get('branch') not in target_branches:
                continue
            if target_batches and student.get('batch_year') not in target_batches:
                continue

        notice_row = dict(notice)
        if student_id:
            read_res = (
                supabase.table('notice_reads')
                .select('id')
                .eq('notice_id', notice['id'])
                .eq('user_id', student_id)
                .limit(1)
                .execute()
            )
            notice_row['is_read'] = bool(read_res.data)

        if is_admin:
            count_res = (
                supabase.table('notice_reads')
                .select('id', count='exact')
                .eq('notice_id', notice['id'])
                .execute()
            )
            notice_row['read_count'] = count_res.count or 0

        filtered.append(notice_row)

    return filtered


@router.post('/{notice_id}/read')
def mark_notice_read(notice_id: str, payload: NoticeReadPayload) -> dict[str, bool]:
    user_id = payload.user_id or payload.student_id
    if not user_id:
        return {'success': False}

    supabase.table('notice_reads').upsert({'notice_id': notice_id, 'user_id': user_id}).execute()
    return {'success': True}
