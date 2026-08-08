"""Run every suite and report which ones failed.

Each suite is a plain script that asserts its way through a feature and prints what it checked,
rather than a pytest module. They run in separate processes on purpose: several of them install
fake `pymongo`, `discord` and `Database` modules into sys.modules, and sharing an interpreter
would let one suite's fakes leak into the next.

    python tests/run_all.py            every suite
    python tests/run_all.py logging    only suites whose name contains "logging"
    python tests/run_all.py -v         show each suite's own output as well
"""

import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent

# Every cog prints a ✓ when it loads. A captured subprocess on Windows gets the console
# codepage rather than UTF-8, so without this the child dies on its own success message.
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def main() -> int:
    args = [a for a in sys.argv[1:]]
    verbose = "-v" in args or "--verbose" in args
    patterns = [a for a in args if not a.startswith("-")]

    suites = sorted(HERE.glob("test_*.py"))
    if patterns:
        suites = [s for s in suites if any(p in s.name for p in patterns)]
    if not suites:
        print("No suites matched.")
        return 1

    failed = []
    started = time.time()
    for suite in suites:
        result = subprocess.run([sys.executable, str(suite)],
                                capture_output=True, text=True, env=CHILD_ENV,
                                encoding="utf-8", errors="replace")
        ok = result.returncode == 0
        print(f"{'PASS' if ok else 'FAIL'}  {suite.name}")
        if verbose and result.stdout:
            print("".join(f"      {line}\n" for line in result.stdout.splitlines()))
        if not ok:
            failed.append(suite.name)
            # The traceback is the useful part, and it goes to stderr.
            tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
            print("".join(f"      {line}\n" for line in tail))

    elapsed = time.time() - started
    print(f"\n{len(suites) - len(failed)}/{len(suites)} passed in {elapsed:.1f}s")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
