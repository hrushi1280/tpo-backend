from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self.admin_connections: dict[str, WebSocket] = {}
        self.student_connections: dict[str, WebSocket] = {}
        self.active_chats: dict[str, str] = {}

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def connect(self, websocket: WebSocket, user_id: str, role: str) -> None:
        await websocket.accept()
        if role == 'admin':
            self.admin_connections[user_id] = websocket
        else:
            self.student_connections[user_id] = websocket

        await websocket.send_json(
            {
                'type': 'connection',
                'status': 'connected',
                'user_id': user_id,
                'role': role,
                'timestamp': self._now_iso(),
            }
        )

    def disconnect(self, user_id: str, role: str) -> None:
        if role == 'admin':
            self.admin_connections.pop(user_id, None)
        else:
            self.student_connections.pop(user_id, None)
            self.active_chats.pop(user_id, None)

    async def broadcast_notice(self, notice_data: dict[str, Any]) -> None:
        payload = {
            'type': 'notice',
            'id': notice_data.get('id'),
            'title': notice_data.get('title', ''),
            'content': notice_data.get('content', ''),
            'priority': notice_data.get('priority', 'NORMAL'),
            'created_at': self._now_iso(),
            'is_pinned': bool(notice_data.get('is_pinned', False)),
        }

        stale_students: list[str] = []
        for student_id, ws in self.student_connections.items():
            try:
                await ws.send_json(payload)
            except Exception:
                stale_students.append(student_id)

        for student_id in stale_students:
            self.student_connections.pop(student_id, None)

    async def send_to_admin(self, message_data: dict[str, Any], target_admin_id: str | None = None) -> None:
        payload = {
            'type': message_data.get('type', 'message'),
            'id': message_data.get('id'),
            'sender_id': message_data.get('sender_id'),
            'sender_name': message_data.get('sender_name'),
            'receiver_id': message_data.get('receiver_id'),
            'message': message_data.get('message'),
            'created_at': message_data.get('created_at', self._now_iso()),
            'is_read': bool(message_data.get('is_read', False)),
        }

        if target_admin_id:
            ws = self.admin_connections.get(target_admin_id)
            if ws:
                try:
                    await ws.send_json(payload)
                except Exception:
                    self.admin_connections.pop(target_admin_id, None)
            return

        stale_admins: list[str] = []
        for admin_id, ws in self.admin_connections.items():
            try:
                await ws.send_json(payload)
            except Exception:
                stale_admins.append(admin_id)

        for admin_id in stale_admins:
            self.admin_connections.pop(admin_id, None)

    async def send_to_student(self, student_id: str, message_data: dict[str, Any]) -> bool:
        ws = self.student_connections.get(student_id)
        if not ws:
            return False

        payload = {
            'type': message_data.get('type', 'message'),
            'id': message_data.get('id'),
            'sender_id': message_data.get('sender_id'),
            'sender_name': message_data.get('sender_name', 'Admin'),
            'receiver_id': student_id,
            'message': message_data.get('message'),
            'created_at': message_data.get('created_at', self._now_iso()),
            'is_read': bool(message_data.get('is_read', False)),
        }

        try:
            await ws.send_json(payload)
            return True
        except Exception:
            self.student_connections.pop(student_id, None)
            return False

    async def get_online_status(self) -> dict[str, Any]:
        return {
            'admins_online': len(self.admin_connections),
            'students_online': len(self.student_connections),
            'active_chats': len(self.active_chats),
            'online_student_ids': list(self.student_connections.keys()),
            'online_admin_ids': list(self.admin_connections.keys()),
        }


manager = ConnectionManager()
