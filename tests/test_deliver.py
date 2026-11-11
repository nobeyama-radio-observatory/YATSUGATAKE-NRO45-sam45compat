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

# マージの代わり（本物と同じ引数: <group> <project> <ログ ID> <置き場所> <ロギング>）:
# <置き場所>/<group>/<project>/ の直下に <ログ ID>.raw を書き、中にロギングのパスを書く
_FAKE = ("import sys, os; g, p, i, root, lg = sys.argv[1:]; assert os.path.exists(lg); "
         "open(os.path.join(root, g, p, i + '.raw'), 'w').write(lg)")
FAKE_MERGE = [sys.executable, "-c", _FAKE, "{group}", "{project}", "{log_id}", "{root}", "{logging}"]
FAIL_MERGE = [sys.executable, "-c",
              "import sys, os; open(os.path.join(sys.argv[1], 'half.raw'), 'w'); sys.stderr.write('bad'); sys.exit(3)",
              "{root}/{group}/{project}"]


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

    def test_move_then_merge(self):
        note = Notifier()
        proj = os.path.join(self.root, "alice", "proj1")
        os.makedirs(proj)
        with open(os.path.join(proj, "old.raw"), "w") as f:      # 前からあるものは動かさない
            f.write("old")
        d = dv.Deliverer(self.root, {"otf": FAKE_MERGE}, note, background=False)
        d.submit(dv.Job(self.log, "otf"))
        lg = os.path.join(proj, "logging", NAME)
        with open(os.path.join(proj, "otf", "20261110120000.raw")) as f:
            self.assertEqual(f.read(), lg)                      # マージには logging/ に移した後のロギングを渡す
        self.assertEqual(sorted(os.listdir(proj)), ["logging", "old.raw", "otf"])
        self.assertEqual(note.sent, [])
        self.assertEqual(os.listdir(self.work), [])

    def test_merge_root(self):
        # マージの計算機からは置き場所が別の場所に見える（NFS のマウント先）
        rec = os.path.join(self.tmp.name, "args")
        cmd = [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write(' '.join(sys.argv[2:]))",
               rec, "{group}", "{project}", "{log_id}", "{root}", "{logging}"]
        d = dv.Deliverer(self.root, cmd, Notifier(), background=False, merge_root="/cosmos3/45m/bklog/yksp")
        d.submit(dv.Job(self.log, "psw"))
        with open(rec) as f:
            self.assertEqual(f.read(), "alice proj1 20261110120000 /cosmos3/45m/bklog/yksp "
                             "/cosmos3/45m/bklog/yksp/alice/proj1/logging/" + NAME)

    def test_merge_per_kind(self):
        d = dv.Deliverer(self.root, {"otf": FAKE_MERGE}, Notifier(), background=False)
        d.submit(dv.Job(self.log, "psw"))                       # psw のマージは無い
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "alice", "proj1"))), ["logging"])
        with self.assertRaises(ValueError):
            dv.Deliverer(self.root, {"other": FAKE_MERGE}, background=False)

    def test_redo_from_logging(self):
        # マージに失敗した後、logging/ にあるロギングで手でやり直す
        d = dv.Deliverer(self.root, None, Notifier(), background=False)
        d.submit(dv.Job(self.log, "psw"))
        lg = os.path.join(self.root, "alice", "proj1", "logging", NAME)
        d = dv.Deliverer(self.root, FAKE_MERGE, Notifier(), background=False)
        d.submit(dv.Job(lg, "psw"))
        self.assertTrue(os.path.exists(lg))
        self.assertTrue(os.path.exists(os.path.join(self.root, "alice", "proj1", "psw", "20261110120000.raw")))

    def test_cli_config(self):
        import json
        cfg = os.path.join(self.tmp.name, "c.json")
        with open(cfg, "w") as f:
            json.dump({"deliver_root": self.root, "merge_cmd": {"psw": FAKE_MERGE}}, f)
        dv.main([self.log, "--kind", "psw", "--config", cfg])
        self.assertTrue(os.path.exists(os.path.join(self.root, "alice", "proj1", "psw", "20261110120000.raw")))

    def test_unsafe_value(self):
        bad = os.path.join(self.work, "YKSP.ob;s.alice.proj1.20261110120000")
        os.rename(self.log, bad)
        d = dv.Deliverer(self.root, FAKE_MERGE, Notifier(), background=False)
        with self.assertRaises(ValueError):
            d.submit(dv.Job(bad, "psw"))

    def test_merge_failure_still_moves_logging(self):
        note = Notifier()
        d = dv.Deliverer(self.root, FAIL_MERGE, note, background=False)
        d.submit(dv.Job(self.log, "psw"))
        self.assertTrue(os.path.exists(os.path.join(self.root, "alice", "proj1", "logging", NAME)))
        self.assertEqual(len(note.sent), 1)
        self.assertIn("マージに失敗（終了コード 3）", note.sent[0])
        self.assertIn("bad", note.sent[0])
        self.assertIn("half.raw", note.sent[0])                 # 途中のファイルは振り分けず、知らせる
        self.assertFalse(os.path.exists(os.path.join(self.root, "alice", "proj1", "psw")))

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
