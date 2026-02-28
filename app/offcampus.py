from __future__ import annotations

import math
import time
from threading import Lock
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .config import ADZUNA_APP_ID, ADZUNA_APP_KEY, ADZUNA_COUNTRY, ADZUNA_CACHE_TTL_SECONDS

router = APIRouter()

_cache_lock = Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_student_bookmarks: dict[str, set[str]] = {}
_student_applications: dict[str, set[str]] = {}


class JobRefPayload(BaseModel):
    job_id: str


def _cache_key(role: str, location: str, page: int, limit: int) -> str:
    return f"{role.strip().lower()}|{location.strip().lower()}|{page}|{limit}"


def _to_lpa(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 100000.0
    except (TypeError, ValueError):
        return None


def _normalize_job(raw: dict[str, Any]) -> dict[str, Any]:
    company = (raw.get('company') or {}).get('display_name') or 'Unknown Company'
    location = (raw.get('location') or {}).get('display_name') or 'Unknown'
    annual_min = _to_lpa(raw.get('salary_min'))
    annual_max = _to_lpa(raw.get('salary_max'))

    salary = None
    if annual_min is not None and annual_max is not None:
        salary = f"{annual_min:.1f} - {annual_max:.1f}"
    elif annual_min is not None:
        salary = f"{annual_min:.1f}+"
    elif annual_max is not None:
        salary = f"Up to {annual_max:.1f}"

    return {
        'id': str(raw.get('id') or raw.get('__id') or raw.get('redirect_url') or f"job-{time.time_ns()}"),
        'company': company,
        'title': raw.get('title') or 'Untitled',
        'location': location,
        'description': raw.get('description') or '',
        'salary': salary,
        'job_type': raw.get('contract_time') or raw.get('contract_type') or '',
        'experience_required': (raw.get('category') or {}).get('label') or '',
        'posted_date': raw.get('created') or '',
        'apply_url': raw.get('redirect_url') or '',
        'source_website': raw.get('source') or 'Adzuna',
        'logo_url': None,
    }


async def _fetch_adzuna_jobs(role: str, location: str, page: int, limit: int) -> dict[str, Any]:
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        raise HTTPException(status_code=503, detail='Adzuna credentials are missing on backend')

    cache_key = _cache_key(role, location, page, limit)
    now = time.time()

    with _cache_lock:
        existing = _cache.get(cache_key)
        if existing and (now - existing[0]) < ADZUNA_CACHE_TTL_SECONDS:
            return existing[1]

    url = f"https://api.adzuna.com/v1/api/jobs/{ADZUNA_COUNTRY}/search/{page}"
    params = {
        'app_id': ADZUNA_APP_ID,
        'app_key': ADZUNA_APP_KEY,
        'results_per_page': limit,
        'content-type': 'application/json',
    }
    if role.strip():
        params['what'] = role.strip()
    if location.strip():
        params['where'] = location.strip()

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f'Adzuna API error: {exc}') from exc

    results = payload.get('results') or []
    normalized = [_normalize_job(item) for item in results if isinstance(item, dict)]
    total = int(payload.get('count') or len(normalized))
    total_pages = max(1, math.ceil(total / max(1, limit)))
    data = {
        'data': normalized,
        'total': total,
        'page': page,
        'total_pages': total_pages,
    }

    with _cache_lock:
        _cache[cache_key] = (time.time(), data)

    return data


@router.get('/off-campus-jobs/recent')
async def get_recent_jobs(limit: int = 12) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 50))
    return await _fetch_adzuna_jobs(role='', location='', page=1, limit=safe_limit)


@router.get('/off-campus-jobs/search')
async def search_off_campus_jobs(
    role: str = '',
    location: str = '',
    page: int = 1,
    limit: int = 12,
    job_type: str = '',
    min_salary: str = '',
    max_salary: str = '',
    experience: str = '',
    bookmarks_only: bool = False,
    student_id: str = '',
) -> dict[str, Any]:
    safe_page = max(1, page)
    safe_limit = max(1, min(limit, 50))

    response = await _fetch_adzuna_jobs(role=role, location=location, page=safe_page, limit=safe_limit)
    jobs = response['data']

    if job_type:
        jobs = [job for job in jobs if job_type.lower() in (job.get('job_type') or '').lower()]

    min_salary_value = None
    max_salary_value = None
    try:
        if min_salary:
            min_salary_value = float(min_salary)
        if max_salary:
            max_salary_value = float(max_salary)
    except ValueError:
        pass

    if min_salary_value is not None or max_salary_value is not None:
        filtered: list[dict[str, Any]] = []
        for job in jobs:
            salary_text = job.get('salary') or ''
            first_number = None
            token = ''
            for ch in salary_text:
                if ch.isdigit() or ch == '.':
                    token += ch
                elif token:
                    break
            if token:
                try:
                    first_number = float(token)
                except ValueError:
                    first_number = None

            if first_number is None:
                continue
            if min_salary_value is not None and first_number < min_salary_value:
                continue
            if max_salary_value is not None and first_number > max_salary_value:
                continue
            filtered.append(job)
        jobs = filtered

    if experience:
        jobs = [job for job in jobs if experience.lower() in (job.get('experience_required') or '').lower()]

    if bookmarks_only and student_id:
        bookmarked = _student_bookmarks.get(student_id, set())
        jobs = [job for job in jobs if job['id'] in bookmarked]

    total = len(jobs)
    total_pages = max(1, math.ceil(total / max(1, safe_limit)))
    start = (safe_page - 1) * safe_limit
    end = start + safe_limit

    return {
        'data': jobs[start:end],
        'total': total,
        'page': safe_page,
        'total_pages': total_pages,
    }


@router.get('/student/{student_id}/job-bookmarks')
def get_job_bookmarks(student_id: str) -> dict[str, Any]:
    return {'data': sorted(list(_student_bookmarks.get(student_id, set())))}


@router.post('/student/{student_id}/job-bookmarks')
def add_job_bookmark(student_id: str, payload: JobRefPayload) -> dict[str, bool]:
    _student_bookmarks.setdefault(student_id, set()).add(payload.job_id)
    return {'success': True}


@router.delete('/student/{student_id}/job-bookmarks/{job_id}')
def remove_job_bookmark(student_id: str, job_id: str) -> dict[str, bool]:
    _student_bookmarks.setdefault(student_id, set()).discard(job_id)
    return {'success': True}


@router.get('/student/{student_id}/job-applications')
def get_job_applications(student_id: str) -> dict[str, Any]:
    return {'data': sorted(list(_student_applications.get(student_id, set())))}


@router.post('/student/{student_id}/job-applications')
def add_job_application(student_id: str, payload: JobRefPayload) -> dict[str, bool]:
    _student_applications.setdefault(student_id, set()).add(payload.job_id)
    return {'success': True}


@router.post('/analytics/job-application')
def track_job_application(_: dict[str, Any]) -> dict[str, bool]:
    return {'success': True}
