import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    monkeypatch.setenv("DATABASE_PATH", tmp.name)
    import app
    import store
    store = importlib.reload(store)
    app = importlib.reload(app)
    app.conn = store.connect(tmp.name)
    store.init_db(app.conn)
    with TestClient(app) as client:
        yield client
    app.conn.close()
    os.unlink(tmp.name)


def post_file(client, name):
    return client.post(
        "/messages",
        content=open(f"examples/{name}", "rb").read(),
        headers={"Content-Type": "text/calendar; charset=utf-8"},
    )


def test_request_reply_cancel_and_agenda(client):
    response = post_file(client, "request_daily.ics")
    assert response.status_code == 200
    assert response.json()["result"] == "applied"

    assert post_file(client, "request_daily.ics").json()["result"] == "idempotent"
    assert post_file(client, "reply_accept.ics").status_code == 200
    assert post_file(client, "cancel_instance.ics").status_code == 200

    agenda = client.get(
        "/agenda",
        params={
            "attendee": "ada@example.com",
            "start": "2026-10-05T00:00:00Z",
            "end": "2026-10-12T00:00:00Z",
        },
    )
    assert agenda.status_code == 200
    items = agenda.json()["items"]
    assert len(items) == 7
    assert items[0]["recurrence_id"] == "2026-10-05T13:00:00Z"
    assert items[0]["status"] == "accepted"
    assert items[2]["status"] == "cancelled"
    assert {i["status"] for i in items} == {"accepted", "cancelled"}


def test_old_reply_is_not_reused_after_sequence_change(client):
    post_file(client, "request_daily.ics")
    post_file(client, "reply_accept.ics")
    updated = open("examples/request_daily.ics", "rb").read().replace(
        b"SEQUENCE:0", b"SEQUENCE:1").replace(
        b"DTSTAMP:20261005T080000Z", b"DTSTAMP:20261005T090000Z")
    assert client.post("/messages", content=updated).status_code == 200
    items = client.get("/agenda", params={
        "attendee": "ada@example.com",
        "start": "2026-10-05T00:00:00Z",
        "end": "2026-10-12T00:00:00Z",
    }).json()["items"]
    assert items
    assert all(i["status"] == "pending" for i in items)


def test_unknown_timezone_is_422(client):
    bad = open("examples/request_daily.ics", "rb").read().replace(
        b"America/New_York", b"Not/AZone")
    response = client.post("/messages", content=bad)
    assert response.status_code == 422
