"""Adjudication of REQUEST / CANCEL / REPLY components against stored state.

Ordering model (iTIP RFC 5545/2446 semantics as implemented here)
--------------------------------------------------------------------------------
* Identity is (UID, RECURRENCE-ID-or-master).
* Notifications are ordered by SEQUENCE then DTSTAMP; older versions are
  ignored and never overwrite newer ones ("旧版回复不沿用").
* Identical content re-delivered is a no-op (replay idempotent); same UID +
  same SEQUENCE + different content for a REQUEST is a hard conflict (409).
* CANCEL / REPLY that cannot yet bind to a known meeting are held in
  ``staging``; when the matching REQUEST arrives they are flushed together
  ("先到回复/取消待请求补到再裁决").
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from . import rrulex
from .errors import CalendarError, ConflictError
from .ics_parse import Component


# ---------------------------------------------------------------------------
# small read helpers
# ---------------------------------------------------------------------------

def get_master(conn: sqlite3.Connection, uid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM vevents WHERE uid=? AND is_master=1", (uid,)
    ).fetchone()


def get_override(conn, uid, rid) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM vevents WHERE uid=? AND is_master=0 AND rid=?",
        (uid, rid),
    ).fetchone()


def get_cancel(conn, uid, rid="") -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cancelled_instances WHERE uid=? AND rid=?", (uid, rid)
    ).fetchone()


def get_reply(conn, uid, rid, email) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM replies WHERE uid=? AND rid=? AND email=?",
        (uid, rid, email),
    ).fetchone()


def master_exdates(conn, uid) -> set[str]:
    return {r["rid"] for r in conn.execute(
        "SELECT rid FROM exdates WHERE uid=?", (uid,))}


# ---------------------------------------------------------------------------
# RECURRENCE-ID resolution
# ---------------------------------------------------------------------------

def resolve_occurrence(conn, master: sqlite3.Row, comp: Component,
                       allow_exdate: bool = False) -> str:
    """Resolve a possibly-sloppy RECURRENCE-ID to an original occurrence key.

    Exact master-rule match wins.  Otherwise the RID is snapped back to the
    closest earlier valid occurrence, jumping over EXDATE holes.  An override
    whose moved DTSTART equals the RID is also accepted (moved instance
    references).  ``allow_exdate`` lets a REQUEST exception re-create a slot
    that the master marked EXDATE (single exception wins).
    """
    rid = comp.rid
    exdates = {rrulex.parse_utc(x) for x in master_exdates(conn, master["uid"])}
    rr = rrulex.get_rrule(master["rrule"], master["start_utc"], master["rrule_tzid"])
    target = rrulex.parse_utc(rid)

    # 1) direct rule member
    if rrulex.occurrence_index(rr, target) is not None:
        if allow_exdate or target not in exdates:
            return rrulex.to_utc_iso(target)

    # 2) reference by the (moved) start of an already stored override
    moved = conn.execute(
        "SELECT rid FROM vevents WHERE uid=? AND is_master=0 AND start_utc=?",
        (master["uid"], rid),
    ).fetchone()
    if moved is not None:
        return moved["rid"]

    # 3) snap back to the nearest earlier rule member not in EXDATE
    snapped = rrulex.earlier_valid_occurrence(rr, target, exdates=exdates)
    if snapped is None:
        raise CalendarError(
            f"RECURRENCE-ID {rid} does not match any occurrence of {master['uid']}"
        )
    return rrulex.to_utc_iso(snapped)


def ensure_recurring_rid(conn, master, comp: Component,
                         allow_exdate: bool = False) -> str:
    if not master["rrule"]:
        raise CalendarError(
            f"RECURRENCE-ID on non-recurring UID {comp.uid} is not allowed"
        )
    return resolve_occurrence(conn, master, comp, allow_exdate=allow_exdate)


# ---------------------------------------------------------------------------
# top level: apply one parsed component inside the open transaction
# ---------------------------------------------------------------------------

def apply_component(conn, c: Component) -> str:
    """Return one of: stored | duplicate | ignored | staged."""
    if c.kind == "REQUEST":
        return _apply_request(conn, c)
    if c.kind == "CANCEL":
        return _apply_cancel(conn, c)
    return _apply_reply(conn, c)


def _newer(a_seq, a_stamp, b_seq, b_stamp) -> bool:
    """True if version a is strictly newer than b."""
    return (a_seq, a_stamp) > (b_seq, b_stamp)


def _same_version(new_seq, new_stamp, old_seq, old_stamp) -> bool:
    return new_seq == old_seq and new_stamp == old_stamp


def _check_same_version_conflict(existing, c, rid) -> None:
    """Same UID(+RID), same SEQUENCE, same DTSTAMP, different text -> 409."""
    if existing is None or existing["content_hash"] == c.content_hash:
        return
    if c.seq == existing["seq"] and c.dtstamp == existing["dtstamp"]:
        raise ConflictError(c.uid, rid or "", c.seq)


# ---------------------------------------------------------------------------
# REQUEST
# ---------------------------------------------------------------------------

def _apply_request(conn, c: Component) -> str:
    if c.is_master:
        return _apply_master_request(conn, c)
    return _apply_override_request(conn, c)


def _wipe_uid(conn, uid: str) -> None:
    """Replace the whole known state of a UID (new master version wins)."""
    conn.execute("DELETE FROM vevents WHERE uid=?", (uid,))
    conn.execute("DELETE FROM exdates WHERE uid=?", (uid,))
    conn.execute("DELETE FROM cancelled_instances WHERE uid=?", (uid,))
    conn.execute("DELETE FROM replies WHERE uid=?", (uid,))
    conn.execute("DELETE FROM attendees WHERE uid=?", (uid,))


def _apply_master_request(conn, c: Component) -> str:
    existing = get_master(conn, c.uid)
    if existing is not None:
        if existing["content_hash"] == c.content_hash:
            return "duplicate"
        _check_same_version_conflict(existing, c, "")
        if not _newer(c.seq, c.dtstamp, existing["seq"], existing["dtstamp"]):
            return "ignored"
        if c.seq > existing["seq"]:
            # newer SEQUENCE: replace everything about this meeting
            _wipe_uid(conn, c.uid)
        # same SEQUENCE, later DTSTAMP, different content: treat as a
        # content refresh at the same sequence (sweep the old row below)

    whole_cancel = get_cancel(conn, c.uid, "")
    if whole_cancel is not None and not _newer(
        c.seq, c.dtstamp, whole_cancel["seq"], whole_cancel["dtstamp"]
    ):
        # A newer/equal whole-meeting CANCEL already won; the REQUEST is stale.
        return "ignored"

    conn.execute(
        """INSERT INTO vevents
           (uid,is_master,rid,seq,dtstamp,start_utc,end_utc,duration_s,
            rrule,rrule_tzid,rrule_until,rrule_count,rrule_last,is_range,
            is_all_day,content_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
           ON CONFLICT(uid,rid) DO UPDATE SET
             seq=excluded.seq, dtstamp=excluded.dtstamp,
             start_utc=excluded.start_utc, end_utc=excluded.end_utc,
             duration_s=excluded.duration_s, rrule=excluded.rrule,
             rrule_tzid=excluded.rrule_tzid, rrule_until=excluded.rrule_until,
             rrule_count=excluded.rrule_count, rrule_last=excluded.rrule_last,
             is_all_day=excluded.is_all_day, content_hash=excluded.content_hash""",
        (c.uid, 1, "", c.seq, c.dtstamp, c.start, c.end, c.duration_s,
         c.rrule_json, c.rrule_tzid, c.rrule_until, c.rrule_count, c.rrule_last,
         1 if c.all_day else 0, c.content_hash),
    )
    # EXDATE set comes from the newest master text (wipe + reinsert).
    conn.execute("DELETE FROM exdates WHERE uid=?", (c.uid,))
    for rid in c.exdates:
        conn.execute("INSERT OR IGNORE INTO exdates(uid,rid) VALUES (?,?)",
                     (c.uid, rid))
    for email, _partstat in c.attendees:
        conn.execute("INSERT OR IGNORE INTO attendees(uid,email) VALUES (?,?)",
                     (c.uid, email))
    if whole_cancel is not None:  # newer REQUEST revives the meeting
        conn.execute("DELETE FROM cancelled_instances WHERE uid=? AND rid=''",
                     (c.uid,))
    return "stored"


def _apply_override_request(conn, c: Component) -> str:
    master = get_master(conn, c.uid)
    if master is None:
        # An override before its master cannot be adjudicated yet. Hold it.
        _stage(conn, c)
        return "staged"

    # An override must belong to the current (or future) master version.
    if c.seq < master["seq"]:
        return "ignored"

    # A REQUEST exception may re-create a slot the master marked EXDATE.
    rid = ensure_recurring_rid(conn, master, c, allow_exdate=True)

    existing = get_override(conn, c.uid, rid)
    if existing is not None:
        if existing["content_hash"] == c.content_hash:
            return "duplicate"
        _check_same_version_conflict(existing, c, rid)
        if not _newer(c.seq, c.dtstamp, existing["seq"], existing["dtstamp"]):
            return "ignored"

    # A CANCEL for this instance outranks an equal/older REQUEST override.
    canc = get_cancel(conn, c.uid, rid)
    if canc is not None and not _newer(
        c.seq, c.dtstamp, canc["seq"], canc["dtstamp"]
    ):
        return "ignored"

    conn.execute(
        """INSERT INTO vevents
           (uid,is_master,rid,seq,dtstamp,start_utc,end_utc,duration_s,
            rrule,rrule_tzid,rrule_until,rrule_count,rrule_last,is_range,
            is_all_day,content_hash)
           VALUES (?,0,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,?,?,?)
           ON CONFLICT(uid,rid) DO UPDATE SET
             seq=excluded.seq, dtstamp=excluded.dtstamp,
             start_utc=excluded.start_utc, end_utc=excluded.end_utc,
             duration_s=excluded.duration_s, is_range=excluded.is_range,
             is_all_day=excluded.is_all_day, content_hash=excluded.content_hash""",
        (c.uid, rid, c.seq, c.dtstamp, c.start, c.end, c.duration_s,
         1 if c.range_future else 0, 1 if c.all_day else 0, c.content_hash),
    )
    # A reschedule invalidates replies that targeted the old text of the
    # single instance; replies to the series master are untouched.
    conn.execute(
        "DELETE FROM replies WHERE uid=? AND rid=? AND seq<=?",
        (c.uid, rid, c.seq),
    )
    if canc is not None and _newer(c.seq, c.dtstamp, canc["seq"], canc["dtstamp"]):
        conn.execute("DELETE FROM cancelled_instances WHERE uid=? AND rid=?",
                     (c.uid, rid))
    return "stored"


# ---------------------------------------------------------------------------
# CANCEL
# ---------------------------------------------------------------------------

def _apply_cancel(conn, c: Component) -> str:
    master = get_master(conn, c.uid)
    if master is None:
        _stage(conn, c)
        return "staged"
    if c.seq < master["seq"]:
        return "ignored"

    if c.rid is None or not master["rrule"]:
        # Whole-UID CANCEL, or a CANCEL aimed at the sole instance of a
        # non-recurring meeting: rendered as a whole-meeting cancellation.
        return _apply_whole_cancel(conn, c, master)

    rid = ensure_recurring_rid(conn, master, c)
    return _apply_instance_cancel(conn, c, master, rid)


def _apply_whole_cancel(conn, c, master) -> str:
    existing = get_cancel(conn, c.uid, "")
    if existing is not None:
        if existing["content_hash"] == c.content_hash:
            return "duplicate"
        _check_same_version_conflict(existing, c, "")
        if not _newer(c.seq, c.dtstamp, existing["seq"], existing["dtstamp"]):
            return "ignored"
    # A whole-series cancel replaces earlier per-instance cancel markers.
    conn.execute("DELETE FROM cancelled_instances WHERE uid=?", (c.uid,))
    conn.execute(
        """INSERT INTO cancelled_instances(uid,rid,seq,dtstamp,is_range,content_hash)
           VALUES (?, '', ?, ?, 0, ?)
           ON CONFLICT(uid,rid) DO UPDATE SET
             seq=excluded.seq, dtstamp=excluded.dtstamp,
             content_hash=excluded.content_hash""",
        (c.uid, c.seq, c.dtstamp, c.content_hash),
    )
    return "stored"


def _apply_instance_cancel(conn, c, master, rid) -> str:
    existing = get_cancel(conn, c.uid, rid)
    if existing is not None:
        if existing["content_hash"] == c.content_hash:
            return "duplicate"
        _check_same_version_conflict(existing, c, rid)
        if not _newer(c.seq, c.dtstamp, existing["seq"], existing["dtstamp"]):
            return "ignored"

    # An equal/newer REQUEST override for the instance keeps the instance.
    ov = get_override(conn, c.uid, rid)
    if ov is not None and not _newer(
        c.seq, c.dtstamp, ov["seq"], ov["dtstamp"]
    ):
        return "ignored"

    conn.execute(
        """INSERT INTO cancelled_instances(uid,rid,seq,dtstamp,is_range,content_hash)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(uid,rid) DO UPDATE SET
             seq=excluded.seq, dtstamp=excluded.dtstamp,
             is_range=excluded.is_range, content_hash=excluded.content_hash""",
        (c.uid, rid, c.seq, c.dtstamp, 1 if c.range_future else 0, c.content_hash),
    )
    if c.range_future:
        # THISANDFUTURE removes later tail overrides at/after the boundary.
        conn.execute(
            "DELETE FROM vevents WHERE uid=? AND is_master=0 AND rid>=? AND is_range=1",
            (c.uid, rid),
        )
        conn.execute(
            "DELETE FROM replies WHERE uid=? AND rid>=? AND seq<=?",
            (c.uid, rid, c.seq),
        )
    else:
        conn.execute(
            "DELETE FROM replies WHERE uid=? AND rid=? AND seq<=?",
            (c.uid, rid, c.seq),
        )
    return "stored"


# ---------------------------------------------------------------------------
# REPLY
# ---------------------------------------------------------------------------

_PARTSTAT_TO_STATUS = {
    "ACCEPTED": "accepted",
    "DECLINED": "declined",
    "TENTATIVE": "tentative",
    "DELEGATED": "delegated",
    "NEEDS-ACTION": "needs-action",
}


def _apply_reply(conn, c: Component) -> str:
    master = get_master(conn, c.uid)
    if master is None:
        _stage(conn, c)
        return "staged"
    if c.seq < master["seq"]:
        return "ignored"

    email = c.attendees[0][0]
    partstat = c.partstat

    if c.rid is None:
        rid = ""  # series-wide / single-meeting reply
    elif not master["rrule"]:
        # A reply to a single meeting carries no RID; a RID there is invalid.
        raise CalendarError(
            f"RECURRENCE-ID on non-recurring UID {c.uid} is not allowed"
        )
    else:
        rid = ensure_recurring_rid(conn, master, c)

    # A cancel at or after this version moots the reply.
    canc = get_cancel(conn, c.uid, "") if rid == "" else get_cancel(conn, c.uid, rid)
    if canc is not None and not _newer(c.seq, c.dtstamp, canc["seq"], canc["dtstamp"]):
        return "ignored"

    # Instance replies must not predate the currently stored instance
# version (SEQUENCE then DTSTAMP); an old-version reply never carries
    # forward to a rescheduled instance.
    if rid:
        ov = get_override(conn, c.uid, rid)
        if ov is not None and not _newer(
            c.seq, c.dtstamp, ov["seq"], ov["dtstamp"]
        ):
            return "ignored"

    existing = get_reply(conn, c.uid, rid, email)
    if existing is not None:
        if existing["content_hash"] == c.content_hash:
            return "duplicate"
        _check_same_version_conflict(existing, c, rid)
        if not _newer(c.seq, c.dtstamp, existing["seq"], existing["dtstamp"]):
            return "ignored"

    conn.execute(
        """INSERT INTO replies(uid,rid,email,partstat,seq,dtstamp,content_hash)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(uid,rid,email) DO UPDATE SET
             partstat=excluded.partstat, seq=excluded.seq,
             dtstamp=excluded.dtstamp, content_hash=excluded.content_hash""",
        (c.uid, rid, email, partstat, c.seq, c.dtstamp, c.content_hash),
    )
    return "stored"


# ---------------------------------------------------------------------------
# staging (messages arriving before the REQUEST that defines the meeting)
# ---------------------------------------------------------------------------

def _stage(conn, c: Component) -> None:
    conn.execute(
        "INSERT INTO staging(uid,kind,content,content_hash) VALUES (?,?,?,?)",
        (c.uid, c.kind, c.raw_blob, c.content_hash),
    )


def flush_staging(conn, uids: set[str], max_count: int) -> None:
    """Re-evaluate held CANCEL/REPLY messages once a REQUEST may unblock them.

    Runs inside the caller's transaction.  Messages still unbindable remain
    staged; stale ones (newer master already superseded them) are dropped.
    """
    from .ics_parse import parse_message

    if not uids:
        return
    # Repeat passes so held components that unlock other held components
    # (e.g. a staged REQUEST override unblocking a staged REPLY) resolve.
    for _ in range(8):
        placeholders = ",".join("?" for _ in uids)
        rows = conn.execute(
            f"SELECT id, uid, kind, content FROM staging "
            f"WHERE uid IN ({placeholders}) ORDER BY id",
            tuple(uids),
        ).fetchall()
        if not rows:
            break
        # Within a flush, held REQUESTs (e.g. an override whose master has
        # just arrived) are applied before CANCEL/REPLY so they can bind.
        rows.sort(key=lambda r: 0 if r["kind"] == "REQUEST" else 1)
        progressed = False
        for row in rows:
            blob = (
                b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:"
                + row["kind"].encode() + b"\r\n"
                + bytes(row["content"])
                + b"END:VCALENDAR\r\n"
            )
            msg = parse_message(blob, max_count)
            comp = msg.components[0]
            result = apply_component(conn, comp)
            if result != "staged":
                conn.execute("DELETE FROM staging WHERE id=?", (row["id"],))
                progressed = True
        if not progressed:
            break
