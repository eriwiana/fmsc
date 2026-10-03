"""Open N sockets on one auction, push bids through it, and report what the watchers saw.

M5 criterion 6. Not part of the suite: 500 sockets on a shared CI runner measures the
runner, and a gate that flakes gets switched off. Run it by hand, against whichever
fan-out backend is being judged, and put the numbers in the pull request.

    uv run python scripts/load_sockets.py --sockets 500 --bids 5
    uv run python scripts/load_sockets.py --sockets 500 --channels-url redis://localhost:6379/0

The server runs as its own process. In-process, the client's 500 sockets and the server
would share one event loop and the latencies would be a measure of the test harness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time

import httpx
import websockets

PASSWORD = "load-test-password"


async def _wait_for_health(base: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(base_url=base) as http:
        while time.monotonic() < deadline:
            try:
                if (await http.get("/health")).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError("server did not become healthy")


async def _signup(http: httpx.AsyncClient, email: str) -> str:
    response = await http.post("/auth/signup", json={"email": email, "password": PASSWORD})
    response.raise_for_status()
    return response.json()["token"]


def _connections(database: str) -> int:
    """Backends on the application's database, to see whether sockets cost connections."""
    result = subprocess.run(
        [
            "psql",
            database,
            "-Atq",
            "-c",
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return int(result.stdout.strip()) if result.stdout.strip().isdigit() else -1


async def _watch(url: str, expected: int, ready: asyncio.Event, seen: list) -> None:
    """One watcher: read the snapshot, then record when each event arrives."""
    async with websockets.connect(url, open_timeout=30, ping_interval=None) as socket:
        snapshot = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
        assert snapshot["type"] == "snapshot", snapshot
        ready.set()
        for _ in range(expected):
            raw = await asyncio.wait_for(socket.recv(), timeout=60)
            event = json.loads(raw)
            seen.append((event["seq"], time.monotonic()))


async def run(args: argparse.Namespace) -> int:
    database = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/fmsc"
    )
    psql_dsn = database.replace("postgresql+asyncpg://", "postgresql://", 1)
    env = dict(os.environ, DATABASE_URL=database)
    if args.channels_url:
        env["CHANNELS_URL"] = args.channels_url

    base = f"http://127.0.0.1:{args.port}"
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(args.port),
            "--log-level",
            "warning",
        ],
        env=env,
    )
    try:
        await _wait_for_health(base)
        idle_connections = _connections(psql_dsn)

        async with httpx.AsyncClient(base_url=base, timeout=60) as http:
            stamp = int(time.time())
            seller = await _signup(http, f"seller-{stamp}@load")
            bidder = await _signup(http, f"bidder-{stamp}@load")
            watcher = await _signup(http, f"watcher-{stamp}@load")
            created = await http.post(
                "/auctions",
                headers={"Authorization": f"Bearer {seller}"},
                json={
                    "title": "load",
                    "starting_bid": "1.00",
                    "ends_at": "2030-01-01T00:00:00+00:00",
                },
            )
            created.raise_for_status()
            auction_id = created.json()["id"]

            # One ticket per socket: they are single-use by design.
            tickets = []
            for _ in range(args.sockets):
                response = await http.post(
                    "/ws/tickets", headers={"Authorization": f"Bearer {watcher}"}
                )
                response.raise_for_status()
                tickets.append(response.json()["ticket"])

            seen: list[list] = [[] for _ in range(args.sockets)]
            readies = [asyncio.Event() for _ in range(args.sockets)]
            watchers = [
                asyncio.create_task(
                    _watch(
                        f"ws://127.0.0.1:{args.port}/ws/auctions/{auction_id}?ticket={ticket}",
                        args.bids,
                        readies[i],
                        seen[i],
                    )
                )
                for i, ticket in enumerate(tickets)
            ]
            await asyncio.wait_for(asyncio.gather(*(r.wait() for r in readies)), timeout=120)
            connected = _connections(psql_dsn)

            # Both stamps. The headline is end to end — bid sent, watcher saw it — because
            # that is the delay a pair of users actually experiences. The second exists
            # because measuring from the response alone produces negative numbers: the
            # handler publishes before it serialises the reply, so watchers are served
            # before the bidder is, and a metric that hides that is worse than no metric.
            published, answered = [], []
            for n in range(args.bids):
                published.append(time.monotonic())
                response = await http.post(
                    f"/auctions/{auction_id}/bids",
                    headers={"Authorization": f"Bearer {bidder}"},
                    json={"amount": f"{10 + n}.00"},
                )
                response.raise_for_status()
                answered.append(time.monotonic())
                await asyncio.sleep(args.gap)

            done, pending = await asyncio.wait(watchers, timeout=120)
            for task in pending:
                task.cancel()
            # A task in `done` may have raised, and an unretrieved exception would be
            # reported as a watcher that finished — in the one number this script exists
            # to produce.
            failures = [task.exception() for task in done if task.exception() is not None]

        pairs = [
            (seq, arrived)
            for events in seen
            for seq, arrived in events
            if 0 < seq <= len(published)
        ]
        latencies = [(arrived - published[seq - 1]) * 1000 for seq, arrived in pairs]
        after_reply = [(arrived - answered[seq - 1]) * 1000 for seq, arrived in pairs]
        delivered = sum(len(events) for events in seen)
        expected = args.sockets * args.bids
        print()
        print(f"backend                  {args.channels_url or 'memory (in-process)'}")
        print(f"sockets                  {args.sockets}")
        print(f"bids published           {args.bids}")
        print(f"events expected          {expected}")
        print(f"events delivered         {delivered}  ({100 * delivered / expected:.1f}%)")
        print(f"watchers that finished   {len(done) - len(failures)} of {args.sockets}")
        print(f"watchers that failed     {len(failures)}")
        for failure in failures[:3]:
            print(f"  {type(failure).__name__}: {failure}")
        print(f"db connections idle      {idle_connections}")
        print(f"db connections, sockets  {connected}")
        if latencies:
            latencies.sort()
            print(f"bid sent -> seen, p50    {statistics.median(latencies):.0f} ms")
            print(f"bid sent -> seen, p95    {latencies[int(len(latencies) * 0.95)]:.0f} ms")
            print(f"bid sent -> seen, max    {latencies[-1]:.0f} ms")
            # Negative means the watcher was served first, which is the normal case here.
            print(f"relative to the bidder   {statistics.median(after_reply):+.0f} ms")
        return 0 if delivered == expected and not failures else 1
    finally:
        server.terminate()
        server.wait(timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sockets", type=int, default=500)
    parser.add_argument("--bids", type=int, default=5)
    parser.add_argument("--gap", type=float, default=1.0, help="seconds between bids")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--channels-url", default=None)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
