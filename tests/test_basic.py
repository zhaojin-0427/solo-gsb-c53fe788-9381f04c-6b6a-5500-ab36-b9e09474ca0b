"""Basics: single meetings, replay idempotency, replies, statuses, cancels."""
from conftest import post, get_agenda, vevent


def _single(seq=0, dtstamp="20250301T120000Z", attendee="alice@example.com"):
    return vevent(
        uid="one@example.com", dtstamp=dtstamp, seq=seq,
        start="20250305T150000Z", end="20250305T160000Z",
        attendees=[(attendee, "NEEDS-ACTION")],
    )


def test_single_request_default_needs_action(client):
    r = post(client, "REQUEST", _single())
    assert r.status_code == 200
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert len(data["events"]) == 1
    e = data["events"][0]
    assert e["uid"] == "one@example.com"
    assert e["recurrence_id"] is None
    assert e["status"] == "needs-action"
    assert e["start"] == "2025-03-05T15:00:00+00:00"


def test_replay_is_idempotent(client):
    body = _single()
    r1 = post(client, "REQUEST", body)
    r2 = post(client, "REQUEST", body)
    assert r1.json()["results"][0]["result"] == "stored"
    assert r2.json()["results"][0]["result"] == "duplicate"
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert len(data["events"]) == 1


def test_accepted_then_declined_reply(client):
    post(client, "REQUEST", _single())

    def reply(partstat, stamp):
        return vevent(uid="one@example.com", dtstamp=stamp, seq=0,
                      attendees=[("alice@example.com", partstat)])

    post(client, "REPLY", reply("ACCEPTED", "20250302T080000Z"))
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["status"] == "accepted"

    post(client, "REPLY", reply("DECLINED", "20250303T080000Z"))
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["status"] == "declined"


def test_old_reply_never_applies(client):
    post(client, "REQUEST", _single(seq=1))
    # reply referencing the obsolete SEQUENCE 0
    old = vevent(uid="one@example.com", dtstamp="20250201T000000Z", seq=0,
                 attendees=[("alice@example.com", "ACCEPTED")])
    r = post(client, "REPLY", old)
    assert r.json()["results"][0]["result"] == "ignored"
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["status"] == "needs-action"


def test_old_reply_does_not_carry_to_new_sequence(client):
    post(client, "REQUEST", _single(seq=0))
    post(client, "REPLY", vevent(
        uid="one@example.com", dtstamp="20250302T000000Z", seq=0,
        attendees=[("alice@example.com", "ACCEPTED")]))
    # New version wipes the old verdict ("旧版回复不沿用").
    post(client, "REQUEST", _single(seq=1, dtstamp="20250304T120000Z"))
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["status"] == "needs-action"


def test_cancel_whole_meeting(client):
    post(client, "REQUEST", _single())
    cancel = vevent(uid="one@example.com", dtstamp="20250304T000000Z", seq=0)
    post(client, "CANCEL", cancel)
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["status"] == "cancelled"


def test_cancel_replay_duplicate(client):
    post(client, "REQUEST", _single())
    cancel = vevent(uid="one@example.com", dtstamp="20250304T000000Z", seq=0)
    r1 = post(client, "CANCEL", cancel)
    r2 = post(client, "CANCEL", cancel)
    assert r1.json()["results"][0]["result"] == "stored"
    assert r2.json()["results"][0]["result"] == "duplicate"


def test_newer_request_revives_cancelled_meeting(client):
    post(client, "REQUEST", _single(seq=0))
    post(client, "CANCEL",
         vevent(uid="one@example.com", dtstamp="20250304T000000Z", seq=0))
    post(client, "REQUEST", _single(seq=1, dtstamp="20250305T000000Z"))
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["status"] == "needs-action"


def test_same_version_conflict_409(client):
    post(client, "REQUEST", _single())
    # same SEQUENCE AND same DTSTAMP but materially different time -> the
    # two copies of the same version disagree -> 409.
    changed = vevent(
        uid="one@example.com", dtstamp="20250301T120000Z", seq=0,
        start="20250305T170000Z", end="20250305T180000Z",
        attendees=[("alice@example.com", "NEEDS-ACTION")])
    r = post(client, "REQUEST", changed)
    assert r.status_code == 409
    assert r.json()["error"] == "version_conflict"


def test_same_sequence_later_dtstamp_is_refresh(client):
    post(client, "REQUEST", _single())
    # same SEQUENCE, strictly later DTSTAMP, different time: adjudicated by
    # DTSTAMP as a content refresh, not a conflict.
    changed = vevent(
        uid="one@example.com", dtstamp="20250301T130000Z", seq=0,
        start="20250305T170000Z", end="20250305T180000Z",
        attendees=[("alice@example.com", "NEEDS-ACTION")])
    r = post(client, "REQUEST", changed)
    assert r.status_code == 200
    assert r.json()["results"][0]["result"] == "stored"
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["start"] == "2025-03-05T17:00:00+00:00"


def test_half_open_window_boundaries(client):
    post(client, "REQUEST", _single())
    # end equals the start of the meeting -> excluded
    data = get_agenda(client, "alice@example.com",
                      "2025-02-01T00:00:00Z", "2025-03-05T15:00:00Z").json()
    assert data["events"] == []
    # start equals the end of the meeting -> excluded
    data = get_agenda(client, "alice@example.com",
                      "2025-03-05T16:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"] == []


def test_attendee_isolation_and_case_insensitive(client):
    post(client, "REQUEST", vevent(
        uid="one@example.com", dtstamp="20250301T120000Z",
        start="20250305T150000Z", end="20250305T160000Z",
        attendees=[("Alice@Example.com", "NEEDS-ACTION"),
                   ("bob@example.com", "NEEDS-ACTION")]))
    post(client, "REPLY", vevent(
        uid="one@example.com", dtstamp="20250302T000000Z",
        attendees=[("ALICE@example.com", "DECLINED")]))
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["status"] == "declined"
    data = get_agenda(client, "bob@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["status"] == "needs-action"
    data = get_agenda(client, "carol@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"] == []
