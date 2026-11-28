"""Try two handoffs from the checkout. Requires only Python 3.11+."""

import json
import subprocess
import sys
from pathlib import Path


def main():
    if sys.version_info < (3, 11):
        sys.exit("This example needs Python 3.11 or newer. Check python3 --version.")

    for target in ("Notes", "Mail"):
        result = subprocess.run(
            [sys.executable, "-m", "systemone.cli", "use",
             "--target", target, "--request", "count the windows"],
            cwd=Path(__file__).resolve().parents[1] / "src",
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            plan = json.loads(result.stdout)
            print(f"{target}: {plan['mode']}, started={json.dumps(plan['started'])}")
        else:
            print(f"{target}: {result.stderr.strip()}")

        print(f"exit {result.returncode}")
        if result.returncode != (0 if target == "Notes" else 2):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
