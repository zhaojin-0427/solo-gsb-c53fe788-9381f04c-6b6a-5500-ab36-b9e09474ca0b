from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from calendar_logic import (
    UTC,
    CalendarError,
    ComponentSpec,
    RuleSpec,
    build_dateutil_rule,
    local_wall_for_rule,
    utc_text,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(database: str = "/data/meeting.db") -> sqlite3.Connection:
    db_path = Path(database)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def rule_to_json(rule: RuleSpec | None):
    if rule is None:
        return None
    return {
        "freq": rule.freq,
        "interval": rule.interval,
        "count": rule.count,
        "until_utc": utc_text(rule.until_utc) if rule.until_utc else None,
        "byday": rule.byday,
        "wkst": rule.wkst,
    }


def _generated_last(component: ComponentSpec, rule: RuleSpec) -> str | None:
    if rule.count is None and rule.until_utc is None:
        return None
    # Find the finite rule's last nominal member without expanding an infinite
    # RRULE. If that wall time is swallowed by a spring-forward gap, walk
    # backwards to the nearest real member.
    rr = build_dateutil_rule(rule, component.wall_start)
    if rule.count is not None:
        wall = rr[rule.count - 1]
    else:
        local_until = rule.until_utc.astimezone(ZoneInfo(component.tzid)).replace(tzinfo=None)
        wall = rr.before(local_until, inc=True)
    actual = local_wall_for_rule(wall, component.tzid)
    while actual is None:
        wall = rr.before(wall, inc=False)
        if wall is None or wall < component.wall_start:
            return utc_text(component.actual_start_utc)
        actual = local_wall_for_rule(wall, component.tzid)
    return utc_text(actual)


def _component_row(component: ComponentSpec, rule: RuleSpec | None, digest: str) -> dict:
    wall = component.wall_start
    weekdays = [day for _, day in (rule.byday if rule else [])]
    mask = 0
    for day in weekdays:
        mask |= 1 << day
    if rule and rule.freq == "WEEKLY" and not weekdays:
        # An inherited THISANDFUTURE segment loses the parent's BYDAY and is
        # therefore anchored solely on the replacement DTSTART's weekday.
        mask = 1 << wall.weekday()
    return {
        "uid": component.uid,
        "recurrence_id": utc_text(component.recurrence_id_utc) if component.recurrence_id_utc else "",
        "range_value": component.range_value,
        "tzid": component.tzid,
        "wall_start": wall.isoformat(),
        "actual_start_utc": utc_text(component.actual_start_utc),
        "actual_end_utc": utc_text(component.actual_end_utc),
        "duration_seconds": int(component.duration.total_seconds()),
        "anchor_day": wall.toordinal(),
        "anchor_weekday": wall.weekday(),
        "freq": rule.freq if rule else None,
        "interval": rule.interval if rule else 1,
        "weekdays_mask": mask,
        "wkst": rule.wkst if rule else 0,
        "rrule_count": rule.count if rule else None,
        "until_utc": utc_text(rule.until_utc) if rule and rule.until_utc else None,
        "last_utc": _generated_last(component, rule) if rule else utc_text(component.actual_start_utc),
        "byday": _json(weekdays),
        "exdates": _json([utc_text(x) for x in component.exdates_utc]),
        "summary": component.summary,
        "status": component.status,
        "digest": digest,
        "sequence": component.sequence,
        "dtstamp": utc_text(component.dtstamp),
    }


def latest_version(conn: sqlite3.Connection, uid: str, method: str):
    if method == "REPLY":
        row = conn.execute(
            """SELECT sequence, dtstamp FROM reply_records WHERE uid=?
               ORDER BY sequence DESC, dtstamp DESC LIMIT 1""", (uid,)).fetchone()
    elif method == "CANCEL":
        row = conn.execute(
            """SELECT sequence, dtstamp FROM cancel_records WHERE uid=?
               ORDER BY sequence DESC, dtstamp DESC LIMIT 1""", (uid,)).fetchone()
    else:
        row = conn.execute(
            """SELECT sequence, dtstamp FROM request_versions WHERE uid=?
               ORDER BY sequence DESC, dtstamp DESC LIMIT 1""", (uid,)).fetchone()
    return (row["sequence"], row["dtstamp"]) if row else None


def existing_digest(conn: sqlite3.Connection, method: str, uid: str,
                    sequence: int, dtstamp: str) -> str | None:
    if method == "REQUEST":
        rows = conn.execute(
            "SELECT DISTINCT digest FROM request_versions WHERE uid=? AND sequence=? AND dtstamp=?",
            (uid, sequence, dtstamp)).fetchall()
    elif method == "CANCEL":
        rows = conn.execute(
            "SELECT DISTINCT digest FROM cancel_records WHERE uid=? AND sequence=? AND dtstamp=?",
            (uid, sequence, dtstamp)).fetchall()
    else:
        return None
    return rows[0]["digest"] if len(rows) == 1 else (rows[0]["digest"] if rows else None)


def is_newer(incoming: tuple[int, str], current: tuple[int, str] | None) -> bool:
    return current is None or (incoming[0], incoming[1]) > current


def _inherit_split_rule(master: ComponentSpec, split: ComponentSpec) -> RuleSpec | None:
    if split.rule is not None:
        return split.rule
    if master.rule is None or split.range_value is None:
        return None
    rule = master.rule
    remaining_count = None
    if rule.count is not None:
        # Generate only up to the boundary index, bounded by declared count.
        generated = build_dateutil_rule(rule, master.wall_start)
        boundary_index = None
        for index, wall in enumerate(generated):
            actual = local_wall_for_rule(wall, master.tzid)
            if actual is not None and actual == split.recurrence_id_utc:
                boundary_index = index
                break
            if rule.count is not None and index >= rule.count:
                break
        if boundary_index is None:
            raise CalendarError("THISANDFUTURE does not reference a generated occurrence")
        remaining_count = rule.count - boundary_index
        if remaining_count < 1:
            raise CalendarError("THISANDFUTURE starts after recurrence ended")
    inherited_byday = [] if rule.freq == "WEEKLY" else rule.byday
    return RuleSpec(rule.freq, rule.interval, remaining_count, rule.until_utc,
                    inherited_byday, rule.wkst)


def _validate_exceptions(master: ComponentSpec,
                         exceptions: list[ComponentSpec]) -> None:
    if not exceptions:
        return
    referenced = {c.recurrence_id_utc for c in exceptions}
    if master.rule is None:
        if any(c.range_value for c in exceptions):
            raise CalendarError("THISANDFUTURE requires a recurring master RRULE")
        if referenced != {master.actual_start_utc}:
            raise CalendarError("RECURRENCE-ID must match the non-recurring DTSTART")
        return
    rr = build_dateutil_rule(master.rule, master.wall_start)
    generated = set()
    if master.rule.count is None and master.rule.until_utc is None:
        # Never iterate an unbounded RRULE. Search a finite interval guaranteed
        # to contain the referenced nominal day/weekday.
        for target in referenced:
            local = target.astimezone(ZoneInfo(master.tzid)).replace(tzinfo=None)
            search_days = 14 * master.rule.interval
            lo = local - timedelta(days=search_days)
            hi = local + timedelta(days=search_days)
            for wall in rr.between(lo, hi, inc=True):
                actual = local_wall_for_rule(wall, master.tzid)
                if actual is not None:
                    generated.add(actual.astimezone(UTC).replace(microsecond=0))
    else:
        for wall in rr:
            actual = local_wall_for_rule(wall, master.tzid)
            if actual is not None:
                generated.add(actual.astimezone(UTC).replace(microsecond=0))
    unknown = referenced - generated
    if unknown:
        raise CalendarError("RECURRENCE-ID does not match a generated occurrence")


def apply_request(conn: sqlite3.Connection, components: list[ComponentSpec],
                  sequence: int, dtstamp: datetime, digest: str) -> None:
    master = next(c for c in components if c.recurrence_id_utc is None)
    exceptions = [c for c in components if c.recurrence_id_utc is not None]
    _validate_exceptions(master, exceptions)
    dtstamp_text = utc_text(dtstamp)
    effective_rules = {}
    for component in components:
        rule = _inherit_split_rule(master, component)
        if component.recurrence_id_utc is not None and component.range_value is None:
            rule = None
        effective_rules[id(component)] = rule
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO request_versions(uid, sequence, dtstamp, digest) VALUES(?,?,?,?)",
            (master.uid, sequence, dtstamp_text, digest))
        conn.execute("DELETE FROM current_components WHERE uid=?", (master.uid,))
        conn.execute("DELETE FROM component_rule_json WHERE uid=?", (master.uid,))
        conn.execute("DELETE FROM current_component_attendees WHERE uid=?", (master.uid,))
        rows = []
        for component in components:
            effective_rule = effective_rules[id(component)]
            row = _component_row(component, effective_rule, digest)
            rows.append(row)
            insert = """INSERT INTO current_components
                (uid, recurrence_id, range_value, tzid, wall_start,
                 actual_start_utc, actual_end_utc, duration_seconds,
                 anchor_day, anchor_weekday, freq, interval, weekdays_mask,
                 wkst, rrule_count, until_utc, last_utc, byday, exdates,
                 summary, status, digest, sequence, dtstamp)
                VALUES (:uid,:recurrence_id,:range_value,:tzid,:wall_start,
                 :actual_start_utc,:actual_end_utc,:duration_seconds,
                 :anchor_day,:anchor_weekday,:freq,:interval,:weekdays_mask,
                 :wkst,:rrule_count,:until_utc,:last_utc,:byday,:exdates,
                 :summary,:status,:digest,:sequence,:dtstamp)"""
            conn.execute(insert, row)
            attendees = component.attendees or master.attendees
            for attendee in attendees:
                conn.execute(
                    """INSERT OR REPLACE INTO current_component_attendees
                       (uid, recurrence_id, attendee) VALUES(?,?,?)""",
                    (component.uid,
                     utc_text(component.recurrence_id_utc) if component.recurrence_id_utc else "",
                     attendee.email))
        for component in components:
            effective_rule = effective_rules[id(component)]
            conn.execute(
                "INSERT OR REPLACE INTO component_rule_json(uid, recurrence_id, rule) VALUES(?,?,?)",
                (master.uid,
                 utc_text(component.recurrence_id_utc) if component.recurrence_id_utc else "",
                 _json(rule_to_json(effective_rule))))


