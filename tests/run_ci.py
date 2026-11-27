"""Run the full unittest suite and surface failures in GitHub check annotations."""

import unittest


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner().run(suite)
    for test, traceback in result.failures + result.errors:
        last_line = traceback.rstrip().splitlines()[-1]
        message = f"{test.id()} — {last_line[:500]}".replace("%", "%25").replace("\r", "%0D")
        print(f"::error::{message}")
    raise SystemExit(not result.wasSuccessful())
