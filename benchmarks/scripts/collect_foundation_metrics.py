"""Record foundation-level engineering metrics as a reproducible JSON result.

Measures only what can be measured honestly at this stage of the project:

* number of collected pytest tests
* pytest wall-clock duration and pass/fail
* API Docker image size (if the image has been built with ``make docker-build``)

Every result carries the commit SHA, timestamp and runtime environment so the
number can be traced back to the exact code that produced it.

Usage (from the repository root, after ``make install``)::

    make metrics
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"
IMAGE = "contextledger-api:local"


def _run(cmd: list[str], cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def _git(*args: str) -> str | None:
    result = _run(["git", *args])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _pytest_metrics() -> dict[str, object]:
    collected = _run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=BACKEND)
    match = re.search(r"(\d+) tests? collected", collected.stdout)

    started = time.perf_counter()
    run = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=BACKEND)
    duration = time.perf_counter() - started

    return {
        "pytest_tests_collected": int(match.group(1)) if match else None,
        "pytest_passed": run.returncode == 0,
        "pytest_duration_seconds": round(duration, 3),
    }


def _docker_image_size() -> int | None:
    if shutil.which("docker") is None:
        return None
    result = _run(["docker", "image", "inspect", IMAGE, "--format", "{{.Size}}"])
    return int(result.stdout.strip()) if result.returncode == 0 else None


def main() -> int:
    now = datetime.now(UTC)
    image_bytes = _docker_image_size()
    result = {
        "benchmark": "foundation_metrics",
        "commit_sha": _git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(_git("status", "--porcelain")),
        "date": now.isoformat(timespec="seconds"),
        "dataset_size": None,
        "environment": "local",
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "configuration": {"docker_image": IMAGE},
        "metrics": {
            **_pytest_metrics(),
            "docker_image_size_bytes": image_bytes,
            "docker_image_size_mb": round(image_bytes / 1_000_000, 1) if image_bytes else None,
        },
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    sha = (result["commit_sha"] or "nocommit")[:8]
    out = RESULTS_DIR / f"foundation-{now:%Y%m%dT%H%M%SZ}-{sha}.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"\nWrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
