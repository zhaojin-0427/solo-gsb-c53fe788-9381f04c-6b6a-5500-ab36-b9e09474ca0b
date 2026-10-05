from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dateutil.rrule import rrule, weekdays
from icalendar import Calendar

UTC = ZoneInfo("UTC")
EMAIL_RE = re.compile(r"MAILTO:([^@\s<>]+@[^@\s<>]+)", re.IGNORECASE)
WEEKDAY_INDEX = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
DATEUTIL_WEEKDAYS = [weekdays.MO, weekdays.TU, weekdays.WE, weekdays.TH,
                     weekdays.FR, weekdays.SA, weekdays.SU]
PARTSTATS = {"ACCEPTED": "accepted", "DECLINED": "declined",
             "TENTATIVE": "tentative", "DELEGATED": "delegated",
             "COMPLETED": "completed", "IN-PROCESS": "in-process",
             "NEEDS-ACTION": "needs-action"}


class CalendarError(ValueError):
    """A malformed or unsupported calendar object."""


@dataclass(frozen=True)
class Attendee:
    email: str
    partstat: str = "NEEDS-ACTION"


@dataclass
class RuleSpec:
    freq: str
    interval: int = 1
    count: int | None = None
    until_utc: datetime | None = None
    byday: list[tuple[int, int]] = field(default_factory=list)
    wkst: int = 0
    raw_until_utc: datetime | None = None


@dataclass
class ComponentSpec:
    uid: str
    sequence: int
    dtstamp: datetime
    wall_start: datetime
    actual_start_utc: datetime
    actual_end_utc: datetime
    duration: timedelta
    tzid: str
    recurrence_id_utc: datetime | None = None
    range_value: str | None = None
    rule: RuleSpec | None = None
    exdates_utc: list[datetime] = field(default_factory=list)
    attendees: list[Attendee] = field(default_factory=list)
    summary: str | None = None
    status: str = "CONFIRMED"


@dataclass
class ParsedCalendar:
    method: str
    uid: str
    sequence: int
    dtstamp: datetime
    components: list[ComponentSpec]
    replies: dict[str, tuple[datetime | None, str]] = field(default_factory=dict)
    cancellation: tuple[datetime | None, str | None] | None = None


def canonical_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_email(value: object) -> str:
    text = str(value).strip()
    match = EMAIL_RE.fullmatch(text)
    if not match:
        raise CalendarError("attendee URI must be a mailto: address")
    email = match.group(1).lower()
    if "\n" in email or "," in email:
        raise CalendarError("invalid attendee address")
    return email


def _single_prop(event, name: str):
    values = event.get(name, [])
    if not isinstance(values, list):
        values = [values]
    if len(values) > 1:
        raise CalendarError(f"multiple {name} properties are not allowed")
    return values[0] if values else None


def _get_tz(prop, required_name: str) -> ZoneInfo:
    tzid = prop.params.get("TZID") if prop is not None else None
    if tzid is None:
        raise CalendarError(f"{required_name} must be UTC or carry an IANA TZID")
    key = str(tzid)
    try:
        return ZoneInfo(key)
    except Exception as exc:
        raise CalendarError(f"unknown IANA timezone: {key}") from exc


def _prop_datetime(prop, name: str) -> tuple[datetime, datetime, str]:
    if prop is None:
        raise CalendarError(f"missing {name}")
    if isinstance(prop.dt, date) and not isinstance(prop.dt, datetime):
        raise CalendarError(f"all-day {name} is not supported")
    dt = prop.dt
    if dt.tzinfo is None:
        if str(prop).endswith("Z"):
            dt = dt.replace(tzinfo=UTC)
        else:
            raise CalendarError(f"floating {name} is not supported")
    tzid_param = prop.params.get("TZID")
    if tzid_param is not None:
        tzid = str(tzid_param)
        try:
            zone = ZoneInfo(tzid)
        except Exception as exc:
            raise CalendarError(f"unknown IANA timezone: {tzid}") from exc
        actual = dt.astimezone(UTC)
        wall = dt.astimezone(zone).replace(tzinfo=None)
    else:
        actual = dt.astimezone(UTC)
        tzid = "UTC"
        wall = actual.replace(tzinfo=None)
    return wall, actual, tzid


