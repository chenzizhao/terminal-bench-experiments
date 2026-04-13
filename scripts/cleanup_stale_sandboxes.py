#!/usr/bin/env python3
"""
Delete stale and unhealthy Daytona sandboxes in the RL org.

Deletes:
  1. "started" sandboxes that have not had an event in over an hour (configurable).
  2. Sandboxes in terminal/unhealthy states: error, build_failed, stopped, unknown.

Uses the Daytona REST API directly (paginated endpoint).

Usage:
    # Dry run (default) — shows what would be deleted
    python cleanup_stale_sandboxes.py

    # Actually delete
    python cleanup_stale_sandboxes.py --delete

    # Custom threshold (minutes)
    python cleanup_stale_sandboxes.py --delete --threshold 120

Environment:
    DAYTONA_API_KEY     — API key for the target organization (default)
    DAYTONA_RL_API_KEY  — Fallback API key (or set in secrets.env)
"""

import argparse
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_BASE = "https://app.daytona.io/api"
PAGE_LIMIT = 200  # max allowed by the paginated endpoint
DEFAULT_THRESHOLD_MINUTES = 60
DEFAULT_WORKERS = 20
# Sandbox states that should be cleaned up unconditionally (no age check).
UNHEALTHY_STATES = ["error", "build_failed", "stopped", "unknown"]

# Try to load secrets.env from a few common locations
SECRET_ENV_PATH = os.environ.get("DC_AGENT_SECRET_ENV")
if SECRET_ENV_PATH and os.path.isfile(SECRET_ENV_PATH):
    load_dotenv(SECRET_ENV_PATH)
else:
    # Fallback: look next to this script or in ~/Documents
    for candidate in [
        os.path.join(os.path.dirname(__file__), "..", "..", "secrets.env"),
        os.path.expanduser("~/Documents/secrets.env"),
    ]:
        if os.path.isfile(candidate):
            load_dotenv(candidate)
            break


def get_api_key(env_var: str = "DAYTONA_API_KEY") -> str:
    key = os.environ.get(env_var)
    if not key:
        # Fallback chain: try common key names
        for fallback in ("DAYTONA_API_KEY", "DAYTONA_RL_API_KEY"):
            if fallback != env_var:
                key = os.environ.get(fallback)
                if key:
                    break
    if not key:
        sys.exit(
            f"ERROR: {env_var} (and fallbacks) not set in environment or secrets.env"
        )
    return key


def headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def list_sandboxes_by_states(api_key: str, states: list[str]) -> list[dict]:
    """Fetch all sandboxes matching the given states, sorted by updatedAt desc."""
    sandboxes: list[dict] = []
    page = 1
    while True:
        # The API expects repeated query params: ?states=x&states=y (multi format)
        params: list[tuple[str, str | int]] = [
            ("sort", "updatedAt"),
            ("order", "desc"),
            ("limit", PAGE_LIMIT),
            ("page", page),
        ]
        for s in states:
            params.append(("states", s))

        resp = requests.get(
            f"{API_BASE}/sandbox/paginated",
            headers=headers(api_key),
            params=params,
            timeout=30,
        )
        if resp.status_code == 400 and page > 1:
            # API may cap the maximum page number; stop with what we have.
            print(
                f"  Warning: API returned 400 on page {page}, stopping pagination ({len(sandboxes)} items collected)."
            )
            break
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        sandboxes.extend(items)
        total = data.get("total", 0)
        if len(sandboxes) >= total:
            break
        page += 1

    return sandboxes


def delete_sandbox(api_key: str, sandbox_id: str) -> bool:
    """Delete a single sandbox. Returns True on success."""
    resp = requests.delete(
        f"{API_BASE}/sandbox/{sandbox_id}",
        headers=headers(api_key),
        timeout=30,
    )
    return resp.status_code in (200, 204, 202)


