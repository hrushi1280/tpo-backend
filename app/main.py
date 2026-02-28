from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import CORS_ORIGINS
from .offcampus import router as offcampus_router
from .routes import notices, messages
from .websocket.manager import manager
from .websocket.connection import receive_json, WebSocketDisconnect
from .db import supabase

app = FastAPI(title='TPO Backend', version='1.0.0')
app.include_router(offcampus_router)
app.include_router(notices.router, prefix='/notices', tags=['notices'])
app.include_router(messages.router, prefix='/messages', tags=['messages'])

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


def _sha256_prefixed(value: str) -> str:
    return 'sha256:' + hashlib.sha256(value.encode('utf-8')).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _single_or_none(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


class LoginRequest(BaseModel):
    email: str | None = None
    prn: str | None = None
    password: str
    device: str | None = None


class RegisterStudentRequest(BaseModel):
    prn: str
    password: str
    full_name: str
    email: str
    phone: str
    branch: str
    batch_year: int
    graduation_year: int


class SessionRequest(BaseModel):
    student_id: str
    session_id: str


class UpdatePayload(BaseModel):
    data: dict[str, Any]


class CreatePayload(BaseModel):
    data: dict[str, Any]


class AutoUnblockWhere(BaseModel):
    lt: dict[str, str] | None = None
    eq: dict[str, Any] | None = None


class AutoUnblockPayload(BaseModel):
    data: dict[str, Any]
    where: AutoUnblockWhere | None = None


class ApplicationSubmitPayload(BaseModel):
    job_drive_id: str
    student_id: str
    custom_resume_url: str | None = None
    custom_resume_name: str | None = None
    answers: dict[str, str] | None = None


@app.get('/health')
def health() -> dict[str, str]:
    return {'status': 'ok'}


@app.post('/auth/login/admin')
def login_admin(payload: LoginRequest) -> dict[str, Any]:
    if not payload.email or not payload.password:
        raise HTTPException(status_code=400, detail='Missing email or password')

    response = supabase.table('admin_users').select('*').eq('email', payload.email.strip().lower()).limit(1).execute()
    admin = _single_or_none(response.data)
    if not admin:
        return {'success': False, 'message': 'Invalid email or password'}

    expected = _sha256_prefixed(payload.password)
    stored = admin.get('password_hash', '')
    if not stored.startswith('sha256:'):
        expected = payload.password

    if expected != stored:
        return {'success': False, 'message': 'Invalid email or password'}

    if not admin.get('is_active', False):
        return {'success': False, 'message': 'Your account is deactivated. Please contact administrator.'}

    admin.pop('password_hash', None)
    return {'success': True, 'user': admin, 'userType': 'admin'}


@app.post('/auth/login/student')
def login_student(payload: LoginRequest) -> dict[str, Any]:
    if not payload.prn or not payload.password:
        raise HTTPException(status_code=400, detail='Missing PRN or password')

    prn = payload.prn.strip().upper()
    response = supabase.table('students').select('*').eq('prn', prn).limit(1).execute()
    student = _single_or_none(response.data)
    if not student:
        return {'success': False, 'message': 'Invalid PRN or password'}

    expected = _sha256_prefixed(payload.password)
    stored = student.get('password_hash', '')
    if not stored.startswith('sha256:'):
        expected = payload.password

    if expected != stored:
        return {'success': False, 'message': 'Invalid PRN or password'}

    if not student.get('is_approved', False):
        return {'success': False, 'message': 'Your account is pending admin approval. Please wait for approval or contact TPO office.'}

    if student.get('is_blocked', False):
        reason = (student.get('block_reason') or '').replace('_', ' ')
        remark = student.get('block_remark')
        message = f'Your account has been blocked. Reason: {reason}'
        if remark:
            message += f' - {remark}'
        return {'success': False, 'message': message}

    new_session_id = str(uuid4())
    supabase.table('students').update({
        'active_session_id': new_session_id,
        'last_login': _now_iso(),
        'last_login_device': payload.device,
    }).eq('id', student['id']).execute()

    student.pop('password_hash', None)
    student['active_session_id'] = new_session_id
    return {'success': True, 'user': student, 'userType': 'student', 'session_id': new_session_id}


@app.post('/auth/register/student')
def register_student(payload: RegisterStudentRequest) -> dict[str, Any]:
    prn = payload.prn.strip().upper()
    exists = supabase.table('students').select('id').eq('prn', prn).limit(1).execute()
    if _single_or_none(exists.data):
        return {'success': False, 'message': 'This PRN is already registered'}

    insert_payload = {
        'prn': prn,
        'password_hash': _sha256_prefixed(payload.password),
        'full_name': payload.full_name.strip(),
        'email': payload.email.strip().lower(),
        'phone': payload.phone.strip(),
        'branch': payload.branch,
        'batch_year': payload.batch_year,
        'graduation_year': payload.graduation_year,
        'is_approved': False,
        'is_blocked': False,
        'placement_status': 'NOT_PLACED',
        'current_cgpa': 0,
        'backlogs': 0,
    }

    result = supabase.table('students').insert(insert_payload).execute()
    if not result.data:
        return {'success': False, 'message': 'Registration failed. Please check the details and try again.'}

    return {'success': True, 'message': 'Registration successful! Please wait for admin approval before logging in.', 'userType': 'student'}


@app.post('/auth/logout/student')
def logout_student(payload: SessionRequest) -> dict[str, bool]:
    supabase.table('students').update({'active_session_id': None}).eq('id', payload.student_id).execute()
    return {'success': True}


@app.get('/auth/session/student/{student_id}')
def validate_student_session(student_id: str, session_id: str) -> dict[str, Any]:
    result = supabase.table('students').select('active_session_id').eq('id', student_id).limit(1).execute()
    row = _single_or_none(result.data)
    if not row:
        return {'valid': False}
    return {'valid': row.get('active_session_id') == session_id}


@app.get('/admin/metrics')
def admin_metrics() -> dict[str, Any]:
    result = supabase.rpc('get_admin_dashboard_metrics', {}).execute()
    row = _single_or_none(result.data)
    if not row:
        return {'metrics': None}
    return {'metrics': row}


@app.get('/students')
def list_students() -> dict[str, Any]:
    data = supabase.table('students').select('*').order('created_at', desc=True).execute().data or []
    return {'data': data}


@app.get('/students/pending')
def pending_students() -> dict[str, Any]:
    data = (
        supabase.table('students')
        .select('*')
        .eq('is_approved', False)
        .eq('is_blocked', False)
        .order('created_at', desc=True)
        .execute()
        .data
        or []
    )
    return {'data': data}


@app.patch('/students/{student_id}')
def update_student(student_id: str, payload: UpdatePayload) -> dict[str, bool]:
    supabase.table('students').update(payload.data).eq('id', student_id).execute()
    return {'success': True}


@app.patch('/students/auto-unblock')
def auto_unblock_students(payload: AutoUnblockPayload) -> dict[str, Any]:
    query = supabase.table('students').update(payload.data)
    if payload.where and payload.where.eq:
        for key, value in payload.where.eq.items():
            query = query.eq(key, value)
    if payload.where and payload.where.lt:
        for key, value in payload.where.lt.items():
            query = query.lt(key, value)
    result = query.execute()
    return {'success': True, 'updated': len(result.data or [])}

@app.delete('/students/{student_id}')
def delete_student(student_id: str) -> dict[str, bool]:
    supabase.table('students').delete().eq('id', student_id).execute()
    return {'success': True}


@app.get('/companies')
def list_companies() -> dict[str, Any]:
    data = supabase.table('companies').select('*').order('created_at', desc=True).execute().data or []
    return {'data': data}


@app.post('/companies')
def create_company(payload: CreatePayload) -> dict[str, bool]:
    supabase.table('companies').insert(payload.data).execute()
    return {'success': True}


@app.patch('/companies/{company_id}')
def update_company(company_id: str, payload: UpdatePayload) -> dict[str, bool]:
    supabase.table('companies').update(payload.data).eq('id', company_id).execute()
    return {'success': True}


@app.delete('/companies/{company_id}')
def delete_company(company_id: str) -> dict[str, bool]:
    supabase.table('companies').delete().eq('id', company_id).execute()
    return {'success': True}


@app.get('/job-drives')
def list_job_drives() -> dict[str, Any]:
    data = (
        supabase.table('job_drives')
        .select('*, company:companies(*), applications(count)')
        .order('created_at', desc=True)
        .execute()
        .data
        or []
    )
    return {'data': data}


@app.post('/job-drives')
def create_job_drive(payload: CreatePayload) -> dict[str, bool]:
    supabase.table('job_drives').insert(payload.data).execute()
    return {'success': True}


@app.patch('/job-drives/{job_drive_id}')
def update_job_drive(job_drive_id: str, payload: UpdatePayload) -> dict[str, bool]:
    supabase.table('job_drives').update(payload.data).eq('id', job_drive_id).execute()
    return {'success': True}


@app.delete('/job-drives/{job_drive_id}')
def delete_job_drive(job_drive_id: str) -> dict[str, bool]:
    supabase.table('job_drives').delete().eq('id', job_drive_id).execute()
    return {'success': True}


@app.get('/job-drives/{job_drive_id}/applications')
def get_job_drive_applications(job_drive_id: str, page: int = 0, page_size: int = 200) -> dict[str, Any]:
    start = page * page_size
    end = start + page_size - 1
    data = (
        supabase.table('applications')
        .select(
            'id,status,applied_at,remarks,custom_resume_url,custom_resume_name,'
            'student:students(id,prn,full_name,email,phone,branch,batch_year,current_cgpa,backlogs,placement_status,resume_url)'
        )
        .eq('job_drive_id', job_drive_id)
        .order('applied_at', desc=True)
        .range(start, end)
        .execute()
        .data
        or []
    )
    return {'data': data}


@app.get('/student/{student_id}/job-drives')
def get_student_job_drives(student_id: str) -> dict[str, Any]:
    student_res = supabase.table('students').select('*').eq('id', student_id).limit(1).execute()
    student = _single_or_none(student_res.data)
    if not student:
        return {'data': []}

    now_iso = _now_iso()
    drives = (
        supabase.table('job_drives')
        .select('*,company:companies(*),questions:custom_questions(*)')
        .eq('is_active', True)
        .gte('application_end', now_iso)
        .order('created_at', desc=True)
        .execute()
        .data
        or []
    )

    eligible: list[dict[str, Any]] = []
    for job in drives:
        if (
            student['branch'] in (job.get('allowed_branches') or [])
            and student['batch_year'] in (job.get('allowed_batches') or [])
            and float(student.get('current_cgpa') or 0) >= float(job.get('min_cgpa') or 0)
            and int(student.get('backlogs') or 0) <= int(job.get('max_backlogs') or 0)
        ):
            eligible.append(job)

    return {'data': eligible}


@app.get('/student/{student_id}/applications')
def get_student_applications(student_id: str) -> dict[str, Any]:
    data = (
        supabase.table('applications')
        .select('*,job_drive:job_drives(*,company:companies(*))')
        .eq('student_id', student_id)
        .order('applied_at', desc=True)
        .execute()
        .data
        or []
    )
    return {'data': data}


@app.post('/student/applications')
def submit_application(payload: ApplicationSubmitPayload) -> dict[str, Any]:
    app_insert = supabase.table('applications').insert({
        'job_drive_id': payload.job_drive_id,
        'student_id': payload.student_id,
        'status': 'APPLIED',
        'custom_resume_url': payload.custom_resume_url,
        'custom_resume_name': payload.custom_resume_name,
    }).execute()

    application = _single_or_none(app_insert.data)
    if not application:
        raise HTTPException(status_code=400, detail='Failed to create application')

    if payload.answers:
        rows: list[dict[str, Any]] = []
        for question_id, answer in payload.answers.items():
            if answer and answer.startswith('http'):
                rows.append({'application_id': application['id'], 'question_id': question_id, 'answer_file_url': answer, 'answer_text': None})
            elif answer:
                rows.append({'application_id': application['id'], 'question_id': question_id, 'answer_text': answer, 'answer_file_url': None})
        if rows:
            supabase.table('student_answers').insert(rows).execute()

    return {'success': True, 'application_id': application['id']}


@app.post('/files/upload')
def upload_file(
    file: UploadFile = File(...),
    bucket: str = Form(...),
    folder: str = Form(...),
    prefix: str = Form('file')
) -> dict[str, Any]:
    ext = (file.filename or 'bin').split('.')[-1]
    filename = f"{prefix}_{uuid4().hex}.{ext}"
    path = f"{folder.strip('/')}/{filename}"

    content = file.file.read()
    supabase.storage.from_(bucket).upload(path, content)
    public_url = supabase.storage.from_(bucket).get_public_url(path)
    return {'success': True, 'publicUrl': public_url, 'path': path}



@app.websocket('/ws/{user_id}/{role}')
async def websocket_endpoint(websocket: WebSocket, user_id: str, role: str):
    await manager.connect(websocket, user_id, role)

    try:
        while True:
            message_data = await receive_json(websocket)
            msg_type = message_data.get('type')

            if msg_type == 'notice' and role == 'admin':
                notice_insert = supabase.table('notices').insert({
                    'title': message_data.get('title', ''),
                    'content': message_data.get('content', ''),
                    'priority': message_data.get('priority', 'NORMAL'),
                    'created_by': user_id,
                    'is_pinned': bool(message_data.get('is_pinned', False)),
                }).execute()

                notice_row = _single_or_none(notice_insert.data) or {}
                await manager.broadcast_notice({
                    'id': notice_row.get('id'),
                    'title': notice_row.get('title', message_data.get('title', '')),
                    'content': notice_row.get('content', message_data.get('content', '')),
                    'priority': notice_row.get('priority', message_data.get('priority', 'NORMAL')),
                    'is_pinned': notice_row.get('is_pinned', bool(message_data.get('is_pinned', False))),
                })

            elif msg_type == 'message':
                if role == 'student':
                    admin_res = supabase.table('admin_users').select('id').eq('is_active', True).limit(1).execute()
                    target_admin_id = (admin_res.data or [{}])[0].get('id')
                    if not target_admin_id:
                        await websocket.send_json({'type': 'error', 'message': 'No active admin available'})
                        continue

                    msg_insert = supabase.table('messages').insert({
                        'sender_id': user_id,
                        'receiver_id': target_admin_id,
                        'sender_role': 'student',
                        'receiver_role': 'admin',
                        'message': message_data.get('message', ''),
                    }).execute()
                    msg_row = _single_or_none(msg_insert.data) or {}
                    await manager.send_to_admin({
                        'type': 'message',
                        'id': msg_row.get('id'),
                        'sender_id': user_id,
                        'receiver_id': target_admin_id,
                        'message': msg_row.get('message', message_data.get('message', '')),
                        'created_at': msg_row.get('created_at', _now_iso()),
                        'is_read': False,
                    })

                elif role == 'admin':
                    student_id = message_data.get('receiver_id') or message_data.get('student_id')
                    if not student_id:
                        await websocket.send_json({'type': 'error', 'message': 'Missing receiver id'})
                        continue

                    msg_insert = supabase.table('messages').insert({
                        'sender_id': user_id,
                        'receiver_id': student_id,
                        'sender_role': 'admin',
                        'receiver_role': 'student',
                        'message': message_data.get('message', ''),
                    }).execute()
                    msg_row = _single_or_none(msg_insert.data) or {}

                    delivered = await manager.send_to_student(student_id, {
                        'type': 'message',
                        'id': msg_row.get('id'),
                        'sender_id': user_id,
                        'receiver_id': student_id,
                        'message': msg_row.get('message', message_data.get('message', '')),
                        'created_at': msg_row.get('created_at', _now_iso()),
                        'is_read': False,
                    })
                    if not delivered:
                        await websocket.send_json({'type': 'error', 'message': 'Student is offline'})

            elif msg_type == 'typing':
                if role == 'student':
                    await manager.send_to_admin({
                        'type': 'typing',
                        'user_id': user_id,
                        'is_typing': bool(message_data.get('is_typing', False)),
                    })
                elif role == 'admin':
                    student_id = message_data.get('student_id') or message_data.get('receiver_id')
                    if student_id:
                        await manager.send_to_student(student_id, {
                            'type': 'typing',
                            'user_id': user_id,
                            'is_typing': bool(message_data.get('is_typing', False)),
                        })

            elif msg_type == 'mark_read' and role == 'admin':
                student_id = message_data.get('student_id')
                if student_id:
                    supabase.table('messages').update({
                        'is_read': True,
                        'read_at': _now_iso(),
                    }).eq('sender_id', student_id).eq('receiver_id', user_id).eq('is_read', False).execute()

            elif msg_type == 'ping':
                await websocket.send_json({'type': 'pong', 'timestamp': message_data.get('timestamp')})

    except WebSocketDisconnect:
        manager.disconnect(user_id, role)
    except Exception as exc:
        try:
            await websocket.send_json({'type': 'error', 'message': str(exc)})
        except Exception:
            pass
        manager.disconnect(user_id, role)


@app.get('/api/ws-status')
async def websocket_status() -> dict[str, Any]:
    return await manager.get_online_status()
if __name__ == '__main__':
    import uvicorn
    from .config import BACKEND_HOST, BACKEND_PORT

    uvicorn.run('app.main:app', host=BACKEND_HOST, port=BACKEND_PORT, reload=True)
