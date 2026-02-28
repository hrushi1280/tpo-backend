from __future__ import annotations

import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect


async def receive_json(websocket: WebSocket) -> dict[str, Any]:
    data = await websocket.receive_text()
    try:
        parsed = json.loads(data)
        if isinstance(parsed, dict):
            return parsed
        return {'type': 'invalid', 'payload': parsed}
    except json.JSONDecodeError:
        return {'type': 'invalid'}


__all__ = ['receive_json', 'WebSocketDisconnect']
