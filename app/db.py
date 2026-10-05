"""SQLite storage layer.

Time storage convention
-----------------------
Every absolute instant is stored as ``YYYY-MM-DDTHH:MM:SS+00:00`` (UTC,
lexicographically sortable).  Recurrence identity keys (RECURRENCE-ID) are the
UTC instant of the *original* master occurrence.  For all-day DATE values the
UTC midnight of the floating date is used.

A UID's master VEVENT and per-instance exceptions live in the same ``vevents``
table, distinguished by ``is_master``.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row per VEVENT (master or override instance).
CREATE TABLE IF NOT EXISTS vevents (
    uid          TEXT NOT NULL,
    is_master    INTEGER NOT NULL,          -- 1 master, 0 override
    rid          TEXT NOT NULL,             -- '' for master, else original occurrence key (UTC)
    seq          INTEGER NOT NULL,
    dtstamp      TEXT NOT NULL,             -- UTC
    start_utc    TEXT NOT NULL,             -- master: DTSTART UTC; override: (possibly moved) DTSTART UTC
    end_utc      TEXT NOT NULL,
    duration_s   INTEGER NOT NULL,
    rrule        TEXT,                      -- JSON for master recurring events, else NULL
    rrule_tzid   TEXT,                      -- DTSTART IANA zone used to build the rrule
    rrule_until  TEXT,                      -- UTC UNTIL or NULL
    rrule_count  INTEGER,                   -- COUNT or NULL
    rrule_last   TEXT,                      -- last occurrence UTC when cheaply known, else NULL
    is_range     INTEGER NOT NULL DEFAULT 0,-- override with RANGE=THISANDFUTURE
    is_all_day   INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (uid, rid)
);

-- EXDATE values of the master (original occurrence keys, UTC midnight for all-day).
CREATE TABLE IF NOT EXISTS exdates (
    uid      TEXT NOT NULL,
    rid      TEXT NOT NULL,
    PRIMARY KEY (uid, rid)
);

-- CANCEL verdicts: '' rid = whole UID, otherwise one original occurrence.
CREATE TABLE IF NOT EXISTS cancelled_instances (
    uid          TEXT NOT NULL,
    rid          TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    dtstamp      TEXT NOT NULL,
    is_range     INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (uid, rid)
);

-- Attendee membership is taken from master REQUEST VEVENTs.
CREATE TABLE IF NOT EXISTS attendees (
    uid   TEXT NOT NULL,
    email TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (uid, email)
);

-- Latest per-attendee, per-instance REPLY verdict.  rid='' means whole series.
CREATE TABLE IF NOT EXISTS replies (
    uid          TEXT NOT NULL,
    rid          TEXT NOT NULL,             -- resolved to the *original* occurrence key
    email        TEXT NOT NULL COLLATE NOCASE,
    partstat     TEXT NOT NULL,             -- ACCEPTED | DECLINED | TENTATIVE ...
    seq          INTEGER NOT NULL,
    dtstamp      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (uid, rid, email)
);

CREATE INDEX IF NOT EXISTS idx_attendees_email ON attendees(email);
-- Single meetings intersecting a window.
CREATE INDEX IF NOT EXISTS idx_vevents_singles
    ON vevents(is_master, start_utc, end_utc) WHERE is_master = 1 AND rrule IS NULL;
-- Recurring masters whose first occurrence starts before the window end.
CREATE INDEX IF NOT EXISTS idx_vevents_series_first
    ON vevents(is_master, start_utc, rrule_last) WHERE is_master = 1 AND rrule IS NOT NULL;
-- Overrides whose (possibly moved) start falls in a padded window.
CREATE INDEX IF NOT EXISTS idx_vevents_override_start
    ON vevents(is_master, start_utc) WHERE is_master = 0;
CREATE INDEX IF NOT EXISTS idx_vevents_override_rid
    ON vevents(uid, rid) WHERE is_master = 0;
CREATE INDEX IF NOT EXISTS idx_exdates ON exdates(uid);
CREATE INDEX IF NOT EXISTS idx_cancelled ON cancelled_instances(uid, rid);
CREATE INDEX IF NOT EXISTS idx_replies_lookup ON replies(email, uid, rid);
CREATE INDEX IF NOT EXISTS idx_replies_uid ON replies(uid);

-- Messages held for adjudication ("REPLY/CANCEL arrived before the REQUEST").
CREATE TABLE IF NOT EXISTS staging (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- CANCEL | REPLY
    content     BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_staging_uid ON staging(uid, id);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA mmap_size = 268435456")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(path: str | None = None) -> None:
    target = path or config.DB_PATH
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
