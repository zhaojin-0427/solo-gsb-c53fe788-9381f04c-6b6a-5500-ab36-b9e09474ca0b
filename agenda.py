from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from calendar_logic import (
    UTC,
    RuleSpec,
    build_dateutil_rule,
    local_wall_for_rule,
    utc_text,
)


@dataclass
class StoredRule:
    row: sqlite3.Row
    rule: RuleSpec | None


def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _rule_from_json(text: str | None) -> RuleSpec | None:
    if not text:
        return None
    data = json.loads(text)
    return RuleSpec(
        freq=data["freq"], interval=data["interval"], count=data["count"],
        until_utc=parse_utc(data["until_utc"]) if data["until_utc"] else None,
        byday=[tuple(x) for x in data["byday"]], wkst=data["wkst"])



def _relevant_components(conn: sqlite3.Connection, attendee: str,
                         start: datetime, end: datetime):
    start_text = utc_text(start)
    end_text = utc_text(end)
    first_day = (start.date() - timedelta(days=1)).toordinal()
    last_day = (end.date() + timedelta(days=1)).toordinal()
    rows = conn.execute(
        """
        WITH RECURSIVE ords(o, wd) AS (
            SELECT ?, (?) % 7
            UNION ALL
            SELECT o+1, (wd+1) % 7 FROM ords WHERE o < ?
        )
        SELECT DISTINCT c.*, r.rule AS rule_json
        FROM current_component_attendees a
        JOIN current_components c
             ON c.uid=a.uid AND c.recurrence_id=a.recurrence_id
        LEFT JOIN component_rule_json r
             ON r.uid=c.uid AND r.recurrence_id=c.recurrence_id
        WHERE a.attendee=? AND (
          (c.freq IS NULL AND (
             (c.recurrence_id='' AND c.actual_start_utc >= ? AND c.actual_start_utc < ?)
             OR (c.recurrence_id<>'' AND (
                (c.actual_start_utc >= ? AND c.actual_start_utc < ?)
                OR (c.recurrence_id >= ? AND c.recurrence_id < ?)))
          ))
          OR (
            c.freq IS NOT NULL AND c.actual_start_utc < ?
            AND (c.last_utc IS NULL OR c.last_utc >= ?)
            AND (
              c.last_utc IS NOT NULL
              OR EXISTS (
                SELECT 1 FROM ords WHERE
                  (c.freq='DAILY' AND (o-c.anchor_day) % c.interval = 0)
                  OR (c.freq='WEEKLY' AND ((c.weekdays_mask >> wd) & 1)=1
                      AND (o-c.anchor_day+c.anchor_weekday-wd) % (7*c.interval) = 0)
              )
            )
          )
        )
        """,
        (first_day, (first_day - 1) % 7, last_day, attendee,
         start_text, end_text, start_text, end_text, start_text, end_text,
         end_text, start_text)).fetchall()
    if not rows:
        return {}
    uids = {r["uid"] for r in rows}
    grouped = {uid: [] for uid in uids}
    masters = conn.execute(
        """SELECT c.*, r.rule AS rule_json FROM current_components c
           LEFT JOIN component_rule_json r
           ON r.uid=c.uid AND r.recurrence_id=c.recurrence_id
           WHERE c.recurrence_id='' AND c.uid IN (%s)"""
        % ",".join("?" for _ in uids), tuple(uids)).fetchall()
    chosen = {}
    for row in rows + masters:
        chosen[(row["uid"], row["recurrence_id"])] = row
    for (uid, _), row in chosen.items():
        grouped[uid].append(StoredRule(row, _rule_from_json(row["rule_json"])))
    return grouped


def _wall_bounds(start: datetime, end: datetime, tzid: str):
    tz = ZoneInfo(tzid)
    return (start.astimezone(tz).replace(tzinfo=None),
            end.astimezone(tz).replace(tzinfo=None))


def _resolve(wall: datetime, tzid: str) -> datetime | None:
    value = local_wall_for_rule(wall, tzid)
    return value.astimezone(UTC).replace(microsecond=0) if value else None


