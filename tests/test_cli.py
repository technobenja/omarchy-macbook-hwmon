from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from hwmon import __main__ as cli
from hwmon import snapshot


class CliStalenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-cli-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.state_dir = self._tmp / "state"
        self.db_path = self._tmp / "hwmon.db"

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        full_argv = ["--state-dir", str(self.state_dir), "--db", str(self.db_path), *argv]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(full_argv)
        return code, out.getvalue(), err.getvalue()

    def test_missing_snapshot_exits_nonzero_with_message(self) -> None:
        code, out, err = self._run([])
        self.assertNotEqual(code, 0)
        self.assertIn("no snapshot", err)

    def test_fresh_snapshot_json_round_trips(self) -> None:
        snap = dict(snapshot._REFERENCE_SNAPSHOT)
        snap["ts"] = time.time()
        snapshot.write_atomic(self.state_dir / "latest.json", snap)
        code, out, err = self._run(["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["schema"], 2)

    def test_stale_snapshot_exits_nonzero_with_message(self) -> None:
        snap = dict(snapshot._REFERENCE_SNAPSHOT)
        snap["ts"] = time.time() - 60  # 60s old, way past the 5s threshold
        snapshot.write_atomic(self.state_dir / "latest.json", snap)
        code, out, err = self._run(["--json"])
        self.assertNotEqual(code, 0)
        self.assertIn("stale", err)

    def test_fresh_snapshot_human_output_does_not_crash(self) -> None:
        snap = dict(snapshot._REFERENCE_SNAPSHOT)
        snap["ts"] = time.time()
        snapshot.write_atomic(self.state_dir / "latest.json", snap)
        code, out, err = self._run([])
        self.assertEqual(code, 0)
        self.assertIn("Battery", out)
        self.assertIn("Fan", out)


class CliHistoryPeaksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="hwmon-cli-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.state_dir = self._tmp / "state"
        self.db_path = self._tmp / "hwmon.db"

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        full_argv = ["--state-dir", str(self.state_dir), "--db", str(self.db_path), *argv]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(full_argv)
        return code, out.getvalue(), err.getvalue()

    def test_peaks_on_empty_db(self) -> None:
        code, out, err = self._run(["peaks"])
        self.assertEqual(code, 0)
        self.assertIn("cpu_package_c", out)

    def test_history_unknown_metric_errors(self) -> None:
        code, out, err = self._run(["history", "not_a_metric"])
        self.assertNotEqual(code, 0)
        self.assertIn("unknown metric", err)


if __name__ == "__main__":
    unittest.main()
