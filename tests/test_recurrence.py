"""Recurrence: daily/weekly RRULE, IANA TZID, EXDATE, overrides, RANGE."""
from conftest import post, get_agenda, vevent

NY = "America/New_York"
S = "2025-03-03T00:00:00Z"
E = "2025-04-01T00:00:00Z"


def _weekly(**overrides):
    kw = dict(
        uid="standup@example.com", dtstamp="20250301T120000Z",
        start_tz=(NY, "20250310T090000"), end_tz=(NY, "20250310T100000"),
        rrule="FREQ=WEEKLY;BYDAY=MO;WKST=MO",
        attendees=[("alice@example.com", "NEEDS-ACTION")],
    )
    kw.update(overrides)
    return vevent(**kw)


def _keys(events):
    return [(e["recurrence_id"], e["start"][11:16], e["status"])
            for e in events]


def test_weekly_rrule_iana_tzid(client):
    # 09:00 America/New_York on 2025-03-10 is EDT (UTC-4) -> 13:00 UTC.
    post(client, "REQUEST", _weekly())
    data = get_agenda(client, "alice@example.com", S, E).json()
    ids = [e["recurrence_id"] for e in data["events"]]
    assert ids[0] == "2025-03-10T13:00:00+00:00"
    assert data["events"][0]["start"] == "2025-03-10T13:00:00+00:00"
    # Mondays in March 2025: 3,10,17,24,31 -> five in window (3 is before
    # DTSTART, so not generated): 10,17,24,31
    assert ids == [
        "2025-03-10T13:00:00+00:00",
        "2025-03-17T13:00:00+00:00",
        "2025-03-24T13:00:00+00:00",
        "2025-03-31T13:00:00+00:00",
    ]


def test_daily_rrule(client):
    post(client, "REQUEST", vevent(
        uid="daily@example.com", dtstamp="20250301T120000Z",
        start="20250310T010000Z", end="20250310T013000Z",
        rrule="FREQ=DAILY;INTERVAL=2;COUNT=4",
        attendees=[("alice@example.com", "NEEDS-ACTION")]))
    data = get_agenda(client, "alice@example.com", S, E).json()
    assert [e["recurrence_id"] for e in data["events"]] == [
        "2025-03-10T01:00:00+00:00",
        "2025-03-12T01:00:00+00:00",
        "2025-03-14T01:00:00+00:00",
        "2025-03-16T01:00:00+00:00",
    ]


def test_exdate_hole_is_skipped(client):
    post(client, "REQUEST", _weekly(exdates=[(NY, "20250317T090000")]))
    data = get_agenda(client, "alice@example.com", S, E).json()
    ids = [e["recurrence_id"] for e in data["events"]]
    assert "2025-03-17T13:00:00+00:00" not in ids
    assert "2025-03-10T13:00:00+00:00" in ids
    assert "2025-03-24T13:00:00+00:00" in ids


def test_single_override_moves_one_instance_keeps_identity(client):
    post(client, "REQUEST", _weekly())
    # Move only the Mar 17 instance two hours later (11:00 local = 15:00 UTC).
    post(client, "REQUEST", vevent(
        uid="standup@example.com", dtstamp="20250305T120000Z",
        rid=(NY, "20250317T090000"),
        start_tz=(NY, "20250317T110000"), end_tz=(NY, "20250317T120000")))
    data = get_agenda(client, "alice@example.com", S, E).json()
    moved = [e for e in data["events"]
             if e["recurrence_id"] == "2025-03-17T13:00:00+00:00"]
    assert len(moved) == 1
    assert moved[0]["start"] == "2025-03-17T15:00:00+00:00"
    assert moved[0]["end"] == "2025-03-17T16:00:00+00:00"
    # other instances untouched
    assert any(e["recurrence_id"] == "2025-03-10T13:00:00+00:00" and
               e["start"] == "2025-03-10T13:00:00+00:00"
               for e in data["events"])


def test_single_override_takes_priority_over_exdate(client):
    # EXDATE removes the slot but an explicit override re-creates it.
    post(client, "REQUEST", _weekly(exdates=[(NY, "20250317T090000")]))
    post(client, "REQUEST", vevent(
        uid="standup@example.com", dtstamp="20250305T120000Z",
        rid=(NY, "20250317T090000"),
        start_tz=(NY, "20250317T110000"), end_tz=(NY, "20250317T120000")))
    data = get_agenda(client, "alice@example.com", S, E).json()
    moved = [e for e in data["events"]
             if e["recurrence_id"] == "2025-03-17T13:00:00+00:00"]
    assert len(moved) == 1
    assert moved[0]["start"] == "2025-03-17T15:00:00+00:00"


def test_rid_snaps_back_to_earlier_occurrence(client):
    # Override whose RECURRENCE-ID sits in a gap before a later slot snaps
    # back to the nearest earlier valid occurrence.
    post(client, "REQUEST", _weekly(exdates=[(NY, "20250317T090000")]))
    post(client, "REQUEST", vevent(
        uid="standup@example.com", dtstamp="20250306T120000Z",
        rid=(NY, "20250318T090000"),  # Tuesday, between Mar17(exdate) and Mar24
        start_tz=(NY, "20250318T110000"), end_tz=(NY, "20250318T120000")))
    data = get_agenda(client, "alice@example.com", S, E).json()
    moved = [e for e in data["events"]
             if e["start"] == "2025-03-18T15:00:00+00:00"]
    assert len(moved) == 1
    # identity snaps back to Mar 10? No: nearest valid *at or before* the
    # given RID time that is still an occurrence — Mar 10 is before Mar 17.
    # But since Mar17 is EXDATE, the closest earlier *valid* is Mar 10...
    # the request semantics: "回拨取较早一次" => Mar 10.
    assert moved[0]["recurrence_id"] == "2025-03-10T13:00:00+00:00"


