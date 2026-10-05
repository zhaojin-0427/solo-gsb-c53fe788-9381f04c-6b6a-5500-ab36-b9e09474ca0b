"""Out-of-order delivery: CANCEL/REPLY before REQUEST is held then adjudicated."""
from conftest import post, get_agenda, vevent


def test_reply_before_request_is_staged_then_applied(client):
    reply = vevent(
        uid="late@example.com", dtstamp="20250302T080000Z",
        attendees=[("alice@example.com", "ACCEPTED")])
    r = post(client, "REPLY", reply)
    assert r.json()["results"][0]["result"] == "staged"
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"] == []

    request = vevent(
        uid="late@example.com", dtstamp="20250301T120000Z",
        start="20250305T150000Z", end="20250305T160000Z",
        attendees=[("alice@example.com", "NEEDS-ACTION")])
    post(client, "REQUEST", request)
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["status"] == "accepted"


def test_cancel_before_request_is_staged_then_applied(client):
    cancel = vevent(uid="late2@example.com", dtstamp="20250304T000000Z")
    post(client, "CANCEL", cancel)
    post(client, "REQUEST", vevent(
        uid="late2@example.com", dtstamp="20250301T120000Z",
        start="20250305T150000Z", end="20250305T160000Z",
        attendees=[("alice@example.com", "NEEDS-ACTION")]))
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["status"] == "cancelled"


def test_override_before_master_is_staged_then_applied(client):
    NY = "America/New_York"
    post(client, "REQUEST", vevent(
        uid="s@example.com", dtstamp="20250305T120000Z",
        rid=(NY, "20250317T090000"),
        start_tz=(NY, "20250317T110000"), end_tz=(NY, "20250317T120000")))
    post(client, "REQUEST", vevent(
        uid="s@example.com", dtstamp="20250301T120000Z",
        start_tz=(NY, "20250310T090000"), end_tz=(NY, "20250310T100000"),
        rrule="FREQ=WEEKLY;BYDAY=MO",
        attendees=[("alice@example.com", "NEEDS-ACTION")]))
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-04-01T00:00:00Z").json()
    moved = [e for e in data["events"]
             if e["recurrence_id"] == "2025-03-17T13:00:00+00:00"]
    assert len(moved) == 1
    assert moved[0]["start"] == "2025-03-17T15:00:00+00:00"


def test_stale_staged_reply_dropped_when_request_newer(client):
    # Staged reply is seq 0; the request that arrives is already seq 1.
    post(client, "REPLY", vevent(
        uid="late3@example.com", dtstamp="20250201T000000Z", seq=0,
        attendees=[("alice@example.com", "ACCEPTED")]))
    post(client, "REQUEST", vevent(
        uid="late3@example.com", dtstamp="20250304T120000Z", seq=1,
        start="20250305T150000Z", end="20250305T160000Z",
        attendees=[("alice@example.com", "NEEDS-ACTION")]))
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["status"] == "needs-action"


def test_cancel_then_reviving_request_arrives_later(client):
    # CANCEL staged first; then a REQUEST with a higher SEQUENCE revives.
    post(client, "CANCEL",
         vevent(uid="r@example.com", dtstamp="20250304T000000Z", seq=0))
    post(client, "REQUEST", vevent(
        uid="r@example.com", dtstamp="20250305T000000Z", seq=1,
        start="20250305T150000Z", end="20250305T160000Z",
        attendees=[("alice@example.com", "NEEDS-ACTION")]))
    data = get_agenda(client, "alice@example.com",
                      "2025-03-03T00:00:00Z", "2025-03-10T00:00:00Z").json()
    assert data["events"][0]["status"] == "needs-action"
