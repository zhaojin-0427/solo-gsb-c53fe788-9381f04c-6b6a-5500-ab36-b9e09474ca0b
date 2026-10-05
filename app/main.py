"""FastAPI application: ingest iTIP messages and query attendee agendas."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import agenda as agenda_mod
from . import config
from .db import get_conn, init_db, transaction
from .errors import CalendarError, ConflictError
from .ics_parse import parse_message
from .ingest import apply_component, flush_staging


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Meeting Invite Adjudication Service",
    version="1.0.0",
    description=(
        "Accepts iTIP REQUEST/CANCEL/REPLY iCalendar messages and resolves "
        "an attendee's meeting status over half-open UTC week windows."
    ),
    lifespan=lifespan,
)


@app.exception_handler(CalendarError)
async def _calendar_error_handler(_request: Request, exc: CalendarError):
    return JSONResponse(status_code=422, content={
        "error": "unprocessable_calendar", "detail": exc.message})


@app.exception_handler(ConflictError)
async def _conflict_handler(_request: Request, exc: ConflictError):
    return JSONResponse(status_code=409, content={
        "error": "version_conflict",
        "detail": str(exc),
        "uid": exc.uid,
        "recurrence_id": exc.rid or None,
        "sequence": exc.seq,
    })


@app.post("/messages", status_code=200)
async def post_message(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        raise CalendarError("empty request body")
    message = parse_message(raw, config.MAX_COUNT)

    conn = get_conn()
    try:
        affected: set[str] = set()
        results = []
        with transaction(conn):
            # Apply master components first so override components in the same
            # message find their series.
            comps = sorted(
                message.components,
                key=lambda c: 0 if (c.kind != "REQUEST" or c.is_master) else 1,
            )
            for c in comps:
                outcome = apply_component(conn, c)
                results.append({"uid": c.uid, "kind": c.kind, "result": outcome})
                affected.add(c.uid)
            for uid in affected:
                flush_staging(conn, {uid}, config.MAX_COUNT)
        return {"method": message.method, "results": results}
    finally:
        conn.close()


@app.get("/agenda")
async def get_agenda(
    attendee: str = Query(..., description="Attendee email (case-insensitive)"),
    start: str | None = Query(None, description="UTC ISO-8601 window start"),
    end: str | None = Query(None, description="UTC ISO-8601 window end (exclusive)"),
    week: str | None = Query(
        None,
        description="ISO week like 2025-W10; defaults to the current UTC week",
    ),
):
    win_start, win_end = _resolve_window(start, end, week)
    conn = get_conn()
    try:
        items = agenda_mod.agenda(
            conn, attendee.strip().lower(), win_start, win_end)
    finally:
        conn.close()
    return {
        "attendee": attendee.strip().lower(),
        "start": _iso(win_start),
        "end": _iso(win_end),
        "events": items,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# window parsing
# ---------------------------------------------------------------------------

def _parse_utc(value: str, name: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalendarError(f"invalid {name} timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise CalendarError(
            f"{name} must carry an explicit UTC offset (e.g. ...Z or +00:00)")
    # Windows are interpreted in UTC; any explicit offset is normalised.
    return parsed.astimezone(ZoneInfo("UTC"))


def _resolve_window(start, end, week) -> tuple[dt.datetime, dt.datetime]:
    if week is not None and (start is not None or end is not None):
        raise CalendarError("use either week= or both start and end, not both")
    if (start is None) != (end is None):
        raise CalendarError("start and end must be provided together")

    if start is not None:
        win_start = _parse_utc(start, "start")
        win_end = _parse_utc(end, "end")
    else:
        if week is not None:
            monday = _parse_iso_week(week)
        else:
            today = dt.datetime.now(ZoneInfo("UTC")).date()
            monday = today - dt.timedelta(days=today.weekday())
        win_start = dt.datetime.combine(monday, dt.time(), ZoneInfo("UTC"))
        win_end = win_start + dt.timedelta(days=7)

    if win_end <= win_start:
        raise CalendarError("window end must be after start")
    if win_end - win_start > dt.timedelta(days=370):
        raise CalendarError("window is too wide (max 370 days)")
    return win_start, win_end


def _parse_iso_week(value: str) -> dt.date:
    try:
        year_s, week_s = value.split("-W")
        year, week = int(year_s), int(week_s)
        return dt.date.fromisocalendar(year, week, 1)
    except (ValueError, IndexError) as exc:
        raise CalendarError(
            f"invalid week {value!r}; expected format YYYY-Www") from exc


def _iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S+00:00")
