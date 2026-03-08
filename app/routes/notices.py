from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import supabase
from ..websocket.manager import manager

router = APIRouter()


class NoticeReadPayload(BaseModel):
    student_id: str | None = None
    user_id: str | None = None


class NoticeCreatePayload(BaseModel):
    title: str
    content: str
    priority: str = 'NORMAL'
    created_by: str | None = None
    is_pinned: bool = False
    expires_at: str | None = None
    attachment_url: str | None = None
    attachment_name: str | None = None
    target_branches: list[str] | None = None
    target_batches: list[int] | None = None
    target_programs: list[str] | None = None
    is_active: bool = True


class NoticeUpdatePayload(BaseModel):
    title: str | None = None
    content: str | None = None
    priority: str | None = None
    is_pinned: bool | None = None
    expires_at: str | None = None
    attachment_url: str | None = None
    attachment_name: str | None = None
    target_branches: list[str] | None = None
    target_batches: list[int] | None = None
    target_programs: list[str] | None = None
    is_active: bool | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _single_or_none(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


def _normalize_priority(value: str | None) -> str:
    priority = (value or 'NORMAL').strip().upper()
    if priority not in {'LOW', 'NORMAL', 'HIGH'}:
        return 'NORMAL'
    return priority


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
        student_res = (
            supabase.table('students')
            .select('id,branch,batch_year')
            .eq('id', student_id)
            .limit(1)
            .execute()
        )
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


@router.post('')
async def create_notice(payload: NoticeCreatePayload) -> dict[str, Any]:
    if not payload.title.strip() or not payload.content.strip():
        raise HTTPException(status_code=400, detail='Title and content are required')

    insert_row = {
        'title': payload.title.strip(),
        'content': payload.content.strip(),
        'priority': _normalize_priority(payload.priority),
        'created_by': payload.created_by,
        'is_pinned': bool(payload.is_pinned),
        'expires_at': payload.expires_at,
        'attachment_url': payload.attachment_url,
        'attachment_name': payload.attachment_name,
        'target_branches': payload.target_branches,
        'target_batches': payload.target_batches,
        'target_programs': payload.target_programs,
        'is_active': bool(payload.is_active),
    }

    result = supabase.table('notices').insert(insert_row).execute()
    notice = _single_or_none(result.data)
    if not notice:
        raise HTTPException(status_code=500, detail='Failed to create notice')

    await manager.broadcast_event({'type': 'notice_created', 'notice': notice})
    return notice


@router.patch('/{notice_id}')
async def update_notice(notice_id: str, payload: NoticeUpdatePayload) -> dict[str, Any]:
    patch_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if 'priority' in patch_data:
        patch_data['priority'] = _normalize_priority(str(patch_data['priority']))

    if not patch_data:
        raise HTTPException(status_code=400, detail='No fields to update')

    result = supabase.table('notices').update(patch_data).eq('id', notice_id).execute()
    notice = _single_or_none(result.data)
    if not notice:
        raise HTTPException(status_code=404, detail='Notice not found')

    await manager.broadcast_event({'type': 'notice_updated', 'notice': notice})
    return notice


@router.delete('/{notice_id}')
async def delete_notice(notice_id: str) -> dict[str, bool]:
    exists = supabase.table('notices').select('id').eq('id', notice_id).limit(1).execute()
    row = _single_or_none(exists.data)
    if not row:
        raise HTTPException(status_code=404, detail='Notice not found')

    supabase.table('notices').delete().eq('id', notice_id).execute()
    await manager.broadcast_event({'type': 'notice_deleted', 'notice_id': notice_id})
    return {'success': True}


@router.post('/{notice_id}/read')
def mark_notice_read(notice_id: str, payload: NoticeReadPayload) -> dict[str, bool]:
    user_id = payload.user_id or payload.student_id
    if not user_id:
        return {'success': False}

    supabase.table('notice_reads').upsert({'notice_id': notice_id, 'user_id': user_id}).execute()
    return {'success': True}
