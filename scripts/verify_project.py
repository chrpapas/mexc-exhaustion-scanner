from __future__ import annotations

import compileall
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HEADERS = {
    "render.yaml": "services:",
    "pyproject.toml": "[build-system]",
    ".gitignore": ".env",
    ".python-version": "3.12.11",
    "app/worker.py": "from __future__ import annotations",
    "migrations/001_init.sql": "CREATE TABLE IF NOT EXISTS contracts (",
}


def main() -> int:
    problems: list[str] = []
    for relative, expected in EXPECTED_HEADERS.items():
        path = ROOT / relative
        if not path.is_file():
            problems.append(f"Missing file: {relative}")
            continue
        first_nonempty = next((line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()), "")
        if first_nonempty != expected:
            problems.append(f"Wrong content in {relative}: expected first line {expected!r}, got {first_nonempty!r}")

    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    if project.get("project", {}).get("name") != "mexc-exhaustion-scanner":
        problems.append("pyproject.toml has the wrong project name")

    if not compileall.compile_dir(ROOT / "app", quiet=1):
        problems.append("Python compilation failed")

    if problems:
        print("PROJECT VERIFICATION FAILED")
        for problem in problems:
            print(f"- {problem}")
        return 1

    print("PROJECT VERIFICATION PASSED")
    for relative in EXPECTED_HEADERS:
        print(f"- {relative}: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
