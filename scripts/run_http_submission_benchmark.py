#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


TERMINAL_STATUSES = {
    "SUCCESS",
    "PASSED",
    "WRONG_ANSWER",
    "TLE",
    "MLE",
    "RUNTIME_ERROR",
    "COMPILATION_ERROR",
    "SYSTEM_ERROR",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Judge Vortex submission benchmark over HTTP.")
    parser.add_argument("--fixture", type=Path, required=True, help="Fixture JSON emitted by seed_benchmark_fixture.py.")
    parser.add_argument("--base-url", required=True, help="Judge Vortex base URL, for example https://judgevortex.duckdns.org.")
    parser.add_argument("--rounds", type=int, default=5, help="Submissions per user.")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Teacher poll interval in seconds.")
    parser.add_argument("--poll-timeout", type=float, default=1800.0, help="How long to wait for verdicts.")
    parser.add_argument("--output", type=Path, required=True, help="Where to write the benchmark JSON summary.")
    return parser


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def percentile(samples: list[float], pct: float) -> float | None:
    if not samples:
        return None
    if len(samples) == 1:
        return samples[0]
    ordered = sorted(samples)
    index = (len(ordered) - 1) * pct
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    lower_value = ordered[lower]
    upper_value = ordered[upper]
    return lower_value + (upper_value - lower_value) * (index - lower)


async def submit_for_user(
    client: httpx.AsyncClient,
    base_url: str,
    user_record: dict[str, Any],
    room_id: int,
    question_id: int,
    submission_payload: dict[str, Any],
    rounds: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    headers = {"Authorization": f"Token {user_record['token']}"}
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for round_index in range(rounds):
        payload = {
            **submission_payload,
            "room_id": room_id,
            "question_id": question_id,
        }
        started = time.perf_counter()
        try:
            response = await client.post(f"{base_url}/api/submissions/submit/", json=payload, headers=headers)
            latency_ms = (time.perf_counter() - started) * 1000
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {
                    "username": user_record["username"],
                    "round": round_index + 1,
                    "status_code": None,
                    "latency_ms": None,
                    "error": str(exc),
                }
            )
            continue

        if response.status_code == 201:
            body = response.json()
            successes.append(
                {
                    "submission_id": body["id"],
                    "user_id": user_record["user_id"],
                    "username": user_record["username"],
                    "round": round_index + 1,
                    "accepted_at_perf": time.perf_counter(),
                    "submit_latency_ms": latency_ms,
                }
            )
            continue

        failures.append(
            {
                "username": user_record["username"],
                "round": round_index + 1,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "error": response.text[:500],
            }
        )

    return successes, failures


async def poll_verdicts(
    client: httpx.AsyncClient,
    base_url: str,
    teacher_token: str,
    room_id: int,
    accepted_ids: set[int],
    poll_interval: float,
    poll_timeout: float,
) -> tuple[dict[int, dict[str, Any]], float, int]:
    headers = {"Authorization": f"Token {teacher_token}"}
    verdicts: dict[int, dict[str, Any]] = {}
    started = time.perf_counter()
    polls = 0

    while True:
        polls += 1
        response = await client.get(f"{base_url}/api/rooms/{room_id}/submissions/", headers=headers)
        response.raise_for_status()
        payload = response.json()
        seen_terminal = 0
        now_perf = time.perf_counter()

        for item in payload:
            submission_id = item["id"]
            if submission_id not in accepted_ids:
                continue
            if item["status"] in TERMINAL_STATUSES:
                verdicts.setdefault(submission_id, {**item, "final_seen_perf": now_perf})
                seen_terminal += 1

        if seen_terminal >= len(accepted_ids):
            return verdicts, time.perf_counter() - started, polls

        if time.perf_counter() - started > poll_timeout:
            return verdicts, time.perf_counter() - started, polls

        await asyncio.sleep(poll_interval)


async def main_async() -> int:
    args = build_parser().parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    base_url = args.base_url.rstrip("/")
    users = fixture["users"]
    room = fixture["room"]
    question = fixture["question"]
    submission_payload = fixture["submission_payload"]
    run_started = utc_now_iso()

    limits = httpx.Limits(max_keepalive_connections=2000, max_connections=4000)
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout, verify=True) as client:
        acceptance_started = time.perf_counter()
        results = await asyncio.gather(
            *[
                submit_for_user(
                    client=client,
                    base_url=base_url,
                    user_record=user_record,
                    room_id=room["id"],
                    question_id=question["id"],
                    submission_payload=submission_payload,
                    rounds=args.rounds,
                )
                for user_record in users
            ]
        )
        acceptance_seconds = time.perf_counter() - acceptance_started

        accepted: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for success_batch, failure_batch in results:
            accepted.extend(success_batch)
            failures.extend(failure_batch)

        accepted_ids = {item["submission_id"] for item in accepted}
        verdicts, verdict_seconds, poll_count = await poll_verdicts(
            client=client,
            base_url=base_url,
            teacher_token=fixture["teacher"]["token"],
            room_id=room["id"],
            accepted_ids=accepted_ids,
            poll_interval=args.poll_interval,
            poll_timeout=args.poll_timeout,
        )

    submit_latencies = [item["submit_latency_ms"] for item in accepted if item.get("submit_latency_ms") is not None]
    execution_times = [
        float(item["execution_time_ms"])
        for item in verdicts.values()
        if item.get("execution_time_ms") is not None
    ]

    accepted_by_id = {item["submission_id"]: item for item in accepted}
    approx_end_to_end_seconds = []
    for submission_id, verdict in verdicts.items():
        accepted_record = accepted_by_id.get(submission_id)
        if not accepted_record:
            continue
        approx_end_to_end_seconds.append(verdict["final_seen_perf"] - accepted_record["accepted_at_perf"])

    final_status_counts = Counter(item["status"] for item in verdicts.values())
    missing_verdict_ids = sorted(accepted_ids - verdicts.keys())
    total_targeted = len(users) * args.rounds

    summary = {
        "run_id": f"{fixture['run_prefix']}_users{len(users)}_subs{total_targeted}",
        "started_at": run_started,
        "finished_at": utc_now_iso(),
        "target_url": base_url,
        "candidates_targeted": len(users),
        "submissions_targeted": total_targeted,
        "accepted_submissions": len(accepted),
        "submission_failures": len(failures),
        "verdicts_observed": len(verdicts),
        "acceptance_wall_seconds": round(acceptance_seconds, 3),
        "verdict_wall_seconds": round(verdict_seconds, 3),
        "submission_ingest_rate_per_sec": round(len(accepted) / acceptance_seconds, 2) if acceptance_seconds > 0 else 0.0,
        "submit_latency_ms": {
            "avg": round(statistics.mean(submit_latencies), 2) if submit_latencies else None,
            "p95": round(percentile(submit_latencies, 0.95), 2) if submit_latencies else None,
            "p99": round(percentile(submit_latencies, 0.99), 2) if submit_latencies else None,
        },
        "execution_time_ms": {
            "avg": round(statistics.mean(execution_times), 2) if execution_times else None,
            "p95": round(percentile(execution_times, 0.95), 2) if execution_times else None,
            "p99": round(percentile(execution_times, 0.99), 2) if execution_times else None,
        },
        "approx_end_to_end_seconds": {
            "avg": round(statistics.mean(approx_end_to_end_seconds), 2) if approx_end_to_end_seconds else None,
            "p95": round(percentile(approx_end_to_end_seconds, 0.95), 2) if approx_end_to_end_seconds else None,
            "p99": round(percentile(approx_end_to_end_seconds, 0.99), 2) if approx_end_to_end_seconds else None,
        },
        "final_status_counts": dict(final_status_counts),
        "poll_count": poll_count,
        "poll_interval_seconds": args.poll_interval,
        "missing_verdict_ids": missing_verdict_ids[:50],
        "sample_failures": failures[:50],
        "topology": {
            "executor_services": 2,
            "kafka_partitions": 8,
            "sandbox_slots": 32,
            "supported_languages": 11,
        },
        "notes": (
            "Live HTTP benchmark against the deployed Judge Vortex stack. "
            f"Used {len(users)} pre-seeded student users with {args.rounds} final submissions each "
            f"for a total target of {total_targeted} submissions."
        ),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