def test_instance_reply_with_moved_rid_reference(client):
    # A reply referencing an already-moved instance by its moved start time
    # binds to the original occurrence identity.
    post(client, "REQUEST", _weekly())
    post(client, "REQUEST", vevent(
        uid="standup@example.com", dtstamp="20250305T120000Z",
        rid=(NY, "20250317T090000"),
        start_tz=(NY, "20250317T110000"), end_tz=(NY, "20250317T120000")))
    post(client, "REPLY", vevent(
        uid="standup@example.com", dtstamp="20250306T000000Z",
        rid=(NY, "20250317T110000"),
        attendees=[("alice@example.com", "ACCEPTED")]))
    data = get_agenda(client, "alice@example.com", S, E).json()
    moved = [e for e in data["events"]
             if e["recurrence_id"] == "2025-03-17T13:00:00+00:00"][0]
    assert moved["status"] == "accepted"
    # reply applies to the one instance only
    other = [e for e in data["events"]
             if e["recurrence_id"] == "2025-03-10T13:00:00+00:00"][0]
    assert other["status"] == "needs-action"


def test_range_thisandfuture_request_shifts_tail(client):
    post(client, "REQUEST", _weekly())
    # From Mar 17 onward move everything to Wednesdays 12:00 local.
    post(client, "REQUEST", vevent(
        uid="standup@example.com", dtstamp="20250305T120000Z", seq=1,
        rid=(NY, "20250317T090000"), rid_range=True,
        start_tz=(NY, "20250319T120000"), end_tz=(NY, "20250319T130000")))
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-04-08T00:00:00Z").json()
    by_rid = {e["recurrence_id"]: e for e in data["events"]}
    # head keeps original slot & identity
    assert by_rid["2025-03-10T13:00:00+00:00"]["start"] == \
        "2025-03-10T13:00:00+00:00"
    # tail keeps original RECURRENCE-IDs but displays on Wednesdays 16:00 UTC
    assert by_rid["2025-03-17T13:00:00+00:00"]["start"] == \
        "2025-03-19T16:00:00+00:00"
    assert by_rid["2025-03-24T13:00:00+00:00"]["start"] == \
        "2025-03-26T16:00:00+00:00"
    assert by_rid["2025-03-31T13:00:00+00:00"]["start"] == \
        "2025-04-02T16:00:00+00:00"


def test_range_thisandfuture_cancel_removes_tail(client):
    post(client, "REQUEST", _weekly())
    post(client, "CANCEL", vevent(
        uid="standup@example.com", dtstamp="20250305T120000Z",
        rid=(NY, "20250317T090000"), rid_range=True))
    data = get_agenda(client, "alice@example.com", S, E).json()
    status = {e["recurrence_id"]: e["status"] for e in data["events"]}
    assert status["2025-03-10T13:00:00+00:00"] == "needs-action"
    assert status["2025-03-17T13:00:00+00:00"] == "cancelled"
    assert status["2025-03-24T13:00:00+00:00"] == "cancelled"
    assert status["2025-03-31T13:00:00+00:00"] == "cancelled"


def test_single_override_revives_one_tail_cancelled_instance(client):
    post(client, "REQUEST", _weekly())
    post(client, "CANCEL", vevent(
        uid="standup@example.com", dtstamp="20250305T120000Z",
        rid=(NY, "20250317T090000"), rid_range=True))
    # A strictly newer single-instance REQUEST for Mar 24 revives just it.
    post(client, "REQUEST", vevent(
        uid="standup@example.com", dtstamp="20250308T120000Z", seq=1,
        rid=(NY, "20250324T090000"),
        start_tz=(NY, "20250324T110000"), end_tz=(NY, "20250324T120000")))
    data = get_agenda(client, "alice@example.com", S, E).json()
    by_rid = {e["recurrence_id"]: e for e in data["events"]}
    assert by_rid["2025-03-24T13:00:00+00:00"]["status"] == "needs-action"
    assert by_rid["2025-03-24T13:00:00+00:00"]["start"] == \
        "2025-03-24T15:00:00+00:00"
    assert by_rid["2025-03-31T13:00:00+00:00"]["status"] == "cancelled"


def test_instance_cancel_single(client):
    post(client, "REQUEST", _weekly())
    post(client, "CANCEL", vevent(
        uid="standup@example.com", dtstamp="20250305T120000Z",
        rid=(NY, "20250317T090000")))
    data = get_agenda(client, "alice@example.com", S, E).json()
    status = {e["recurrence_id"]: e["status"] for e in data["events"]}
    assert status["2025-03-17T13:00:00+00:00"] == "cancelled"
    assert status["2025-03-10T13:00:00+00:00"] == "needs-action"
    assert status["2025-03-24T13:00:00+00:00"] == "needs-action"


def test_series_wide_reply_then_instance_reply(client):
    post(client, "REQUEST", _weekly())
    post(client, "REPLY", vevent(
        uid="standup@example.com", dtstamp="20250302T000000Z",
        attendees=[("alice@example.com", "ACCEPTED")]))
    data = get_agenda(client, "alice@example.com", S, E).json()
    assert all(e["status"] == "accepted" for e in data["events"])
    # decline just one
    post(client, "REPLY", vevent(
        uid="standup@example.com", dtstamp="20250303T000000Z",
        rid=(NY, "20250317T090000"),
        attendees=[("alice@example.com", "DECLINED")]))
    data = get_agenda(client, "alice@example.com", S, E).json()
    status = {e["recurrence_id"]: e["status"] for e in data["events"]}
    assert status["2025-03-17T13:00:00+00:00"] == "declined"
    assert status["2025-03-10T13:00:00+00:00"] == "accepted"
