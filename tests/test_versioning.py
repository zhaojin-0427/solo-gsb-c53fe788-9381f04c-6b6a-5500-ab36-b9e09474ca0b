"""Versioning: SEQUENCE/DTSTAMP adjudication across updates and ranges."""
from conftest import post, get_agenda, vevent

NY = "America/New_York"
S = "2025-03-03T00:00:00Z"
E = "2025-04-08T00:00:00Z"


def _weekly(seq=0, stamp="20250301T120000Z"):
    return vevent(
        uid="v@example.com", dtstamp=stamp, seq=seq,
        start_tz=(NY, "20250310T090000"), end_tz=(NY, "20250310T093000"),
        rrule="FREQ=WEEKLY;BYDAY=MO;WKST=MO",
        attendees=[("alice@example.com", "NEEDS-ACTION")])


def test_new_sequence_wipes_overrides_cancels_and_replies(client):
    post(client, "REQUEST", _weekly(0))
    post(client, "REQUEST", vevent(
        uid="v@example.com", dtstamp="20250305T120000Z", seq=0,
        rid=(NY, "20250317T090000"),
        start_tz=(NY, "20250317T110000"), end_tz=(NY, "20250317T113000")))
    post(client, "CANCEL", vevent(
        uid="v@example.com", dtstamp="20250306T120000Z", seq=0,
        rid=(NY, "20250331T090000")))
    post(client, "REPLY", vevent(
        uid="v@example.com", dtstamp="20250302T000000Z", seq=0,
        attendees=[("alice@example.com", "ACCEPTED")]))
    # Brand-new SEQUENCE 1: fresh master, prior verdicts must not carry over.
    post(client, "REQUEST", _weekly(seq=1, stamp="20250310T120000Z"))
    data = get_agenda(client, "alice@example.com", S, E).json()
    by_rid = {e["recurrence_id"]: e for e in data["events"]}
    # override gone -> Mar17 back at its original 09:00 local slot
    assert by_rid["2025-03-17T13:00:00+00:00"]["start"] == \
        "2025-03-17T13:00:00+00:00"
    # cancel gone -> Mar31 alive again
    assert by_rid["2025-03-31T13:00:00+00:00"]["status"] == "needs-action"
    # old accepted reply gone -> needs-action everywhere
    assert all(e["status"] == "needs-action" for e in data["events"])


def test_later_range_request_revives_tail_after_range_cancel(client):
    post(client, "REQUEST", _weekly(0))
    # cancel tail from Mar17
    post(client, "CANCEL", vevent(
        uid="v@example.com", dtstamp="20250305T120000Z", seq=0,
        rid=(NY, "20250317T090000"), rid_range=True))
    data = get_agenda(client, "alice@example.com", S, E).json()
    assert all(e["status"] == "cancelled"
               for e in data["events"]
               if e["recurrence_id"] >= "2025-03-17")
    # strictly newer RANGE REQUEST from Mar24 re-establishes the tail
    post(client, "REQUEST", vevent(
        uid="v@example.com", dtstamp="20250320T120000Z", seq=1,
        rid=(NY, "20250324T090000"), rid_range=True,
        start_tz=(NY, "20250325T100000"), end_tz=(NY, "20250325T103000")))
    data = get_agenda(client, "alice@example.com", S, E).json()
    by_rid = {e["recurrence_id"]: e for e in data["events"]}
    # Mar17 (before the new boundary) stays cancelled by the older marker
    assert by_rid["2025-03-17T13:00:00+00:00"]["status"] == "cancelled"
    # tail revived on Tuesdays 14:00 UTC (10:00 EDT)
    assert by_rid["2025-03-24T13:00:00+00:00"]["status"] == "needs-action"
    assert by_rid["2025-03-24T13:00:00+00:00"]["start"] == \
        "2025-03-25T14:00:00+00:00"
    assert by_rid["2025-03-31T13:00:00+00:00"]["status"] == "needs-action"
    assert by_rid["2025-03-31T13:00:00+00:00"]["start"] == \
        "2025-04-01T14:00:00+00:00"


def test_stale_cancel_ignored_for_newer_override(client):
    post(client, "REQUEST", _weekly(0))
    # newer single override for Mar17 at seq1
    post(client, "REQUEST", vevent(
        uid="v@example.com", dtstamp="20250320T120000Z", seq=1,
        rid=(NY, "20250317T090000"),
        start_tz=(NY, "20250317T110000"), end_tz=(NY, "20250317T113000")))
    # late, stale cancel at seq0 cannot remove the seq1 instance
    r = post(client, "CANCEL", vevent(
        uid="v@example.com", dtstamp="20250321T120000Z", seq=0,
        rid=(NY, "20250317T090000")))
    assert r.json()["results"][0]["result"] == "ignored"
    data = get_agenda(client, "alice@example.com", S, E).json()
    by_rid = {e["recurrence_id"]: e for e in data["events"]}
    assert by_rid["2025-03-17T13:00:00+00:00"]["status"] == "needs-action"


def test_old_instance_reply_ignored_after_instance_reschedule(client):
    post(client, "REQUEST", _weekly(0))
    post(client, "REQUEST", vevent(
        uid="v@example.com", dtstamp="20250305T120000Z", seq=1,
        rid=(NY, "20250317T090000"),
        start_tz=(NY, "20250317T110000"), end_tz=(NY, "20250317T113000")))
    # reply stamped before the reschedule version cannot apply to it
    r = post(client, "REPLY", vevent(
        uid="v@example.com", dtstamp="20250304T000000Z", seq=1,
        rid=(NY, "20250317T090000"),
        attendees=[("alice@example.com", "ACCEPTED")]))
    assert r.json()["results"][0]["result"] == "ignored"
    data = get_agenda(client, "alice@example.com", S, E).json()
    by_rid = {e["recurrence_id"]: e for e in data["events"]}
    assert by_rid["2025-03-17T13:00:00+00:00"]["status"] == "needs-action"
