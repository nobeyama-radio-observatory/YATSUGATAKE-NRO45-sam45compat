# SPDX-License-Identifier: BSD-3-Clause
"""version（動いているソフトの版）の試験。"""

import os
import subprocess
import tempfile
import unittest

from sam45compat import version as ver


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class TestVersion(unittest.TestCase):
    def test_read_this_repo(self):
        d = ver.read()
        self.assertTrue(d["sam45compat"])
        self.assertIn("YATSUGATAKE", ver.short(d))

    def test_tag_and_dirty(self):
        with tempfile.TemporaryDirectory() as root:
            _git(root, "init", "-q")
            _git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "a")
            _git(root, "tag", "-a", "v9.9.9", "-m", "試験")
            with open(os.path.join(root, "f"), "w") as f:
                f.write("x")
            _git(root, "add", "f")
            d = ver.read(root)
            self.assertEqual((d["sam45compat"], d["dirty"]), ("v9.9.9-dirty", True))
            self.assertEqual(d["yatsugatake"], "unknown")             # submodule が無い
            self.assertFalse(d["yatsugatake_mismatch"])

    def test_not_git(self):
        with tempfile.TemporaryDirectory() as root:
            d = ver.read(root)
            self.assertNotEqual(d["sam45compat"], "")
            self.assertIsNone(d["commit"])

    def test_append_log(self):
        sw = {"sam45compat": "v0.1.0", "yatsugatake": "2g-2/v1.0.0", "dirty": False}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.tsv")
            ver.append_log(p, "2026-11-12T10:00:00+0900", "20261112100000", "psw",
                           ver.observation(sw, {"bit": "sam45-fine", "usr": "abc1234", "version": "3"}, "fine"))
            ver.append_log(p, "2026-11-12T11:00:00+0900", "20261112110000", "otf", ver.observation(sw, None, "wide"))
            with open(p) as f:
                rows = [x.rstrip("\n").split("\t") for x in f]
        self.assertEqual(rows[0], list(ver.LOG_COLUMNS))
        self.assertEqual(rows[1], ["2026-11-12T10:00:00+0900", "20261112100000", "psw", "v0.1.0", "2g-2/v1.0.0",
                                   "sam45-fine", "abc1234", "3", ""])
        self.assertEqual(rows[2][5:8], ["wide", "-", "-"])


if __name__ == "__main__":
    unittest.main()
