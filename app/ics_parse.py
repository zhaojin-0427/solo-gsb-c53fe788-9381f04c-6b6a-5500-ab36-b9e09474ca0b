"""Parse iTIP messages (REQUEST / CANCEL / REPLY) into normalized components.

Strictness rules (violations -> 422):

* METHOD must be REQUEST, CANCEL or REPLY and at least one VEVENT present;
* UID/DTSTAMP mandatory; SEQUENCE defaults to 0;
* absolute DATE-TIME values must be UTC ("...Z") or carry a *known IANA* TZID;
  floating times, unknown zones and custom VTIMEZONE definitions are rejected;
* only DAILY/WEEKLY RRULEs with INTERVAL/COUNT/UNTIL/WKST/BYDAY are supported.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from zoneinfo import ZoneInfo

from icalendar import Calendar

from .errors import CalendarError
from . import rrulex

ALLOWED_FREQ = {"DAILY", "WEEKLY"}
ALLOWED_RRULE_KEYS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "WKST", "BYDAY"}


class Component:
    def __init__(self, **kw):
        self.kind: str = kw["kind"]                 # REQUEST | CANCEL | REPLY
        self.uid: str = kw["uid"]
        self.seq: int = kw["seq"]
        self.dtstamp: str = kw["dtstamp"]           # UTC iso
        self.is_master: bool = kw.get("is_master", True)
        self.rid: str | None = kw.get("rid")        # UTC iso key or None
        self.range_future: bool = kw.get("range_future", False)
        self.start: str | None = kw.get("start")    # UTC iso
        self.end: str | None = kw.get("end")        # UTC iso
        self.duration_s: int | None = kw.get("duration_s")
        self.all_day: bool = kw.get("all_day", False)
        self.rrule_json: str | None = kw.get("rrule_json")
        self.rrule_tzid: str | None = kw.get("rrule_tzid")
        self.rrule_until: str | None = kw.get("rrule_until")
        self.rrule_count: int | None = kw.get("rrule_count")
        self.rrule_last: str | None = kw.get("rrule_last")
        self.rrule = kw.get("rrule")                # dateutil rrule (master only)
        self.exdates: set[str] = kw.get("exdates", set())
        self.attendees: list[tuple[str, str]] = kw.get("attendees", [])
        self.partstat: str | None = kw.get("partstat")
        self.content_hash: str = kw.get("content_hash", "")
        self.raw_blob: bytes = kw.get("raw_blob", b"")

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "rrule"}


class Message:
    def __init__(self, method: str, components: list[Component], raw: bytes):
        self.method = method
        self.components = components
        self.raw = raw


def parse_message(raw: bytes, max_count: int) -> Message:
    try:
        cal = Calendar.from_ical(raw)
    except Exception as exc:  # icalendar raises a variety of parse errors
        raise CalendarError(f"invalid calendar data: {exc}") from exc

    method = cal.get("METHOD")
    if method is None:
        raise CalendarError("calendar METHOD is required")
    method = str(method).upper()
    if method not in ("REQUEST", "CANCEL", "REPLY"):
        raise CalendarError(f"unsupported METHOD: {method}")

    vevents = cal.walk("VEVENT")
    if not vevents:
        raise CalendarError("message contains no VEVENT")

    components = [_parse_vevent(v, method, max_count) for v in vevents]
    return Message(method, components, raw)

# ---------------------------------------------------------------------------
# property helpers
# ---------------------------------------------------------------------------

def _as_utc(value, tzid: str | None) -> dt.datetime:
    """Normalise a date/datetime value to an aware UTC datetime."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            if tzid:
                raise CalendarError(f"unknown or unsupported TZID: {tzid!r}")
            raise CalendarError("floating DATE-TIME is not supported; use UTC or IANA TZID")
        if tzid and not isinstance(value.tzinfo, ZoneInfo):
            raise CalendarError(f"TZID {tzid!r} is not a known IANA time zone")
        return value.astimezone(ZoneInfo("UTC"))
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=ZoneInfo("UTC"))
    raise CalendarError(f"unsupported date value: {value!r}")


def _time_prop(ve, name: str):
    """Return (utc_iso, is_date, tzid_key) for a property or None."""
    prop = ve.get(name)
    if prop is None:
        return None
    value = prop.dt
    tzid = prop.params.get("TZID")
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return rrulex.to_utc_iso(value), True, "UTC"
    utc = _as_utc(value, tzid)
    key = str(value.tzinfo) if tzid and isinstance(value.tzinfo, ZoneInfo) else "UTC"
    return rrulex.to_utc_iso(utc), False, key