def _iter_rule(rule: RuleSpec, wall_start: datetime, tzid: str,
               start: datetime, end: datetime, skip_first: bool = False):
    lo, hi = _wall_bounds(start, end, tzid)
    rr = build_dateutil_rule(rule, wall_start)
    for wall in rr.between(lo, hi, inc=True):
        if skip_first and wall == wall_start:
            continue
        actual = _resolve(wall, tzid)
        if actual is None:
            # Spring-forward gap: no real instant exists, so skip this nominal
            # member. During fall-back, fold=0 chooses the earlier occurrence.
            continue
        if start <= actual < end:
            yield wall, actual


def _attendees(conn: sqlite3.Connection, uid: str, recurrence_id: str) -> set[str]:
    rows = conn.execute(
        """SELECT attendee FROM current_component_attendees
           WHERE uid=? AND recurrence_id=?""", (uid, recurrence_id)).fetchall()
    return {r["attendee"] for r in rows}


def _eligible_versions(conn: sqlite3.Connection, uid: str,
                       sequence: int, request_dtstamp: str, table: str):
    rows = conn.execute(
        f"""SELECT * FROM {table}
            WHERE uid=? AND sequence=? AND dtstamp>=?
            ORDER BY dtstamp""",
        (uid, sequence, request_dtstamp)).fetchall()
    return rows


def _replies(conn, uid, sequence, dtstamp, attendee):
    rows = _eligible_versions(conn, uid, sequence, dtstamp, "reply_records")
    latest = {}
    for row in rows:
        if row["attendee"] != attendee:
            continue
        key = row["recurrence_id"]
        if key not in latest or row["dtstamp"] >= latest[key]["dtstamp"]:
            latest[key] = row
    return latest


def _cancels(conn, uid, sequence, dtstamp):
    return _eligible_versions(conn, uid, sequence, dtstamp, "cancel_records")


def _is_canceled(cancels, recurid_text: str) -> bool:
    recurid = parse_utc(recurid_text)
    for row in cancels:
        target = row["recurrence_id"]
        if target is None:
            return True
        target_dt = parse_utc(target)
        if target_dt == recurid:
            return True
        if row["range_value"] == "THISANDFUTURE" and recurid >= target_dt:
            return True
    return False


def _status(replies, recurid_text: str, canceled: bool):
    if canceled:
        return "cancelled"
    exact = replies.get(recurid_text)
    row = exact or replies.get(None)
    if row is None:
        return "pending"
    if row["partstat"] == "ACCEPTED":
        return "accepted"
    if row["partstat"] == "DECLINED":
        return "declined"
    return "pending"


