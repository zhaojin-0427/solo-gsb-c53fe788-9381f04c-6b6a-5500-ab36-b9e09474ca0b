from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agenda import agenda_for
from calendar_logic import UTC, CalendarError, canonical_digest, parse_ical, utc_text
from store import (
    apply_request,
    connect,
    init_db,
    is_newer,
    latest_version,
    record_cancel,
    record_message,
    record_reply,
)

DB_PATH = os.environ.get("DATABASE_PATH", "/data/meeting.db")
conn = connect(DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(conn)
    yield

app = FastAPI(title="Meeting Invitation Adjudication API", version="1.0.0",
              lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    detail = exc.errors()[0]
    return JSONResponse(status_code=422, content={
        "error": detail.get("msg", "invalid request parameters"),
        "location": ".".join(str(x) for x in detail.get("loc", [])),
    })


@app.exception_handler(CalendarError)
async def calendar_error_handler(request: Request, exc: CalendarError):
    return JSONResponse(status_code=422, content={"error": str(exc)})


def _same_version_conflict(method: str, uid: str, sequence: int,
                           dtstamp: str, digest: str) -> bool:
    if method == "REPLY":
        rows = conn.execute(
            """SELECT DISTINCT digest FROM reply_records
               WHERE uid=? AND sequence=? AND dtstamp=? AND digest<>?""",
            (uid, sequence, dtstamp, digest)).fetchall()
    elif method == "CANCEL":
        rows = conn.execute(
            """SELECT DISTINCT digest FROM cancel_records
               WHERE uid=? AND sequence=? AND dtstamp=? AND digest<>?""",
            (uid, sequence, dtstamp, digest)).fetchall()
    else:
        rows = conn.execute(
            """SELECT DISTINCT digest FROM request_versions
               WHERE uid=? AND sequence=? AND dtstamp=? AND digest<>?""",
            (uid, sequence, dtstamp, digest)).fetchall()
    return bool(rows)


@app.post("/messages")
async def post_message(request: Request):
    body = await request.body()
    if not body:
        raise CalendarError("request body must contain an iCalendar message")
    parsed = parse_ical(body)
    digest = canonical_digest(body)
    dtstamp_text = utc_text(parsed.dtstamp)
    key = (parsed.sequence, dtstamp_text)

    if _same_version_conflict(parsed.method, parsed.uid,
                              parsed.sequence, dtstamp_text, digest):
        return JSONResponse(status_code=409, content={
            "error": "same UID/SEQUENCE/DTSTAMP notification has different content",
            "uid": parsed.uid, "method": parsed.method,
            "sequence": parsed.sequence, "dtstamp": dtstamp_text})

    seen = conn.execute(
        "SELECT 1 FROM messages WHERE uid=? AND method=? AND digest=? LIMIT 1",
        (parsed.uid, parsed.method, digest)).fetchone()

    result = "stored"
    if parsed.method == "REQUEST":
        current = latest_version(conn, parsed.uid, "REQUEST")
        if seen:
            result = "idempotent"
        elif is_newer(key, current):
            apply_request(conn, parsed.components, parsed.sequence,
                          parsed.dtstamp, digest)
            result = "applied"
        else:
            result = "ignored_stale"
    elif parsed.method == "REPLY":
        if not seen:
            for email, (rid, partstat) in parsed.replies.items():
                record_reply(conn, parsed.uid, parsed.sequence, parsed.dtstamp,
                             rid, email, partstat, digest)
            result = "stored"
        else:
            result = "idempotent"
    else:
        rid, range_value = parsed.cancellation
        if not seen:
            record_cancel(conn, parsed.uid, parsed.sequence, parsed.dtstamp,
                          rid, range_value, digest)
            result = "stored"
        else:
            result = "idempotent"

    if not seen:
        record_message(conn, parsed.uid, parsed.method, parsed.sequence,
                       parsed.dtstamp, digest, result, body)
    return {
        "uid": parsed.uid,
        "method": parsed.method,
        "sequence": parsed.sequence,
        "dtstamp": dtstamp_text,
        "result": result,
    }


@app.get("/agenda")
async def get_agenda(
    attendee: str = Query(..., min_length=3),
    start: datetime = Query(..., description="Inclusive UTC ISO-8601 start"),
    end: datetime = Query(..., description="Exclusive UTC ISO-8601 end"),
):
    email = attendee.strip().lower()
    if not email.startswith("mailto:") and "@" not in email:
        raise CalendarError("attendee must be an email address")
    if email.startswith("mailto:"):
        email = email[7:].lower()
    if start.tzinfo is None or end.tzinfo is None:
        raise CalendarError("start and end must include a UTC offset (use Z)")
    start_utc = start.astimezone(UTC).replace(microsecond=0)
    end_utc = end.astimezone(UTC).replace(microsecond=0)
    if end_utc <= start_utc:
        raise CalendarError("end must be after start")
    if (end_utc - start_utc).total_seconds() != 7 * 24 * 3600:
        raise CalendarError("agenda window must be an exact UTC half-open seven-day week")
    items = agenda_for(conn, email, start_utc, end_utc)
    return {"attendee": email, "start": utc_text(start_utc),
            "end": utc_text(end_utc), "items": items}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
