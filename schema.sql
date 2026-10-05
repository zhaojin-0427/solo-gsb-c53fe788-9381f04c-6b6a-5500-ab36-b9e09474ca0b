PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    uid TEXT NOT NULL,
    method TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    dtstamp TEXT NOT NULL,
    digest TEXT NOT NULL,
    result TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_uid ON messages(uid, sequence, dtstamp);
CREATE INDEX IF NOT EXISTS idx_messages_attendee_lookup ON messages(received_at);

CREATE TABLE IF NOT EXISTS request_versions (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    dtstamp TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(uid, sequence, dtstamp, digest)
);

CREATE TABLE IF NOT EXISTS reply_records (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    dtstamp TEXT NOT NULL,
    recurrence_id TEXT,
    attendee TEXT NOT NULL,
    partstat TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(uid, sequence, dtstamp, recurrence_id, attendee, digest)
);
CREATE INDEX IF NOT EXISTS idx_replies_lookup
    ON reply_records(attendee, uid, sequence, dtstamp, recurrence_id, partstat);

CREATE TABLE IF NOT EXISTS cancel_records (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    dtstamp TEXT NOT NULL,
    recurrence_id TEXT,
    range_value TEXT,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(uid, sequence, dtstamp, recurrence_id, range_value, digest)
);
CREATE INDEX IF NOT EXISTS idx_cancels_lookup
    ON cancel_records(uid, sequence, dtstamp, recurrence_id, range_value);

CREATE TABLE IF NOT EXISTS component_rule_json (
    uid TEXT NOT NULL,
    recurrence_id TEXT NOT NULL DEFAULT '',
    rule TEXT,
    PRIMARY KEY (uid, recurrence_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS current_components (
    uid TEXT NOT NULL,
    recurrence_id TEXT NOT NULL DEFAULT '',
    range_value TEXT,
    tzid TEXT NOT NULL,
    wall_start TEXT NOT NULL,
    actual_start_utc TEXT NOT NULL,
    actual_end_utc TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    anchor_day INTEGER NOT NULL,
    anchor_weekday INTEGER NOT NULL,
    freq TEXT,
    interval INTEGER NOT NULL DEFAULT 1,
    weekdays_mask INTEGER NOT NULL DEFAULT 0,
    wkst INTEGER NOT NULL DEFAULT 0,
    rrule_count INTEGER,
    until_utc TEXT,
    last_utc TEXT,
    byday TEXT NOT NULL DEFAULT '[]',
    exdates TEXT NOT NULL DEFAULT '[]',
    summary TEXT,
    status TEXT NOT NULL DEFAULT 'CONFIRMED',
    digest TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    dtstamp TEXT NOT NULL,
    PRIMARY KEY (uid, recurrence_id)
);
CREATE INDEX IF NOT EXISTS idx_components_window
    ON current_components(actual_start_utc, last_utc);

CREATE TABLE IF NOT EXISTS current_component_attendees (
    uid TEXT NOT NULL,
    recurrence_id TEXT NOT NULL,
    attendee TEXT NOT NULL,
    PRIMARY KEY (uid, recurrence_id, attendee)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_component_attendees_attendee
    ON current_component_attendees(attendee, uid, recurrence_id);