def record_reply(conn: sqlite3.Connection, uid: str, sequence: int,
                 dtstamp: datetime, recurrence_id: str | None,
                 attendee: str, partstat: str, digest: str) -> None:
    text = utc_text(dtstamp)
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO reply_records
               (uid, sequence, dtstamp, recurrence_id, attendee, partstat, digest)
               VALUES(?,?,?,?,?,?,?)""",
            (uid, sequence, text, recurrence_id, attendee, partstat, digest))


def record_cancel(conn: sqlite3.Connection, uid: str, sequence: int,
                  dtstamp: datetime, recurrence_id: str | None,
                  range_value: str | None, digest: str) -> None:
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO cancel_records
               (uid, sequence, dtstamp, recurrence_id, range_value, digest)
               VALUES(?,?,?,?,?,?)""",
            (uid, sequence, utc_text(dtstamp), recurrence_id, range_value, digest))


def record_message(conn: sqlite3.Connection, uid: str, method: str,
                   sequence: int, dtstamp: datetime, digest: str,
                   result: str, content: bytes) -> None:
    with conn:
        conn.execute(
            """INSERT INTO messages(uid, method, sequence, dtstamp, digest, result, content)
               VALUES(?,?,?,?,?,?,?)""",
            (uid, method, sequence, utc_text(dtstamp), digest, result,
             content.decode("utf-8", errors="replace")))