def _rid_prop(ve, kind: str) -> tuple[str | None, bool]:
    prop = ve.get("RECURRENCE-ID")
    if prop is None:
        return None, False
    value = prop.dt
    tzid = prop.params.get("TZID")
    _as_utc(value, tzid)  # validates zone / floating time
    rng = prop.params.get("RANGE")
    if rng is not None:
        rng = str(rng).upper()
        if rng != "THISANDFUTURE":
            raise CalendarError(f"unsupported RANGE value: {rng}")
        if kind == "REPLY":
            raise CalendarError("RANGE on a REPLY is not supported")
    utc = _as_utc(value, tzid)
    return rrulex.to_utc_iso(utc), (rng == "THISANDFUTURE")


def _one(value, name: str):
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise CalendarError(f"multiple {name} values are not supported")
        return value[0]
    return value


def _dtstamp(ve) -> str:
    prop = ve.get("DTSTAMP")
    if prop is None:
        raise CalendarError("DTSTAMP is required")
    value = prop.dt
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise CalendarError("DTSTAMP must be an absolute UTC DATE-TIME")
    return rrulex.to_utc_iso(value.astimezone(ZoneInfo("UTC")))


def _attendees(ve, kind: str) -> list[tuple[str, str]]:
    raw = ve.get("ATTENDEE")
    if raw is None:
        return []
    props = raw if isinstance(raw, list) else [raw]
    out = []
    for att in props:
        addr = str(att).strip()
        if addr.lower().startswith("mailto:"):
            addr = addr[7:]
        if not addr:
            continue
        partstat = str(att.params.get("PARTSTAT",
                        "NEEDS-ACTION" if kind == "REQUEST" else "")).upper()
        out.append((addr.lower(), partstat))
    return out


# ---------------------------------------------------------------------------
# VEVENT parsing
# ---------------------------------------------------------------------------

def _parse_vevent(ve, kind: str, max_count: int) -> Component:
    uid = ve.get("UID")
    if uid is None or str(uid).strip() == "":
        raise CalendarError("VEVENT UID is required")
    uid = str(uid).strip()
    dtstamp = _dtstamp(ve)
    seq = int(ve.get("SEQUENCE", 0))

    rid, range_future = _rid_prop(ve, kind)
    is_master = rid is None

    if ve.get("RRULE") is not None and not is_master:
        raise CalendarError("RRULE on an override VEVENT is not supported")
    if ve.get("EXDATE") is not None and not is_master:
        raise CalendarError("EXDATE on an override VEVENT is not supported")

    start = end = None
    duration_s = None
    all_day = False
    rrule_json = rrule_tzid = rrule_until = rrule_last = None
    rrule_count = None
    rr = None
    exdates: set[str] = set()

    if kind == "REQUEST":
        t = _time_prop(ve, "DTSTART")
        if t is None:
            raise CalendarError("REQUEST VEVENT requires DTSTART")
        start, all_day, start_tzid = t
        duration_s, end = _duration(ve, start, all_day)

        if is_master:
            rr, rrule_json, rrule_tzid, rrule_until, rrule_count, rrule_last = (
                _parse_rrule(ve, start, all_day, start_tzid, max_count)
            )
            exdates = _parse_exdates(ve)
    else:
        # CANCEL / REPLY do not reschedule time; RANGE is still validated.
        pass

    attendees = _attendees(ve, kind)
    if kind == "REPLY":
        if not attendees:
            raise CalendarError("REPLY VEVENT requires an ATTENDEE")
        email, partstat = attendees[0]
        if not partstat:
            raise CalendarError("REPLY ATTENDEE requires PARTSTAT")
    else:
        email, partstat = None, None

    comp = Component(
        kind=kind, uid=uid, seq=seq, dtstamp=dtstamp,
        is_master=is_master, rid=rid, range_future=range_future,
        start=start, end=end, duration_s=duration_s, all_day=all_day,
        rrule_json=rrule_json, rrule_tzid=rrule_tzid,
        rrule_until=rrule_until, rrule_count=rrule_count, rrule_last=rrule_last,
        rrule=rr, exdates=exdates, attendees=attendees, partstat=partstat,
        raw_blob=ve.to_ical(),
    )
    comp.content_hash = _canonical_hash(comp)
    return comp


def _duration(ve, start_iso: str, all_day: bool) -> tuple[int, str]:
    start_dt = rrulex.parse_utc(start_iso)
    end_prop = ve.get("DTEND")
    dur_prop = ve.get("DURATION")
    if end_prop is not None:
        end_iso, end_date, _ = _time_prop(ve, "DTEND")
        end_dt = rrulex.parse_utc(end_iso)
        if all_day != end_date:
            raise CalendarError("DTSTART/DTEND date types must match")
    elif dur_prop is not None:
        delta: dt.timedelta = dur_prop.dt
        if not isinstance(delta, dt.timedelta):
            raise CalendarError("invalid DURATION")
        end_dt = start_dt + delta
        end_iso = rrulex.to_utc_iso(end_dt)
    else:
        raise CalendarError("REQUEST VEVENT requires DTEND or DURATION")
    if end_dt < start_dt:
        raise CalendarError("DTEND must be on or after DTSTART")
    return int((end_dt - start_dt).total_seconds()), end_iso


