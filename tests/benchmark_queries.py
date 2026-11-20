"""Measure page latency and SQLite batch writes using disposable synthetic data."""

from datetime import datetime, timedelta
from statistics import median
from tempfile import TemporaryDirectory
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
        print(f"{years}y {workouts} activities / {routes} routes · SQLite {storage_size(directory)/1024**2:.1f} MiB"
              f" · fixture ingest {(perf_counter() - begin) * 1000:.1f} ms")
        with TestClient(create_app(directory)) as browser:
            for url in ("/compare?sport=Running&first=w0&second=w2", "/routes", "/routes?q=route&sport=Cycling"):
                browser.get(url).raise_for_status()
                samples = []
                for _ in range(7):
                    begin = perf_counter()
                    result = browser.get(url)
                    result.raise_for_status()
                    samples.append((perf_counter() - begin) * 1000)
                print(f"{url} · median {median(samples):.1f} ms · HTML {len(result.content)} bytes")


if __name__ == "__main__":
    for years in (1, 3):
        measure(years)
