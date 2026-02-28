from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..db import supabase

router = APIRouter()


def _load_user_names(user_ids: set[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    if not user_ids:
        return names

    for user_id in user_ids:
        student_res = supabase.table('students').select('full_name').eq('id', user_id).limit(1).execute()
        if student_res.data:
            names[user_id] = student_res.data[0].get('full_name') or 'Student'
            continue

        admin_res = supabase.table('admin_users').select('full_name').eq('id', user_id).limit(1).execute()
        if admin_res.data:
            names[user_id] = admin_res.data[0].get('full_name') or 'Admin'

    return names


@router.get('/{user_id}')
def get_messages(user_id: str, other_id: str | None = None) -> list[dict[str, Any]]:
    result = (
        supabase.table('messages')
        .select('*')
        .or_(f'sender_id.eq.{user_id},receiver_id.eq.{user_id}')
        .order('created_at')
        .execute()
    )
    rows = result.data or []

    if other_id:
        rows = [
            row
            for row in rows
            if (row.get('sender_id') == other_id or row.get('receiver_id') == other_id)
            and (row.get('sender_id') == user_id or row.get('receiver_id') == user_id)
        ]

    user_ids = {row.get('sender_id') for row in rows if row.get('sender_id')}
    names = _load_user_names(user_ids)

    formatted: list[dict[str, Any]] = []
    for row in rows:
        formatted.append(
            {
                'id': row.get('id'),
                'sender_id': row.get('sender_id'),
                'receiver_id': row.get('receiver_id'),
                'message': row.get('message', ''),
                'created_at': row.get('created_at'),
                'is_read': bool(row.get('is_read', False)),
                'sender_name': names.get(row.get('sender_id', ''), None),
            }
        )

    return formatted


@router.get('/unread-count/{user_id}')
def get_unread_count(user_id: str) -> dict[str, int]:
    result = (
        supabase.table('messages')
        .select('id', count='exact')
        .eq('receiver_id', user_id)
        .eq('is_read', False)
        .execute()
    )
    return {'count': result.count or 0}
