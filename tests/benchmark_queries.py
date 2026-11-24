"""Measure page latency and SQLite batch writes using disposable synthetic data."""

from concurrent.futures import ThreadPoolExecutor
from csv import reader
from datetime import datetime, timedelta
from io import StringIO
from statistics import median
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter

from fastapi.testclient import TestClient

from suunto.maintenance import storage_size
from suunto.store import Store
from suunto.sync import TZ
from suunto.web import create_app


def measure(years):
    with TemporaryDirectory() as directory:
        store = Store(directory)
        store.initialize()
        when = datetime.now(TZ) - timedelta(days=1)
        run = store.start("benchmark", when.isoformat())
        padding = "fixture " * 150
        workouts, routes = 365 * 6 * years, 1000 * years
        begin = perf_counter()
        store.commit(run, "workouts", [
            (f"w{i}", (when - timedelta(hours=4 * i)).isoformat(),
             {"workoutId": f"w{i}", "workoutName": f"Fixture {i}", "activityName": ["Running", "Cycling"][i % 2],
              "totalDistance": 5000 + i, "totalTimeSeconds": 1800, "description": padding,
              "summary": {"ascent": i % 1000}})
            for i in range(workouts)])
        store.commit(run, "routes", [
            (f"r{i:04}", (when - timedelta(hours=i)).isoformat(),
             {"routeId": f"r{i:04}", "modified": (when - timedelta(hours=i)).isoformat(),
              "description": f"Fixture route {i}", "activityNames": ["Running", "Cycling"],
              "totalDistance": i * 10, "payload_fixture": padding})
            for i in range(routes)])
        sleep = 365 * years
        recovery = sleep * 48
        store.commit(run, "sleep", [
            (f"s{i}", (when - timedelta(days=i)).isoformat(),
             {"entryData": {"SleepId": i, "Duration": 25200}})
            for i in range(sleep)])
        store.commit(run, "recovery", [
            (f"p{i}", (when - timedelta(minutes=30 * i)).isoformat(),
             {"entryData": {"Balance": .5, "StressState": 2, **{f"fixture{j}": j for j in range(6)}}})
            for i in range(recovery)])
        print(f"{years}y {workouts} activities / {routes} routes · SQLite {storage_size(directory)/1024**2:.1f} MiB"
              f" · fixture ingest {(perf_counter() - begin) * 1000:.1f} ms")
        startup = perf_counter()
        with TestClient(create_app(directory)) as browser:
            browser.get("/healthz").raise_for_status()
            print(f"cold app startup and health check {(perf_counter() - startup) * 1000:.1f} ms")
            urls = ("/", "/training", "/training?days=365", "/settings", "/settings?edit_preferences=true",
                    "/sleep-recovery", "/compare?sport=Running&first=w0&second=w2", "/routes", "/routes?q=route&sport=Cycling")
            for url in urls:
                browser.get(url).raise_for_status()
                samples = []
                for _ in range(7):
                    begin = perf_counter()
                    result = browser.get(url)
                    result.raise_for_status()
                    samples.append((perf_counter() - begin) * 1000)
                print(f"{url} · median {median(samples):.1f} ms · HTML {len(result.content)} bytes")
            concurrent_access(browser, store, when, workouts + routes + sleep + recovery)


def concurrent_access(browser, store, when, expected_rows):
    wrote, release = Event(), Event()

    def simulate_sync_transaction():
        with store.connect() as db:
            db.execute("INSERT INTO records VALUES (?, ?, ?, ?)", ("workouts", "pending", when.isoformat(), "{}"))
            wrote.set()
            if not release.wait(30):
                raise TimeoutError("The read workload did not finish while the writer was active")

    with ThreadPoolExecutor(max_workers=1) as workers:
        writer = workers.submit(simulate_sync_transaction)
        try:
            if not wrote.wait(10):
                raise TimeoutError("SQLite writer failed to start")
            begin = perf_counter()
            browser.get("/routes").raise_for_status()
            print(f"routes with active writer {(perf_counter() - begin) * 1000:.1f} ms")
            begin = perf_counter()
            exported = browser.get("/export.csv").raise_for_status()
            elapsed = perf_counter() - begin
            records = reader(StringIO(exported.content.decode("utf-8-sig")))
            assert next(records) == ["source", "record_key", "occurred_at", "payload"]
            keys = [row[1] for row in records]
            assert len(keys) == expected_rows and "pending" not in keys
            assert store.record("workouts", "pending") is None
            print(f"CSV with active writer · {expected_rows} rows · {len(exported.content)/1024**2:.1f} MiB · {elapsed * 1000:.1f} ms")
        finally:
            release.set()
        writer.result()
    assert store.record("workouts", "pending") == {}


if __name__ == "__main__":
    for years in (1, 3):
        measure(years)