def delete_sandboxes_parallel(
    api_key: str, sandboxes: list[dict], workers: int
) -> tuple[int, int]:
    """Delete sandboxes in parallel. Returns (success_count, fail_count)."""
    success = 0
    failed = 0
    total = len(sandboxes)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(delete_sandbox, api_key, sb["id"]): sb["id"] for sb in sandboxes
        }
        for i, future in enumerate(as_completed(futures), 1):
            if future.result():
                success += 1
            else:
                failed += 1
            if i % 500 == 0 or i == total:
                print(f"  Progress: {i}/{total}  (ok={success}, fail={failed})")

    return success, failed


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def find_stale_sandboxes(sandboxes: list[dict], threshold_minutes: int) -> list[dict]:
    """Return sandboxes whose updatedAt is older than threshold_minutes ago."""
    now = datetime.now(timezone.utc)
    stale = []

    for sb in sandboxes:
        updated_str = sb.get("updatedAt")
        if not updated_str:
            continue
        # Parse ISO-8601 timestamp (with or without trailing Z)
        updated_str = updated_str.replace("Z", "+00:00")
        updated_at = datetime.fromisoformat(updated_str)
        age_minutes = (now - updated_at).total_seconds() / 60.0
        if age_minutes > threshold_minutes:
            sb["_age_minutes"] = round(age_minutes, 1)
            stale.append(sb)

    return stale


def main():
    parser = argparse.ArgumentParser(description="Clean up stale Daytona RL sandboxes")
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete stale sandboxes (default is dry-run)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD_MINUTES,
        help=f"Minutes of inactivity before a sandbox is considered stale (default: {DEFAULT_THRESHOLD_MINUTES})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of parallel deletion threads (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--api-key-env",
        type=str,
        default="DAYTONA_API_KEY",
        help="Environment variable name containing the API key (default: DAYTONA_API_KEY)",
    )
    args = parser.parse_args()

    api_key = get_api_key(args.api_key_env)

    to_delete: list[dict] = []

    # 1. Stale "started" sandboxes
    print("Fetching all started sandboxes …")
    started = list_sandboxes_by_states(api_key, ["started"])
    print(f"  Found {len(started)} started sandboxes.")
    stale = find_stale_sandboxes(started, args.threshold)
    print(f"  {len(stale)} are stale (no event in >{args.threshold} min).")
    for sb in stale:
        sb["_reason"] = "stale"
    to_delete.extend(stale)

    # 2. Unhealthy sandboxes — query each state separately to avoid the
    #    API's per-query pagination cap (~5000 items).
    print(f"\nFetching unhealthy sandboxes ({', '.join(UNHEALTHY_STATES)}) …")
    for state in UNHEALTHY_STATES:
        sbs = list_sandboxes_by_states(api_key, [state])
        print(f"  {state}: {len(sbs)}")
        for sb in sbs:
            sb["_reason"] = state
        to_delete.extend(sbs)

    if not to_delete:
        print("\nNothing to clean up.")
        return

    # 3. Summary counts by reason
    counts = Counter(sb["_reason"] for sb in to_delete)
    print(f"\nTotal to delete: {len(to_delete)}")
    for reason, count in counts.most_common():
        print(f"  {reason}: {count}")

    if not args.delete:
        print(
            f"\nDry run — pass --delete to actually remove these {len(to_delete)} sandboxes."
        )
        return

    # 4. Delete in parallel
    total_success = 0
    total_failed = 0
    print(f"\nDeleting {len(to_delete)} sandboxes with {args.workers} workers …")
    ok, fail = delete_sandboxes_parallel(api_key, to_delete, args.workers)
    total_success += ok
    total_failed += fail

    # 5. Loop to drain unhealthy states beyond the pagination cap.
    #    Each fetch is capped at ~5000, so keep fetching+deleting until empty.
    for state in UNHEALTHY_STATES:
        while True:
            remaining = list_sandboxes_by_states(api_key, [state])
            if not remaining:
                break
            print(f"\n  {state}: {len(remaining)} more remaining — deleting …")
            ok, fail = delete_sandboxes_parallel(api_key, remaining, args.workers)
            total_success += ok
            total_failed += fail

    print(f"\nDone. Deleted {total_success} sandboxes, {total_failed} failures.")


if __name__ == "__main__":
    main()
