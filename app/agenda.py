"""Render an attendee's agenda over a half-open UTC window.

Expansion is always bounded by the (padded) query window; infinite RRULEs are
never pre-expanded.  Semantics:

* Per-instance single overrides replace only their occurrence; the original
  RECURRENCE-ID identity is preserved even when the meeting moves.
* RANGE=THISANDFUTURE REQUEST splits the series: head occurrences keep the
  master cadence, the tail continues from the override's DTSTART with the same
  cadence/COUNT/UNTIL (a bounded shift).
* RANGE=THISANDFUTURE CANCEL marks its occurrence and every later one
  cancelled, unless a strictly newer RANGE override revives the tail.
* EXDATE holes are skipped on master/head cadence only.
* Status per instance: CANCEL markers vs REQUEST versions are adjudicated by
  (SEQUENCE, DTSTAMP); otherwise the latest matching REPLY wins (instance
  reply beats series reply), defaulting to needs-action.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

from dateutil import rrule as _rr

from . import rrulex

# Moved instances can land earlier than their original slot; pad the window
# used for master expansion accordingly.
WINDOW_PAD = dt.timedelta(days=400)


def _iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def agenda(conn, attendee: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
    uids, padded = _candidate_uids(conn, attendee, start, end)
    results: list[dict] = []
    for uid in uids:
        results.extend(_render_uid(conn, attendee, uid, start, end,
                                   padded=uid in padded))
    results.sort(key=lambda e: (e["start"], e["uid"], e["recurrence_id"] or ""))
    return results


# ---------------------------------------------------------------------------
# candidate UIDs (indexed prefilters; rrule work only for plausible series)
# ---------------------------------------------------------------------------

def _candidate_uids(conn, email, start, end) -> tuple[list[str], set[str]]:
    """Return (candidate uids, uids that need padded recurrence expansion)."""
    uids: set[str] = set()
    padded: set[str] = set()

    rows = conn.execute(
        """SELECT DISTINCT v.uid FROM vevents v
           JOIN attendees a ON a.uid = v.uid
           WHERE a.email = ? AND v.is_master=1 AND v.rrule IS NULL
             AND v.start_utc < ? AND v.end_utc > ?""",
        (email, _iso(end), _iso(start)),
    ).fetchall()
    uids.update(r["uid"] for r in rows)

    # Recurring series with an occurrence directly in the window: tight,
    # unpadded expansion (infinite rules enumerate at most a week).
    rows = conn.execute(
        """SELECT v.* FROM vevents v
           JOIN attendees a ON a.uid = v.uid
           WHERE a.email = ? AND v.is_master=1 AND v.rrule IS NOT NULL
             AND v.start_utc < ?
             AND (v.rrule_last IS NULL OR v.rrule_last >= ?)""",
        (email, _iso(end), _iso(start)),
    ).fetchall()
    for m in rows:
        rr = rrulex.get_rrule(m["rrule"], m["start_utc"], m["rrule_tzid"])
        if rr.between(start, end, inc=True):
            uids.add(m["uid"])

    # Series with a moved override whose display time lands near/in the window
    # need padded expansion so the moved instance keeps its original identity.
    rows = conn.execute(
        """SELECT DISTINCT v.uid FROM vevents v
           JOIN attendees a ON a.uid = v.uid
           WHERE a.email = ? AND v.is_master=0
             AND v.start_utc >= ? AND v.start_utc < ?""",
        (email, _iso(start - WINDOW_PAD), _iso(end)),
    ).fetchall()
    for r in rows:
        uids.add(r["uid"])
        padded.add(r["uid"])
    return sorted(uids), padded


# ---------------------------------------------------------------------------
# per-UID rendering
# ---------------------------------------------------------------------------

def _render_uid(conn, email, uid, win_start, win_end, padded=False) -> list[dict]:
    master = conn.execute(
        "SELECT * FROM vevents WHERE uid=? AND is_master=1", (uid,)
    ).fetchone()
    if master is None:
        return []

    overrides = {r["rid"]: r for r in conn.execute(
        "SELECT * FROM vevents WHERE uid=? AND is_master=0", (uid,))}
    cancels = {r["rid"]: r for r in conn.execute(
        "SELECT * FROM cancelled_instances WHERE uid=?", (uid,))}
    exdates = {r["rid"] for r in conn.execute(
        "SELECT rid FROM exdates WHERE uid=?", (uid,))}
    replies = {r["rid"]: r for r in conn.execute(
        "SELECT * FROM replies WHERE uid=? AND email=?", (uid, email))}

    if not master["rrule"]:
        return [_render_single(master, overrides, cancels, replies)]

    return _render_recurring(
        master, overrides, cancels, exdates, replies,
        win_start, win_end, padded=padded)

def _instance_status(key, overrides, cancels, replies, tail_cancel=None):
    ov = overrides.get(key)
    inst_cancel = cancels.get(key)
    whole_cancel = cancels.get("")

    cancelled = whole_cancel is not None
    if whole_cancel is not None and ov is not None and (
        ov["seq"], ov["dtstamp"]
    ) > (whole_cancel["seq"], whole_cancel["dtstamp"]):
        # strictly newer per-instance request revives one instance
        cancelled = False

    if inst_cancel is not None:
        cancelled = ov is None or (ov["seq"], ov["dtstamp"]) <= (
            inst_cancel["seq"], inst_cancel["dtstamp"])

    # RANGE=THISANDFUTURE CANCEL flowing over the tail; a strictly newer
    # single-instance REQUEST revives just this occurrence (a later RANGE
    # override is already handled by the tail state machine).
    if not cancelled and tail_cancel is not None:
        if ov is None or ov["is_range"] or (ov["seq"], ov["dtstamp"]) <= (
            tail_cancel["seq"], tail_cancel["dtstamp"]
        ):
            cancelled = True

    if cancelled:
        return "cancelled"

    reply = replies.get(key) or replies.get("")
    if reply is not None:
        ps = (reply["partstat"] or "").upper()
        if ps == "ACCEPTED":
            return "accepted"
        if ps == "DECLINED":
            return "declined"
        if ps in ("TENTATIVE", "DELEGATED"):
            return ps.lower()
    return "needs-action"


def _render_single(master, overrides, cancels, replies):
    return {
        "uid": master["uid"],
        "recurrence_id": None,
        "start": master["start_utc"],
        "end": master["end_utc"],
        "status": _instance_status("", overrides, cancels, replies),
    }


def _render_recurring(master, overrides, cancels, exdates, replies,
                      win_start, win_end, padded=False):
    rr = rrulex.get_rrule(master["rrule"], master["start_utc"],
                          master["rrule_tzid"])
    master_duration = master["duration_s"]

    if padded:
        # A moved exception may sit outside its original slot on either side.
        exp_start, exp_end = win_start - WINDOW_PAD, win_end + WINDOW_PAD
    else:
        exp_start, exp_end = win_start, win_end
    base = rrulex.occurrences_between(rr, exp_start, exp_end)

    # Sorted THISANDFUTURE boundary table: index -> effective state.
    boundaries = _boundary_table(rr, overrides, cancels)

    # tail state: None = master cadence
    #   ("cancel", cancel_row) -> occurrences from this index onward cancelled
    #   ("shift", tail_rr, start_idx, duration_s)
    tail = None
    tail_cancel = None
    out = []
    for occ_utc in base:
        key = rrulex.to_utc_iso(occ_utc)
        idx = rrulex.occurrence_index(rr, occ_utc)
        if idx is None:
            continue
        if idx in boundaries:
            tail = boundaries[idx]
            tail_cancel = tail[1] if tail[0] == "cancel" else None

        single = overrides.get(key)
        if single is not None and not single["is_range"]:
            disp_start = rrulex.parse_utc(single["start_utc"])
            disp_end = rrulex.parse_utc(single["end_utc"])
        elif tail is not None and tail[0] == "cancel":
            disp_start, disp_end = occ_utc, occ_utc + dt.timedelta(
                seconds=master_duration)
        elif tail is not None and tail[0] == "shift":
            _, tail_rr, start_idx, duration_s = tail
            n = idx - start_idx
            try:
                disp_start = rrulex.occurrence_at(tail_rr, n)
            except IndexError:
                continue
            disp_end = disp_start + dt.timedelta(seconds=duration_s)
        else:
            if key in exdates:
                continue
            disp_start = occ_utc
            disp_end = occ_utc + dt.timedelta(seconds=master_duration)

        # Half-open intersection with the requested window.
        if disp_start >= win_end or disp_end <= win_start:
            continue

        out.append({
            "uid": master["uid"],
            "recurrence_id": key,
            "start": _iso(disp_start),
            "end": _iso(disp_end),
            "status": _instance_status(
                key, overrides, cancels, replies, tail_cancel),
        })
    return out


def _boundary_table(rr, overrides, cancels) -> dict:
    """Map master-rule index -> tail state at/after that index.

    Same-index RANGE CANCEL and RANGE REQUEST are adjudicated by
    (SEQUENCE, DTSTAMP); later boundaries in time replace earlier ones.
    """
    events = []  # (index, version_tuple, kind, row)
    for row in overrides.values():
        if not row["is_range"]:
            continue
        idx = rrulex.occurrence_index(rr, rrulex.parse_utc(row["rid"]))
        if idx is not None:
            events.append((idx, (row["seq"], row["dtstamp"]), "override", row))
    for row in cancels.values():
        if not row["rid"] or not row["is_range"]:
            continue
        idx = rrulex.occurrence_index(rr, rrulex.parse_utc(row["rid"]))
        if idx is not None:
            events.append((idx, (row["seq"], row["dtstamp"]), "cancel", row))

    # Walk chronologically; at each index the newest version wins, and a
    # boundary later in time replaces whatever tail was active.
    events.sort(key=lambda e: (e[0], e[1]))
    table: dict = {}
    for idx, _version, kind, row in events:
        if kind == "cancel":
            table[idx] = ("cancel", row)
        else:
            table[idx] = ("shift", _tail_rrule(rr, row, idx), idx,
                          row["duration_s"])
    return table


def _tail_rrule(master_rr, override_row, boundary_idx):
    """Continuing rrule seeded at the override's moved DTSTART.

    Per iTIP RANGE=THISANDFUTURE the tail repeats on the cadence of the
    override's DTSTART, so the master BYDAY set is *not* carried over (the
    moved DTSTART itself fixes the new anchor weekdays).  A master COUNT is
    reduced to the occurrences still remaining from the boundary; UNTIL
    stays intact.
    """
    tzid = override_seed_tzid(master_rr)
    new_start = rrulex.parse_utc(override_row["start_utc"]).astimezone(
        ZoneInfo(tzid))
    rule = getattr(master_rr, "_original_rule", {}) or {}
    wkst = "MO"
    if rule.get("wkst") is not None:
        inv = {getattr(v, "weekday", v): k for k, v in rrulex._WEEKDAYS.items()}
        wkst = inv[getattr(rule["wkst"], "weekday", rule["wkst"])]
    until = None
    if rule.get("until") is not None:
        until = rrulex._to_utc(rule["until"]).astimezone(ZoneInfo(tzid))
    count = getattr(master_rr, "_count", None)
    if count is not None:
        count = max(1, count - boundary_idx)
    return rrulex.build_rrule(
        new_start,
        freq="DAILY" if master_rr._freq == _rr.DAILY else "WEEKLY",
        interval=master_rr._interval,
        wkst=wkst, byday=None, until=until, count=count,
    )


def override_seed_tzid(master_rr) -> str:
    tz = master_rr._dtstart.tzinfo
    return str(tz) if tz is not None else "UTC"
