import importlib
import os
import tempfile
import time

from fastapi.testclient import TestClient


def test_agenda_performance_with_100k_history():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    os.environ["DATABASE_PATH"] = tmp.name
    import store
    store = importlib.reload(store)
    import app
    app = importlib.reload(app)
    db = store.connect(tmp.name)
    store.init_db(db)
    dtstamps = [f"2020-01-01T00:{i // 60:02d}:{i % 60:02d}Z" for i in range(60)]
    for i in range(100_000):
        # Direct historical notification insert: this benchmarks retrieval and
        # adjudication rather than ICS parsing throughput.
        db.execute(
            """INSERT INTO messages(uid,method,sequence,dtstamp,digest,result,content)
               VALUES(?,?,?,?,?,?,?)""",
            (f"history-{i}", "REQUEST", 0,
             dtstamps[i % len(dtstamps)], f"digest-{i}", "stored", ""))
    db.commit()
    db.close()

    with TestClient(app) as client:
        request = open("examples/request_daily.ics", "rb").read()
        client.post("/messages", content=request,
                    headers={"Content-Type": "text/calendar"})
        started = time.perf_counter()
        response = client.get("/agenda", params={
            "attendee": "ada@example.com",
            "start": "2026-10-05T00:00:00Z",
            "end": "2026-10-12T00:00:00Z",
        })
        elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert len(response.json()["items"]) == 7
    assert elapsed < 1.0
    os.unlink(tmp.name)
