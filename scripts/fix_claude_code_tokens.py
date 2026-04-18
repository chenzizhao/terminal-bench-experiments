"""
Retroactively populate token counts for claude-code trials that are missing them.

Claude Code's session JSONL files contain valid usage data, but populate_context_post_run
was not called during the original trial execution. This script:
1. Finds all claude-code trials with null token counts
2. Calls populate_context_post_run to parse session JSONL and generate trajectory.json
3. Updates result.json with the extracted token counts

Usage:
    uv run python scripts/fix_claude_code_tokens.py [--jobs-dir jobs] [--dry-run]
"""

import argparse
import json
from pathlib import Path

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.models.agent.context import AgentContext


def fix_trial(trial_dir: Path, dry_run: bool = False) -> dict | None:
    """Fix token counts for a single trial. Returns stats dict or None if skipped."""
    result_path = trial_dir / "result.json"
    if not result_path.exists():
        return None

    result_data = json.loads(result_path.read_text())
    agent_result = result_data.get("agent_result")
    if agent_result is None:
        return None  # Agent never ran (e.g. environment setup failure)

    # Skip if tokens are already populated
    if agent_result.get("n_input_tokens") is not None:
        return None

    agent_dir = trial_dir / "agent"
    sessions_dir = agent_dir / "sessions"
    if not sessions_dir.exists():
        return {"trial": trial_dir.name, "status": "no_sessions_dir"}

    # Extract agent config
    config = result_data.get("config", {})
    agent_config = config.get("agent", {})
    model_name = agent_config.get("model_name", "unknown")

    # Create agent and run populate_context_post_run
    try:
        agent = ClaudeCode(
            logs_dir=agent_dir,
            model_name=model_name,
            version=agent_config.get("kwargs", {}).get("version", "unknown"),
        )

        context = AgentContext()
        agent.populate_context_post_run(context)

        if context.n_input_tokens is None:
            return {"trial": trial_dir.name, "status": "no_metrics_extracted"}

        stats = {
            "trial": trial_dir.name,
            "status": "fixed",
            "n_input_tokens": context.n_input_tokens,
            "n_output_tokens": context.n_output_tokens,
            "n_cache_tokens": context.n_cache_tokens,
            "cost_usd": context.cost_usd,
        }

        if not dry_run:
            # Update result.json
            result_data["agent_result"]["n_input_tokens"] = context.n_input_tokens
            result_data["agent_result"]["n_output_tokens"] = context.n_output_tokens
            result_data["agent_result"]["n_cache_tokens"] = context.n_cache_tokens
            result_data["agent_result"]["cost_usd"] = context.cost_usd
            result_path.write_text(json.dumps(result_data, indent=4))

        return stats

    except Exception as e:
        return {"trial": trial_dir.name, "status": "error", "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-dir", type=Path, default=Path("jobs"))
    parser.add_argument("--dry-run", action="store_true", help="Don't modify files")
    parser.add_argument("--job-pattern", default="*__claude-code__*",
                        help="Glob pattern for job directories")
    args = parser.parse_args()

    job_dirs = sorted(args.jobs_dir.glob(args.job_pattern))
    print(f"Found {len(job_dirs)} matching job directories")

    total_fixed = 0
    total_skipped = 0
    total_errors = 0
    total_no_sessions = 0

    for job_dir in job_dirs:
        if not job_dir.is_dir():
            continue

        trial_dirs = sorted(
            d for d in job_dir.iterdir()
            if d.is_dir() and (d / "result.json").exists()
        )

        job_fixed = 0
        for trial_dir in trial_dirs:
            result = fix_trial(trial_dir, dry_run=args.dry_run)
            if result is None:
                total_skipped += 1
                continue

            status = result["status"]
            if status == "fixed":
                job_fixed += 1
                total_fixed += 1
            elif status == "no_sessions_dir":
                total_no_sessions += 1
            elif status == "error":
                total_errors += 1
                print(f"  ERROR {result['trial']}: {result.get('error')}")
            elif status == "no_metrics_extracted":
                total_errors += 1
                print(f"  NO METRICS {result['trial']}")

        if job_fixed > 0:
            action = "Would fix" if args.dry_run else "Fixed"
            print(f"  {action} {job_fixed}/{len(trial_dirs)} trials in {job_dir.name}")

    print(f"\nSummary:")
    print(f"  Fixed: {total_fixed}")
    print(f"  Skipped (already has tokens or no agent_result): {total_skipped}")
    print(f"  No sessions dir: {total_no_sessions}")
    print(f"  Errors: {total_errors}")
    if args.dry_run:
        print("\n  (dry-run mode — no files were modified)")


if __name__ == "__main__":
    main()