def _reference_datetime(prop, name: str) -> datetime:
    if prop is None:
        return None
    if isinstance(prop.dt, date) and not isinstance(prop.dt, datetime):
        raise CalendarError(f"all-day {name} is not supported")
    dt = prop.dt
    if dt.tzinfo is None:
        raise CalendarError(f"floating {name} is not supported")
    tzid_param = prop.params.get("TZID")
    if tzid_param is not None:
        try:
            ZoneInfo(str(tzid_param))
        except Exception as exc:
            raise CalendarError(f"unknown IANA timezone: {tzid_param}") from exc
    return dt.astimezone(UTC)


def _parse_byday(values: list[str]) -> list[tuple[int, int]]:
    result = []
    for item in values:
        match = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", item.upper())
        if not match:
            raise CalendarError(f"invalid BYDAY value: {item}")
        ordinal = int(match.group(1)) if match.group(1) else 0
        result.append((ordinal, WEEKDAY_INDEX[match.group(2)]))
    return result


def parse_rule(prop, wall_start: datetime, tzid: str) -> RuleSpec | None:
    if prop is None:
        return None
    parts: dict[str, list[str]] = {}
    for item in prop.to_ical().decode("utf-8").split(";"):
        if "=" not in item:
            raise CalendarError(f"invalid RRULE part: {item}")
        key, value = item.split("=", 1)
        parts[key.upper()] = [v.strip().upper() for v in value.split(",") if v.strip()]
    freq_tokens = parts.get("FREQ", [])
    if len(freq_tokens) != 1 or freq_tokens[0] not in {"DAILY", "WEEKLY"}:
        raise CalendarError("only DAILY and WEEKLY RRULE frequencies are supported")
    if set(parts) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}:
        raise CalendarError("unsupported RRULE part")
    freq = freq_tokens[0]
    interval = 1
    if "INTERVAL" in parts:
        if len(parts["INTERVAL"]) != 1 or not parts["INTERVAL"][0].isdigit():
            raise CalendarError("invalid RRULE INTERVAL")
        interval = int(parts["INTERVAL"][0])
        if interval < 1:
            raise CalendarError("RRULE INTERVAL must be positive")
    count = None
    if "COUNT" in parts:
        if len(parts["COUNT"]) != 1 or not parts["COUNT"][0].isdigit():
            raise CalendarError("invalid RRULE COUNT")
        count = int(parts["COUNT"][0])
        if count < 1:
            raise CalendarError("RRULE COUNT must be positive")
        if count > 100_000:
            raise CalendarError("RRULE COUNT is limited to 100000")
    until_utc = None
    if "UNTIL" in parts:
        if count is not None:
            raise CalendarError("COUNT and UNTIL cannot be used together")
        if len(parts["UNTIL"]) != 1:
            raise CalendarError("invalid RRULE UNTIL")
        token = parts["UNTIL"][0]
        try:
            until = datetime.strptime(token, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError as exc:
            raise CalendarError("RRULE UNTIL must be UTC (YYYYMMDDTHHMMSSZ)") from exc
        until_utc = until
    byday = _parse_byday(parts["BYDAY"]) if "BYDAY" in parts else []
    if any(ordinal for ordinal, _ in byday):
        raise CalendarError("ordinal BYDAY is not supported")
    wkst = 0
    if "WKST" in parts:
        if len(parts["WKST"]) != 1 or parts["WKST"][0] not in WEEKDAY_INDEX:
            raise CalendarError("invalid RRULE WKST")
        wkst = WEEKDAY_INDEX[parts["WKST"][0]]
    # Validate it constructs with dateutil; generation happens only over windows.
    build_dateutil_rule(RuleSpec(freq, interval, count, until_utc, byday, wkst),
                        wall_start)
    return RuleSpec(freq=freq, interval=interval, count=count,
                    until_utc=until_utc, byday=byday, wkst=wkst)


def _localize_wall(wall: datetime, tzid: str, allow_gap: bool = False) -> datetime:
    tz = ZoneInfo(tzid)
    local = wall.replace(tzinfo=tz)
    roundtrip = local.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    offset = local.utcoffset()
    if roundtrip != wall or offset is None:
        if allow_gap:
            return None
        raise CalendarError("event start falls in a non-existent local time")
    # PEP 495: fold=0 is the earlier of two repeated wall times.
    return wall.replace(tzinfo=tz, fold=0).astimezone(UTC)


def local_wall_for_rule(wall: datetime, tzid: str) -> datetime | None:
    return _localize_wall(wall, tzid, allow_gap=True)


def build_dateutil_rule(spec: RuleSpec, start: datetime,
                        count_override: int | None = None) -> rrule:
    byweekday = [DATEUTIL_WEEKDAYS[d] for ordinal, d in spec.byday if ordinal == 0]
    if spec.freq == "DAILY":
        return rrule(freq=rrule.DAILY, interval=spec.interval, dtstart=start,
                     count=count_override if count_override is not None else spec.count,
                     until=utc_to_wall_until(spec.until_utc, start.tzinfo)
                     if spec.until_utc else None)
    return rrule(freq=rrule.WEEKLY, interval=spec.interval, dtstart=start,
                 wkst=DATEUTIL_WEEKDAYS[spec.wkst],
                 byweekday=byweekday or None,
                 count=count_override if count_override is not None else spec.count,
                 until=utc_to_wall_until(spec.until_utc, start.tzinfo)
                 if spec.until_utc else None)


def utc_to_wall_until(value: datetime, tzid: str) -> datetime:
    tz = ZoneInfo(tzid) if isinstance(tzid, str) else tzid
    return value.astimezone(tz).replace(tzinfo=None)


def _event_exdates(event) -> list[datetime]:
    raw = event.get("EXDATE")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    result = []
    for prop in raw:
        if hasattr(prop, "dts"):
            values = prop.dts
        elif hasattr(prop, "dt") and isinstance(prop.dt, list):
            values = prop.dt
        else:
            values = [prop]
        for item in values:
            dt = item.dt
            if isinstance(dt, date) and not isinstance(dt, datetime):
                raise CalendarError("all-day EXDATE is not supported")
            if dt.tzinfo is None:
                raise CalendarError("floating EXDATE is not supported")
            result.append(dt.astimezone(UTC).replace(microsecond=0))
    return sorted(set(result))


def _parse_attendees(event, required: bool) -> list[Attendee]:
    props = event.get("ATTENDEE", [])
    if not isinstance(props, list):
        props = [props]
    attendees = []
    seen = set()
    for prop in props:
        email = parse_email(prop)
        if email in seen:
            raise CalendarError(f"duplicate attendee {email}")
        seen.add(email)
        partstat = str(prop.params.get("PARTSTAT", "NEEDS-ACTION")).upper()
        if partstat not in PARTSTATS:
            raise CalendarError(f"unsupported PARTSTAT: {partstat}")
        attendees.append(Attendee(email=email, partstat=partstat))
    if required and not attendees:
        raise CalendarError("REQUEST VEVENT must contain at least one ATTENDEE")
    return attendees


def _parse_request_component(event) -> ComponentSpec:
    uid = str(_single_prop(event, "UID") or "")
    if not uid:
        raise CalendarError("missing UID")
    dtstamp = _prop_datetime(_single_prop(event, "DTSTAMP"), "DTSTAMP")[1]
    seq_prop = _single_prop(event, "SEQUENCE")
    sequence = int(seq_prop) if seq_prop is not None else 0
    start_prop = _single_prop(event, "DTSTART")
    wall_start, actual_start, tzid = _prop_datetime(start_prop, "DTSTART")
    end_prop = _single_prop(event, "DTEND")
    duration_prop = _single_prop(event, "DURATION")
    if end_prop is not None and duration_prop is not None:
        raise CalendarError("DTEND and DURATION cannot both be present")
    if end_prop is not None:
        _, actual_end, end_tzid = _prop_datetime(end_prop, "DTEND")
        if end_tzid != tzid:
            raise CalendarError("DTSTART and DTEND timezones must match")
    elif duration_prop is not None:
        actual_end = actual_start + duration_prop.dt
    else:
        raise CalendarError("timed VEVENT requires DTEND or DURATION")
    if actual_end <= actual_start:
        raise CalendarError("event end must be after its start")
    rid_prop = _single_prop(event, "RECURRENCE-ID")
    rid = _reference_datetime(rid_prop, "RECURRENCE-ID") if rid_prop else None
    range_value = None
    if rid_prop is not None:
        raw_range = rid_prop.params.get("RANGE")
        if raw_range is not None:
            range_value = str(raw_range).upper()
            if range_value != "THISANDFUTURE":
                raise CalendarError("only RANGE=THISANDFUTURE is supported")
    rule = parse_rule(_single_prop(event, "RRULE"), wall_start, tzid)
    if rid is None and range_value is not None:
        raise CalendarError("RANGE requires RECURRENCE-ID")
    if rid is not None and local_wall_for_rule(wall_start, tzid) is None:
        raise CalendarError("exception DTSTART falls in a non-existent local time")
    if rid is None and rule is None and local_wall_for_rule(wall_start, tzid) is None:
        raise CalendarError("event start falls in a non-existent local time")
    status_prop = _single_prop(event, "STATUS")
    status = str(status_prop).upper() if status_prop is not None else "CONFIRMED"
    if status not in {"CONFIRMED", "TENTATIVE", "CANCELLED"}:
        raise CalendarError(f"unsupported VEVENT STATUS: {status}")
    return ComponentSpec(
        uid=uid, sequence=sequence, dtstamp=dtstamp, wall_start=wall_start,
        actual_start_utc=actual_start.replace(microsecond=0),
        actual_end_utc=actual_end.replace(microsecond=0),
        duration=actual_end - actual_start, tzid=tzid,
        recurrence_id_utc=rid.replace(microsecond=0) if rid else None,
        range_value=range_value, rule=rule,
        exdates_utc=_event_exdates(event),
        attendees=_parse_attendees(event, rid is None),
        summary=str(_single_prop(event, "SUMMARY")) if _single_prop(event, "SUMMARY") else None,
        status=status,
    )


def parse_ical(data: bytes) -> ParsedCalendar:
    try:
        calendar = Calendar.from_ical(data)
    except Exception as exc:
        raise CalendarError(f"invalid iCalendar object: {exc}") from exc
    method = calendar.get("METHOD")
    if method is None:
        raise CalendarError("missing calendar METHOD")
    method = str(method).upper()
    if method not in {"REQUEST", "CANCEL", "REPLY"}:
        raise CalendarError(f"unsupported METHOD: {method}")
    events = [c for c in calendar.walk("VEVENT")]
    if not events:
        raise CalendarError("calendar contains no VEVENT")
    first = events[0]
    uid = str(_single_prop(first, "UID") or "")
    if not uid:
        raise CalendarError("missing UID")
    dtstamp = _prop_datetime(_single_prop(first, "DTSTAMP"), "DTSTAMP")[1]
    seq_prop = _single_prop(first, "SEQUENCE")
    sequence = int(seq_prop) if seq_prop is not None else 0
    for event in events:
        if str(_single_prop(event, "UID") or "") != uid:
            raise CalendarError("all VEVENTs must share the same UID")
        event_dtstamp = _prop_datetime(_single_prop(event, "DTSTAMP"), "DTSTAMP")[1]
        if event_dtstamp != dtstamp:
            raise CalendarError("all VEVENTs must share the same DTSTAMP")
        event_sequence = int(_single_prop(event, "SEQUENCE")) if _single_prop(event, "SEQUENCE") else 0
        if event_sequence != sequence:
            raise CalendarError("all VEVENTs must share the same SEQUENCE")
    if method == "REQUEST":
        components = [_parse_request_component(e) for e in events]
        masters = [c for c in components if c.recurrence_id_utc is None]
        if len(masters) != 1:
            raise CalendarError("REQUEST must contain exactly one master VEVENT")
        rids = [c.recurrence_id_utc for c in components if c.recurrence_id_utc]
        if len(rids) != len(set(rids)):
            raise CalendarError("duplicate RECURRENCE-ID in REQUEST")
        ranges = [c for c in components if c.range_value]
        if len(ranges) > 1:
            raise CalendarError("multiple THISANDFUTURE components are not allowed")
        return ParsedCalendar(method, uid, sequence, dtstamp, components)
    for event in events:
        event_seq = int(_single_prop(event, "SEQUENCE")) if _single_prop(event, "SEQUENCE") else 0
        if event_seq != sequence:
            raise CalendarError("all VEVENTs must share the same SEQUENCE")
    if method == "REPLY":
        if len(events) != 1:
            raise CalendarError("REPLY must contain one VEVENT")
        rid_prop = _single_prop(first, "RECURRENCE-ID")
        rid = _reference_datetime(rid_prop, "RECURRENCE-ID") if rid_prop else None
        replies = {}
        for attendee in _parse_attendees(first, True):
            partstat = attendee.partstat
            if partstat not in {"ACCEPTED", "DECLINED", "TENTATIVE"}:
                raise CalendarError("REPLY PARTSTAT must be ACCEPTED, DECLINED or TENTATIVE")
            if attendee.email in replies:
                raise CalendarError(f"duplicate REPLY attendee {attendee.email}")
            replies[attendee.email] = (
                rid.replace(microsecond=0) if rid else None, partstat)
        return ParsedCalendar(method, uid, sequence, dtstamp, [], replies)
    # CANCEL: DTSTART and RECURRENCE-ID identify the affected master/instance.
    rids = []
    for event in events:
        rid_prop = _single_prop(event, "RECURRENCE-ID")
        rid = _reference_datetime(rid_prop, "RECURRENCE-ID") if rid_prop else None
        range_value = None
        if rid_prop is not None:
            rids.append(rid.replace(microsecond=0) if rid else None)
            raw_range = rid_prop.params.get("RANGE")
            if raw_range is not None:
                range_value = str(raw_range).upper()
                if range_value != "THISANDFUTURE":
                    raise CalendarError("only RANGE=THISANDFUTURE is supported")
    if len(rids) != len(set(rids)):
        raise CalendarError("duplicate RECURRENCE-ID in CANCEL")
    if len(events) == 1:
        rid = rids[0]
        range_value = None
        if rid is not None:
            prop = _single_prop(events[0], "RECURRENCE-ID")
            raw = prop.params.get("RANGE")
            range_value = str(raw).upper() if raw is not None else None
        return ParsedCalendar(method, uid, sequence, dtstamp, [],
                              cancellation=(rid, range_value))
    if any(x is None for x in rids):
        raise CalendarError("whole-series CANCEL cannot be combined with instances")
    # A batch of individual instance cancellations is represented as one record
    # per event by the caller; a single multipart message itself is unsupported
    # to keep version identity unambiguous.
    raise CalendarError("CANCEL may cancel only one target per HTTP message")
