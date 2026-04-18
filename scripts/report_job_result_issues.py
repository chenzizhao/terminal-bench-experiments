import argparse
import csv
import json
import re
from pathlib import Path


DEFAULT_EXCLUDED_ERRORS = (
    "VerifierTimeoutError",
    "AgentTimeoutError",
    "RewardFileNotFoundError",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan job-level result.json files and report jobs that have mismatched "
            "trial counts or non-excluded exception types."
        )
    )
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=Path("jobs"),
        help="Jobs directory that contains <job_name>/result.json files.",
    )
    parser.add_argument(
        "--configs-root",
        type=Path,
        default=Path("outputs/adapter_experiments/batch1/contributors/Zoe"),
        help="Root path to search for corresponding config YAMLs.",
    )
    parser.add_argument(
        "--excluded-errors",
        nargs="+",
        default=list(DEFAULT_EXCLUDED_ERRORS),
        help=(
            "Exception types to ignore. Comparison is case-insensitive and ignores "
            "non-alphanumeric characters."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("rerun_candidates.csv"),
        help="Output CSV path for flagged jobs.",
    )
    return parser.parse_args()


def normalize_error_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def find_exception_stats(stats: dict) -> dict[str, list[str]]:
    combined: dict[str, list[str]] = {}

    root_exception_stats = stats.get("exception_stats")
    if isinstance(root_exception_stats, dict):
        for error_name, trials in root_exception_stats.items():
            combined.setdefault(str(error_name), []).extend(
                trials if isinstance(trials, list) else []
            )

    evals = stats.get("evals")
    if isinstance(evals, dict):
        for eval_data in evals.values():
            if not isinstance(eval_data, dict):
                continue
            exception_stats = eval_data.get("exception_stats")
            if not isinstance(exception_stats, dict):
                continue
            for error_name, trials in exception_stats.items():
                combined.setdefault(str(error_name), []).extend(
                    trials if isinstance(trials, list) else []
                )

    return combined


def build_config_map(configs_root: Path) -> dict[str, Path]:
    config_map: dict[str, Path] = {}
    if not configs_root.is_dir():
        return config_map

    for path in configs_root.rglob("*.yaml"):
        config_map.setdefault(path.stem, path)
    return config_map


def build_phase_config_map(configs_root: Path) -> dict[tuple[str, str], Path]:
    phase_config_map: dict[tuple[str, str], Path] = {}
    if not configs_root.is_dir():
        return phase_config_map

    for path in configs_root.rglob("*.yaml"):
        parts = path.relative_to(configs_root).parts
        phase = next((part for part in parts if re.fullmatch(r"phase\d+", part)), "")
        if phase:
            phase_config_map.setdefault((phase, path.stem), path)

    return phase_config_map


def resolve_config_path(
    job_name: str,
    configs_root: Path,
    config_map: dict[str, Path],
    phase_config_map: dict[tuple[str, str], Path],
) -> Path | None:
    parts = job_name.split("__")
    if len(parts) >= 5 and re.fullmatch(r"phase\d+", parts[-1]):
        phase = parts[-1]
        stem = "__".join(parts[:-2])
        by_phase = phase_config_map.get((phase, stem))
        if by_phase is not None:
            return by_phase

        candidate = configs_root / phase / f"{stem}.yaml"
        if candidate.is_file():
            return candidate

        fallback = config_map.get(stem)
        if fallback is not None:
            return fallback

    return config_map.get(job_name)


def inspect_result_file(
    result_path: Path,
    excluded_normalized: set[str],
    configs_root: Path,
    config_map: dict[str, Path],
    phase_config_map: dict[tuple[str, str], Path],
) -> dict | None:
    try:
        payload = json.loads(result_path.read_text())
    except Exception:
        return None

    n_total_trials = payload.get("n_total_trials")
    stats = payload.get("stats") or {}
    n_trials = stats.get("n_trials")
    if not isinstance(n_total_trials, int) or not isinstance(n_trials, int):
        return None

    mismatch = n_total_trials > n_trials
    exception_stats = find_exception_stats(stats)
    non_excluded = sorted(
        error_name
        for error_name in exception_stats
        if normalize_error_name(error_name) not in excluded_normalized
    )
    has_non_excluded_errors = bool(non_excluded)

    if not mismatch and not has_non_excluded_errors:
        return None

    job_name = result_path.parent.name
    config_path = resolve_config_path(
        job_name=job_name,
        configs_root=configs_root,
        config_map=config_map,
        phase_config_map=phase_config_map,
    )
    non_excluded_count = sum(
        len(exception_stats[error_name]) for error_name in non_excluded
    )

    return {
        "job_name": job_name,
        "result_path": result_path,
        "config_path": config_path,
        "n_total_trials": n_total_trials,
        "stats_n_trials": n_trials,
        "count_mismatch": mismatch,
        "non_excluded_errors": non_excluded,
        "non_excluded_exception_count": non_excluded_count,
    }


def iter_job_result_paths(jobs_dir: Path) -> list[Path]:
    if not jobs_dir.is_dir():
        return []
    return sorted(
        path for path in jobs_dir.glob("*/result.json") if path.is_file()
    )


def main() -> None:
    args = parse_args()
    excluded_normalized = {normalize_error_name(e) for e in args.excluded_errors}
    config_map = build_config_map(args.configs_root)
    phase_config_map = build_phase_config_map(args.configs_root)

    findings: list[dict] = []
    for result_path in iter_job_result_paths(args.jobs_dir):
        finding = inspect_result_file(
            result_path=result_path,
            excluded_normalized=excluded_normalized,
            configs_root=args.configs_root,
            config_map=config_map,
            phase_config_map=phase_config_map,
        )
        if finding is not None:
            findings.append(finding)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            [
                "job_name",
                "count_mismatch",
                "n_total_trials",
                "stats_n_trials",
                "non_excluded_exception_count",
                "non_excluded_errors",
                "result_path",
                "zoe_config_path",
            ]
        )
        for finding in findings:
            writer.writerow(
                [
                    finding["job_name"],
                    int(finding["count_mismatch"]),
                    finding["n_total_trials"],
                    finding["stats_n_trials"],
                    finding["non_excluded_exception_count"],
                    ",".join(finding["non_excluded_errors"]),
                    str(finding["result_path"]),
                    str(finding["config_path"] or ""),
                ]
            )

    print(f"Wrote {args.output} with {len(findings)} flagged jobs.")


if __name__ == "__main__":
    main()