def _parse_rrule(ve, start_iso, all_day, start_tzid, max_count):
    recur = ve.get("RRULE")
    if recur is None:
        return None, None, None, None, None, None
    if isinstance(recur, list):
        raise CalendarError("multiple RRULEs are not supported")

    keys = set(recur.keys())
    unknown = keys - ALLOWED_RRULE_KEYS
    if unknown:
        raise CalendarError(f"unsupported RRULE part(s): {sorted(unknown)}")

    freq = str(_one(recur.get("FREQ"), "FREQ")).upper()
    if freq not in ALLOWED_FREQ:
        raise CalendarError(f"unsupported RRULE FREQ: {freq} (only DAILY/WEEKLY)")

    interval_v = recur.get("INTERVAL")
    interval = int(_one(interval_v, "INTERVAL")) if interval_v else 1
    if interval < 1:
        raise CalendarError("RRULE INTERVAL must be >= 1")

    count_v, until_v = recur.get("COUNT"), recur.get("UNTIL")
    if count_v is not None and until_v is not None:
        raise CalendarError("RRULE COUNT and UNTIL are mutually exclusive")
    count = int(_one(count_v, "COUNT")) if count_v else None
    if count is not None and count < 1:
        raise CalendarError("RRULE COUNT must be >= 1")
    if count is not None and count > max_count:
        raise CalendarError(f"RRULE COUNT {count} exceeds limit {max_count}")

    until = None
    until_iso = None
    if until_v is not None:
        value = _one(until_v, "UNTIL")
        if isinstance(value, dt.datetime):
            if value.tzinfo is None:
                # RFC 5545: naive UNTIL pairs with floating/UTC starts; treat as UTC.
                value = value.replace(tzinfo=ZoneInfo("UTC"))
            until = value.astimezone(ZoneInfo("UTC"))
        elif isinstance(value, dt.date):
            until = dt.datetime(value.year, value.month, value.day, tzinfo=ZoneInfo("UTC"))
        else:
            raise CalendarError("invalid RRULE UNTIL")
        until_iso = rrulex.to_utc_iso(until)

    byday_v = recur.get("BYDAY")
    if byday_v:
        byday = [str(x).upper() for x in byday_v]
        bad = [b for b in byday if b not in ("MO", "TU", "WE", "TH", "FR", "SA", "SU")]
        if bad:
            raise CalendarError(f"unsupported BYDAY: {bad}")
    else:
        byday = None
    wkst_v = recur.get("WKST")
    wkst = str(_one(wkst_v, "WKST")).upper() if wkst_v else "MO"
    if wkst not in ("MO", "TU", "WE", "TH", "FR", "SA", "SU"):
        raise CalendarError(f"unsupported WKST: {wkst}")

    if all_day:
        tz = ZoneInfo("UTC")
        start_local = rrulex.parse_utc(start_iso).astimezone(tz)
    else:
        tz = ZoneInfo(start_tzid)
        start_local = rrulex.parse_utc(start_iso).astimezone(tz)

    rr = rrulex.build_rrule(
        start_local, freq=freq, interval=interval, wkst=wkst,
        byday=byday, until=until, count=count,
    )
    last = None
    if count is not None and count <= 5000:
        last = rrulex.to_utc_iso(rrulex.last_occurrence(rr, count))
    return rr, rrulex.rrule_to_json(rr), str(tz), until_iso, count, last


def _parse_exdates(ve) -> set[str]:
    raw = ve.get("EXDATE")
    if raw is None:
        return set()
    props = raw if isinstance(raw, list) else [raw]
    out: set[str] = set()
    for prop in props:
        tzid = prop.params.get("TZID")
        for item in prop.dts:
            out.add(rrulex.to_utc_iso(_as_utc(item.dt, tzid)))
    return out


# ---------------------------------------------------------------------------
# canonical identity (DTSTAMP intentionally excluded)
# ---------------------------------------------------------------------------

def _canonical_hash(c: Component) -> str:
    if c.kind == "REQUEST":
        if c.is_master:
            obj = {
                "k": "REQUEST", "uid": c.uid, "seq": c.seq, "master": True,
                "start": c.start, "end": c.end, "allday": c.all_day,
                "rrule": c.rrule_json,
                "exdates": sorted(c.exdates),
                "attendees": sorted(c.attendees),
            }
        else:
            obj = {
                "k": "REQUEST", "uid": c.uid, "seq": c.seq, "master": False,
                "rid": c.rid, "range": c.range_future,
                "start": c.start, "end": c.end, "allday": c.all_day,
            }
    elif c.kind == "CANCEL":
        obj = {"k": "CANCEL", "uid": c.uid, "seq": c.seq,
               "rid": c.rid, "range": c.range_future}
    else:
        email, _ = c.attendees[0]
        obj = {"k": "REPLY", "uid": c.uid, "seq": c.seq, "rid": c.rid,
               "email": email, "partstat": c.partstat}
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()
