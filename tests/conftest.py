"""Test fixtures: isolated temp DB + FastAPI TestClient + ICS builders."""
import datetime as dt
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="meeting-test-")
    db_path = os.path.join(tmpdir, "test.db")
    monkeypatch.setenv("MEETING_DB_PATH", db_path)
    from app import config
    monkeypatch.setattr(config, "DB_PATH", db_path)
    # Reload the app module so its startup uses the patched DB path.
    import importlib
    from app import main as main_mod
    importlib.reload(main_mod)
    with TestClient(main_mod.app) as c:
        yield c
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# ICS construction helpers
# ---------------------------------------------------------------------------

def _lines(method, body_lines):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"METHOD:{method}",
             "PRODID:-//Tests//Meeting//EN"]
    lines.extend(body_lines)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def vevent(**kw):
    """Build a VEVENT block from keyword parts.

    Supported keys: uid, dtstamp, seq, start, end (plain strings),
    start_tz/end_tz (tzid:value), rrule, rid (tzid:value), rid_range,
    attendees (list of (email, partstat)), duration, allday_start.
    """
    out = ["BEGIN:VEVENT"]
    out.append(f"UID:{kw['uid']}")
    out.append(f"DTSTAMP:{kw.get('dtstamp', '20250101T000000Z')}")
    if "seq" in kw:
        out.append(f"SEQUENCE:{kw['seq']}")
    if "allday_start" in kw:
        out.append(f"DTSTART;VALUE=DATE:{kw['allday_start']}")
        out.append(f"DTEND;VALUE=DATE:{kw['allday_end']}")
    if "start" in kw:
        out.append(f"DTSTART:{kw['start']}")
        out.append(f"DTEND:{kw['end']}")
    if "start_tz" in kw:
        tz, val = kw["start_tz"]
        out.append(f"DTSTART;TZID={tz}:{val}")
        tz, val = kw["end_tz"]
        out.append(f"DTEND;TZID={tz}:{val}")
    if "duration" in kw:
        out.append(f"DURATION:{kw['duration']}")
    if "rrule" in kw:
        out.append(f"RRULE:{kw['rrule']}")
    for ex in kw.get("exdates", []):
        tz, val = ex
        if tz:
            out.append(f"EXDATE;TZID={tz}:{val}")
        else:
            out.append(f"EXDATE:{val}")
    if "rid" in kw:
        tz, val = kw["rid"]
        rng = ";RANGE=THISANDFUTURE" if kw.get("rid_range") else ""
        if tz:
            out.append(f"RECURRENCE-ID;TZID={tz}{rng}:{val}")
        else:
            out.append(f"RECURRENCE-ID{rng}:{val}")
    for email, partstat in kw.get("attendees", []):
        ps = f";PARTSTAT={partstat}" if partstat else ""
        out.append(f"ATTENDEE{ps}:mailto:{email}")
    out.append("END:VEVENT")
    return "\r\n".join(out)


def make_ics(method, *events):
    return _lines(method, [line for ev in events for line in ev.split("\r\n")
                           if line])


def post(client, method, *events):
    return client.post("/messages", content=make_ics(method, *events))


def get_agenda(client, email, start, end):
    r = client.get("/agenda", params={
        "attendee": email, "start": start, "end": end})
    return r


WEEK = ("2025-03-03T00:00:00+00:00", "2025-03-24T00:00:00+00:00")
