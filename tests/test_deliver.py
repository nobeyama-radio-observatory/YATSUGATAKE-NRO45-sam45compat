# SPDX-License-Identifier: BSD-3-Clause
"""deliver（観測の後: マージして、配信の置き場所に移す）の試験。"""

import errno
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from sam45compat import deliver as dv
from sam45compat import lc as lcm
from sam45compat.fake import FakeSpectrometer
from sam45compat.notify import Notifier
from test_lc import _write
from test_mapping import _obs
from test_yksp import _sam45_records

NAME = "YKSP.obs1.alice.proj1.20261110120000"

# マージの代わり: {outdir} に <ログ ID>.raw を書く
FAKE_MERGE = [sys.executable, "-c",
              "import sys, os; open(os.path.join(sys.argv[2], sys.argv[3] + '.raw'), 'w').write(sys.argv[1])",
              "{logging}", "{outdir}", "{log_id}"]
FAIL_MERGE = [sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"]


class TestDeliver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = os.path.join(self.tmp.name, "bklog")
        self.root = os.path.join(self.tmp.name, "download")
        os.makedirs(self.work)
        self.log = os.path.join(self.work, NAME)
        with open(self.log, "wb") as f:
            f.write(b"x" * 100)

    def tearDown(self):
        self.tmp.cleanup()

    def test_kind_of(self):
        self.assertEqual(dv.kind_of("/cosmos3/45m/bklog/sam45psw"), "psw")
        self.assertEqual(dv.kind_of("/cosmos3/45m/bklog/sam45otf/"), "otf")
        self.assertIsNone(dv.kind_of("/path/to/logging"))

    def test_logging_only_without_merge(self):
        d = dv.Deliverer(self.root, None, Notifier(), background=False)
        d.submit(dv.Job(self.log, "psw"))
        dst = os.path.join(self.root, "alice", "proj1", "logging", NAME)
        self.assertEqual(d.done, [dst])
        self.assertFalse(os.path.exists(self.log))
        self.assertEqual(stat.S_IMODE(os.stat(dst).st_mode), 0o644)
        self.assertFalse(os.path.exists(os.path.join(self.root, "alice", "proj1", "psw")))

    def test_merge_then_move(self):
        note = Notifier()
        d = dv.Deliverer(self.root, FAKE_MERGE, note, background=False)
        d.submit(dv.Job(self.log, "otf"))
        raw = os.path.join(self.root, "alice", "proj1", "otf", "20261110120000.raw")
        with open(raw) as f:
            self.assertEqual(f.read(), self.log)                # マージには一時置き場のロギングを渡す
        self.assertTrue(os.path.exists(os.path.join(self.root, "alice", "proj1", "logging", NAME)))
        self.assertEqual(note.sent, [])
        self.assertEqual([f for f in os.listdir(self.work)], [])    # 出力の一時のフォルダは消える

    def test_merge_failure_still_moves_logging(self):
        note = Notifier()
        d = dv.Deliverer(self.root, FAIL_MERGE, note, background=False)
        d.submit(dv.Job(self.log, "psw"))
        self.assertTrue(os.path.exists(os.path.join(self.root, "alice", "proj1", "logging", NAME)))
        self.assertEqual(len(note.sent), 1)
        self.assertIn("マージに失敗（終了コード 3）", note.sent[0])
        self.assertIn("bad", note.sent[0])

    def test_does_not_overwrite(self):
        dst_dir = os.path.join(self.root, "alice", "proj1", "logging")
        os.makedirs(dst_dir)
        with open(os.path.join(dst_dir, NAME), "w") as f:
            f.write("old")
        d = dv.Deliverer(self.root, None, Notifier(), background=False)
        with self.assertRaises(FileExistsError):
            d.submit(dv.Job(self.log, "psw"))
        self.assertTrue(os.path.exists(self.log))                   # 元は残る
        with open(os.path.join(dst_dir, NAME)) as f:
            self.assertEqual(f.read(), "old")

    def test_other_disk(self):
        # 別のディスク（rename が EXDEV）なら、写してから付け替える
        real = os.rename

        def rename(a, b):
            if a == self.log:
                raise OSError(errno.EXDEV, "cross-device")
            return real(a, b)
        with mock.patch("os.rename", side_effect=rename):
            dst = dv.move_file(self.log, os.path.join(self.root, "x"))
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), b"x" * 100)
        self.assertFalse(os.path.exists(self.log))
        self.assertEqual(os.listdir(os.path.join(self.root, "x")), [NAME])   # .part は残らない

    def test_bad_kind(self):
        d = dv.Deliverer(self.root, None, Notifier(), background=False)
        with self.assertRaises(ValueError):
            d.dest_dirs(self.log, "other")

    def test_background_notifies_failure(self):
        note = Notifier()
        d = dv.Deliverer(self.root, None, note)
        d.submit(dv.Job(os.path.join(self.work, "YKSP.a.b.c.20261110120001"), "psw"))   # 無いファイル
        d.close()
        self.assertEqual(len(note.sent), 1)
        self.assertIn("観測の後の処理に失敗", note.sent[0])


class TestLcDeliver(unittest.TestCase):
    def test_observation_to_download(self):
        with tempfile.TemporaryDirectory() as d:
            dirs = {k: os.path.join(d, k) for k in ("sam45psw", "sam45otf", "yksppsw", "ykspotf", "download")}
            for k in ("sam45psw", "sam45otf"):
                os.makedirs(dirs[k])
            cfg = lcm.LcConfig(watch=[[dirs["sam45psw"], dirs["yksppsw"], "psw"],
                                      [dirs["sam45otf"], dirs["ykspotf"]]],          # 種類は名前から
                               deliver_root=dirs["download"], merge_cmd=FAKE_MERGE)
            note = Notifier()
            lc = lcm.LC(cfg, FakeSpectrometer(), note)
            recs = _sam45_records(_obs(arrays=range(8)), range(8))
            _write(os.path.join(dirs["sam45otf"], "SAM45.otfobs.bob.prj2.20261110000001"), recs)
            _write(os.path.join(dirs["sam45psw"], "SAM45.pswobs.bob.prj2.20261110000002"), recs)
            lc.step(0.0)
            lc.step(0.0)
            lc.close()
            base = os.path.join(dirs["download"], "bob", "prj2")
            self.assertEqual(sorted(os.listdir(base)), ["logging", "otf", "psw"])
            self.assertEqual(os.listdir(os.path.join(base, "otf")), ["20261110000001.raw"])
            self.assertEqual(os.listdir(os.path.join(base, "psw")), ["20261110000002.raw"])
            self.assertEqual(sorted(os.listdir(os.path.join(base, "logging"))),
                             ["YKSP.otfobs.bob.prj2.20261110000001", "YKSP.pswobs.bob.prj2.20261110000002"])
            self.assertEqual(os.listdir(dirs["ykspotf"]), [])          # 一時置き場からは移った
            self.assertEqual(note.sent, [])

    def test_kind_required(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = lcm.LcConfig(watch=[[os.path.join(d, "in"), os.path.join(d, "out")]],
                               deliver_root=os.path.join(d, "download"))
            with self.assertRaises(ValueError):
                lcm.LC(cfg, FakeSpectrometer())


if __name__ == "__main__":
    unittest.main()
