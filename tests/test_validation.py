"""422 validation: unknown zones, floating times, unsupported rules."""
from conftest import post, vevent


def test_unknown_tzid_422(client):
    r = post(client, "REQUEST", vevent(
        uid="x@example.com", dtstamp="20250301T120000Z",
        start_tz=("Mars/Olympus", "20250310T090000"),
        end_tz=("Mars/Olympus", "20250310T100000")))
    assert r.status_code == 422
    assert r.json()["error"] == "unprocessable_calendar"


def test_floating_time_422(client):
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\nUID:f@example.com\r\nDTSTAMP:20250301T120000Z\r\n"
        "DTSTART:20250310T090000\r\nDTEND:20250310T100000\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    r = client.post("/messages", content=ics)
    assert r.status_code == 422


def test_unsupported_freq_422(client):
    r = post(client, "REQUEST", vevent(
        uid="m@example.com", dtstamp="20250301T120000Z",
        start="20250310T090000Z", end="20250310T100000Z",
        rrule="FREQ=MONTHLY"))
    assert r.status_code == 422


def test_unsupported_rrule_part_422(client):
    r = post(client, "REQUEST", vevent(
        uid="m@example.com", dtstamp="20250301T120000Z",
        start="20250310T090000Z", end="20250310T100000Z",
        rrule="FREQ=DAILY;BYHOUR=9"))
    assert r.status_code == 422


def test_unsupported_range_value_422(client):
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:CANCEL\r\n"
        "BEGIN:VEVENT\r\nUID:m@example.com\r\nDTSTAMP:20250301T120000Z\r\n"
        "RECURRENCE-ID;RANGE=THISANDPRIOR:20250310T090000Z\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    r = client.post("/messages", content=ics)
    assert r.status_code == 422


def test_bad_calendar_422(client):
    r = client.post("/messages", content="not an ics at all")
    assert r.status_code == 422


def test_missing_method_422(client):
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:m@example.com\r\nDTSTAMP:20250301T120000Z\r\n"
        "DTSTART:20250310T090000Z\r\nDTEND:20250310T100000Z\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    assert client.post("/messages", content=ics).status_code == 422


def test_reply_without_partstat_422(client):
    r = post(client, "REPLY", vevent(
        uid="m@example.com", dtstamp="20250301T120000Z",
        attendees=[("alice@example.com", "")]))
    assert r.status_code == 422


def test_rid_on_non_recurring_422(client):
    post(client, "REQUEST", vevent(
        uid="one@example.com", dtstamp="20250301T120000Z",
        start="20250305T150000Z", end="20250305T160000Z",
        attendees=[("alice@example.com", "NEEDS-ACTION")]))
    r = post(client, "REPLY", vevent(
        uid="one@example.com", dtstamp="20250302T000000Z",
        rid=(None, "20250305T150000"),
        attendees=[("alice@example.com", "ACCEPTED")]))
    assert r.status_code == 422


def test_offset_window_normalised_to_utc(client):
    post(client, "REQUEST", vevent(
        uid="one@example.com", dtstamp="20250301T120000Z",
        start="20250305T150000Z", end="20250305T160000Z",
        attendees=[("alice@example.com", "NEEDS-ACTION")]))
    # +02:00 offsets are accepted and normalised to UTC; meeting 15:00Z falls
    # inside the equivalent window.
    r = client.get("/agenda", params={
        "attendee": "alice@example.com",
        "start": "2025-03-05T17:00:00+02:00",
        "end": "2025-03-10T02:00:00+02:00"})
    assert r.status_code == 200
    assert len(r.json()["events"]) == 1


def test_naive_window_422(client):
    r = client.get("/agenda", params={
        "attendee": "a@b.com",
        "start": "2025-03-03T00:00:00",
        "end": "2025-03-10T00:00:00"})
    assert r.status_code == 422


def test_all_day_event_accepted(client):
    r = post(client, "REQUEST", vevent(
        uid="day@example.com", dtstamp="20250301T120000Z",
        allday_start="20250306", allday_end="20250307",
        attendees=[("alice@example.com", "NEEDS-ACTION")]))
    assert r.status_code == 200
    data = client.get("/agenda", params={
        "attendee": "alice@example.com",
        "start": "2025-03-03T00:00:00Z", "end": "2025-03-10T00:00:00Z"}).json()
    e = data["events"][0]
    assert e["start"] == "2025-03-06T00:00:00+00:00"
    assert e["end"] == "2025-03-07T00:00:00+00:00"