def agenda_for(conn: sqlite3.Connection, attendee: str,
               start: datetime, end: datetime) -> list[dict]:
    grouped = _relevant_components(conn, attendee, start, end)
    output = {}
    for uid, components in grouped.items():
        rows = conn.execute(
            """SELECT c.*, r.rule AS rule_json FROM current_components c
               LEFT JOIN component_rule_json r
               ON r.uid=c.uid AND r.recurrence_id=c.recurrence_id
               WHERE c.uid=? AND c.recurrence_id<>'' AND c.range_value='THISANDFUTURE'""",
            (uid,)).fetchall()
        existing = {(x.row["uid"], x.row["recurrence_id"]) for x in components}
        for row in rows:
            if (uid, row["recurrence_id"]) not in existing:
                components.append(StoredRule(row, _rule_from_json(row["rule_json"])))
        master_stored = next((x for x in components if x.row["recurrence_id"] == ""), None)
        if master_stored is None:
            continue
        master = master_stored.row
        sequence = master["sequence"]
        request_dtstamp = master["dtstamp"]
        master_attendees = _attendees(conn, uid, "")
        replies = _replies(conn, uid, sequence, request_dtstamp, attendee)
        cancels = _cancels(conn, uid, sequence, request_dtstamp)
        overrides = [x for x in components if x.row["recurrence_id"] != ""]
        splits = [x for x in overrides if x.row["range_value"] == "THISANDFUTURE"]
        single = [x for x in overrides if x.row["range_value"] is None]
        split_cutoff = None
        if splits:
            split_cutoff = min(parse_utc(x.row["recurrence_id"]) for x in splits)
        exdates = {parse_utc(x) for x in json.loads(master["exdates"])}
        master_tz = master["tzid"]
        master_wall = datetime.fromisoformat(master["wall_start"])
        duration = timedelta(seconds=master["duration_seconds"])
        single_by_rid = {x.row["recurrence_id"]: x for x in single}
        split_by_rid = {x.row["recurrence_id"]: x for x in splits}

        master_canceled = master["status"] == "CANCELLED"
        if master_stored.rule is None:
            actual_start = parse_utc(master["actual_start_utc"])
            if start <= actual_start < end and attendee in master_attendees:
                rid = ""
                item_key = (uid, rid)
                canceled = master_canceled or _is_canceled(cancels, utc_text(actual_start))
                # Non-recurring master is addressed by whole-scope replies and
                # cancellation only; RECURRENCE-ID is null for clients.
                status = _status(replies, "", canceled)
                output[item_key] = {
                    "uid": uid, "recurrence_id": None,
                    "start": master["actual_start_utc"],
                    "end": master["actual_end_utc"], "status": status}
        else:
            for _, actual in _iter_rule(master_stored.rule, master_wall,
                                       master_tz, start, end):
                rid_text = utc_text(actual)
                if split_cutoff is not None and actual >= split_cutoff:
                    continue
                rid_dt = parse_utc(rid_text)
                if rid_dt in exdates and rid_text not in single_by_rid:
                    continue
                if rid_text in split_by_rid:
                    continue
                override = single_by_rid.get(rid_text)
                if override:
                    row = override.row
                    if attendee not in _attendees(conn, uid, rid_text):
                        continue
                    item_start = parse_utc(row["actual_start_utc"])
                    item_end = parse_utc(row["actual_end_utc"])
                    rec_out = rid_text
                    if not (start <= item_start < end):
                        continue
                else:
                    if attendee not in master_attendees:
                        continue
                    item_start = actual
                    item_end = actual + duration
                    rec_out = rid_text
                canceled = (_is_canceled(cancels, rid_text)
                            or (override is not None and override.row["status"] == "CANCELLED"))
                status = _status(replies, rid_text, canceled)
                output[(uid, rec_out)] = {
                    "uid": uid, "recurrence_id": rec_out,
                    "start": utc_text(item_start), "end": utc_text(item_end),
                    "status": status}

            # A moved exception can leave its original slot outside the window
            # while its new start is inside. Exceptions always win.
            for stored_override in single:
                row = stored_override.row
                rid_text = row["recurrence_id"]
                item_start = parse_utc(row["actual_start_utc"])
                if not (start <= item_start < end):
                    continue
                if attendee not in _attendees(conn, uid, rid_text):
                    continue
                key = (uid, rid_text)
                output[key] = {
                    "uid": uid, "recurrence_id": rid_text,
                    "start": utc_text(item_start),
                    "end": row["actual_end_utc"],
                    "status": _status(replies, rid_text,
                                      _is_canceled(cancels, rid_text)
                                      or row["status"] == "CANCELLED")}

            for split_stored in splits:
                row = split_stored.row
                boundary = row["recurrence_id"]
                component_attendees = _attendees(conn, uid, boundary)
                if attendee in component_attendees and start <= parse_utc(row["actual_start_utc"]) < end:
                    output[(uid, boundary)] = {
                        "uid": uid, "recurrence_id": boundary,
                        "start": row["actual_start_utc"],
                        "end": row["actual_end_utc"],
                        "status": _status(replies, boundary,
                                          _is_canceled(cancels, boundary)
                                          or row["status"] == "CANCELLED")}
                if split_stored.rule is None:
                    continue
                # DTSTART of the replacement rule represents the moved boundary.
                for _, actual in _iter_rule(
                        split_stored.rule, datetime.fromisoformat(row["wall_start"]),
                        row["tzid"], start, end, True):
                    split_exdates = {
                        parse_utc(x) for x in json.loads(row["exdates"])}
                    if actual in split_exdates:
                        continue
                    rid_text = utc_text(actual)
                    if attendee not in component_attendees:
                        continue
                    output[(uid, rid_text)] = {
                        "uid": uid, "recurrence_id": rid_text,
                        "start": rid_text,
                        "end": utc_text(actual + timedelta(seconds=row["duration_seconds"])),
                        "status": _status(replies, rid_text,
                                          _is_canceled(cancels, rid_text))}

    return [output[k] for k in sorted(output, key=lambda x: (x[0], output[x]["start"], x[1]))]
