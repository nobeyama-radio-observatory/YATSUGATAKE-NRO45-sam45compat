# SPDX-License-Identifier: BSD-3-Clause
"""combine（合体）・lc の combined・watchdog（素通し）の試験。"""

import os
import sys
import tempfile
import time
import unittest

from sam45compat import combine as cb
from sam45compat import lc as lcm
from sam45compat import mapping as mp
from sam45compat import samlog as sl
from sam45compat import watchdog as wd
from sam45compat import yksp as yk
from sam45compat.fake import FakeDumps, FakeSpectrometer
from sam45compat.notify import Notifier
from test_mapping import _obs
from test_yksp import _sam45_records

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import replay  # noqa: E402

SAMPLE = os.environ.get("SAM45_SAMPLE")
NAME = "20260101000000.SAM45.obs.grp.prj"


def _write(path, recs):
    with open(path, "wb") as f:
        for r in recs:
            f.write(r.to_bytes())


def _names(recs):
    return [(r.kind, sl.cstr(r.s.cary_name) if r.kind == "L2" else "") for r in recs]


class TestMerge(unittest.TestCase):
    def test_replace_and_insert(self):
        sam = [r for r in _sam45_records(_obs(arrays=range(8)), [0, 1, 4, 9], ("R",)) if r.kind == "L2"]
        mine = {}
        for a in (4, 5):
            r = sl.new_record("L2")
            sl.set_cstr(r.s, "cary_name", sl.array_logname(a))
            mine[a] = r
        out = cb.merge_integration(sam, mine)
        self.assertEqual([sl.cstr(r.s.cary_name) for r in out], ["A1", "A2", "A5", "A6", "A10"])
        self.assertIs(out[2], mine[4])       # 差し替え
        self.assertIs(out[3], mine[5])       # 足した


class TestCombineFiles(unittest.TestCase):
    def test_synthetic(self):
        obs = _obs(arrays=range(8))
        sam = _sam45_records(obs, range(8))
        b = yk.YkspBuilder(mp.plan(obs.s), mp.IFATT_IGNORE, FakeDumps({"A0": 3.0}))
        yksp, _ = yk.build_offline(sam, b)
        out, st = cb.combine_records(sam, yksp)
        self.assertEqual(_names(out), _names(sam))           # 並びは SAM45 と同じ
        self.assertEqual(st, dict(integrations=4, replaced=16, added=0, missing=0, yksp_unused=0))
        l1 = out[1].s
        self.assertEqual([i for i in range(32) if l1.iary_usefg[i]], list(range(8)))   # 全 Array を残す
        self.assertEqual(l1.dbebw[4], 32e6)
        self.assertEqual(l1.dbebw[0], 31.25e6)
        self.assertEqual(l1.iary_ifatt[4], 0)
        self.assertEqual(l1.iary_ifatt[0], 5)
        for s, o in zip(sam, out):
            if o.kind == "L2" and sl.array_index(sl.cstr(o.s.cary_name)) < 4:
                self.assertEqual(s.to_bytes(), o.to_bytes())  # 肩代わりでない Array はそのまま

    def test_missing_yksp_invalidates(self):
        obs = _obs(arrays=range(8))
        sam = _sam45_records(obs, range(8))
        b = yk.YkspBuilder(mp.plan(obs.s), mp.IFATT_FOLLOW, FakeDumps())
        yksp, _ = yk.build_offline(sam, b)
        yksp = [r for r in yksp if not (r.kind == "L2" and sl.cstr(r.s.cscan_type) == "ON")]
        out, st = cb.combine_records(sam, yksp)
        self.assertEqual(st["missing"], 4)
        on = [r for r in out if r.kind == "L2" and sl.cstr(r.s.cscan_type) == "ON"]
        self.assertEqual([r.s.idmy_flag for r in on], [0, 0, 0, 0, 1, 1, 1, 1])

    @unittest.skipUnless(SAMPLE, "SAM45_SAMPLE が無い")
    def test_sample_cli(self):
        sam = sl.read_file(SAMPLE)
        b = yk.YkspBuilder(mp.plan(sam[1].s), mp.IFATT_FOLLOW, FakeDumps())
        yksp, _ = yk.build_offline(sam, b)
        with tempfile.TemporaryDirectory() as d:
            yp, op = os.path.join(d, "y"), os.path.join(d, "o")
            _write(yp, yksp)
            cb.main([SAMPLE, yp, "-o", op])
            out = sl.read_file(op)
            self.assertEqual(_names(out), _names(sam))
            a13 = [(s, o) for s, o in zip(sam, out) if o.kind == "L2" and sl.cstr(o.s.cary_name) == "A13"]
            self.assertEqual(len(a13), 52)
            self.assertTrue(all(s.to_bytes() == o.to_bytes() for s, o in a13))
            self.assertEqual(out[1].s.dbebw[4], 32e6)
            self.assertEqual(out[1].s.dbebw[12], 31.25e6)


class TestCombinedLC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.hb = os.path.join(self.d, "hb")
        self.cfg = lcm.LcConfig(log_dir=self.d, output="combined", in_prefix="_")
        self.recs = _sam45_records(_obs(arrays=range(8)), range(8), ("ZERO", "R", "SKY", "ON", "OFF", "ON"))
        self.src = os.path.join(self.d, "_" + NAME)
        self.dst = os.path.join(self.d, NAME)

    def tearDown(self):
        self.tmp.cleanup()

    def test_combined_output(self):
        spec = FakeSpectrometer()
        lc = lcm.LC(self.cfg, spec)
        for k in range(1, len(self.recs) + 1):
            _write(self.src, self.recs[:k])
            lc.step(0.0)
        out = sl.read_file(self.dst)
        self.assertEqual(_names(out), _names(self.recs))
        self.assertEqual(out[1].s.dbebw[4], 32e6)
        self.assertEqual(lc.sessions, {})

    def test_same_name_rejected(self):
        cfg = lcm.LcConfig(log_dir=self.d, output="combined", in_prefix="")
        with self.assertRaises(ValueError):
            lcm.Session(os.path.join(self.d, NAME), cfg, FakeSpectrometer(), Notifier())

    def test_watchdog_takes_over_midway(self):
        lc = lcm.LC(self.cfg, FakeSpectrometer(), Notifier())
        half = 2 + 8 * 3                              # ZERO・R・SKY まで
        for k in range(1, half + 1):
            _write(self.src, self.recs[:k])
            lc.step(0.0)
        # 合体ファイルの最後に、書きかけのレコードを作る（落ちた途中）
        with open(self.dst, "ab") as f:
            f.write(self.recs[half].to_bytes()[:1000])
        # 暫定 LC が止まった: 心拍が古い
        with open(self.hb, "w") as f:
            f.write(f"{time.time() - 100:.3f} 1\n")
        note = Notifier()
        w = wd.Watchdog(wd.WatchdogConfig(self.d, self.hb, stale_s=30), note)
        _write(self.src, self.recs)                   # SAM45 は最後まで書いた
        w.step()
        w.step()
        self.assertTrue(os.path.exists(self.dst + lcm.MARKER_SUFFIX))
        self.assertTrue(any("見張り" in t for t in note.sent))
        out = sl.read_file(self.dst)
        self.assertEqual(_names(out), _names(self.recs))
        self.assertEqual(out[-1].kind, "ED")
        # 前半（合体した分）は YKSP、後半（素通し）は SAM45 のまま
        a5 = [r for r in out if r.kind == "L2" and sl.cstr(r.s.cary_name) == "A5"]
        sam_a5 = [r for r in self.recs if r.kind == "L2" and sl.cstr(r.s.cary_name) == "A5"]
        self.assertNotEqual(a5[1].to_bytes(), sam_a5[1].to_bytes())   # R は合体
        self.assertEqual(a5[4].to_bytes(), sam_a5[4].to_bytes())      # OFF は素通し
        # 暫定 LC は印を見て手を引く
        lc.step(100.0)
        self.assertEqual(lc.sessions, {})
        self.assertEqual(sl.read_file(self.dst)[-1].kind, "ED")
        lc.close()

    def test_watchdog_quiet_while_heartbeat(self):
        with open(self.hb, "w") as f:
            f.write(f"{time.time():.3f} 1\n")
        _write(self.src, self.recs[:10])
        w = wd.Watchdog(wd.WatchdogConfig(self.d, self.hb, stale_s=30))
        w.step()
        self.assertFalse(os.path.exists(self.dst))

    def test_watchdog_full_copy_without_lc(self):
        _write(self.src, self.recs)
        w = wd.Watchdog(wd.WatchdogConfig(self.d, self.hb, stale_s=30))   # 心拍のファイルが無い
        w.step()
        with open(self.src, "rb") as a, open(self.dst, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_heartbeat_thread(self):
        cfg = lcm.LcConfig(log_dir=self.d, output="combined", in_prefix="_",
                           heartbeat_path=self.hb, heartbeat_s=0.05)
        lc = lcm.LC(cfg, FakeSpectrometer())
        time.sleep(0.3)
        w = wd.Watchdog(wd.WatchdogConfig(self.d, self.hb, stale_s=1))
        self.assertLess(w.heartbeat_age(time.time()), 1.0)
        lc.close()

    @unittest.skipUnless(SAMPLE, "SAM45_SAMPLE が無い")
    def test_sample_replay_combined(self):
        lc = lcm.LC(self.cfg, FakeSpectrometer())
        path = replay.replay(SAMPLE, self.d, speed=0, prefix="_", log_id="20261106140000",
                             on_chunk=lambda p: lc.step(0.0))
        lc.step(0.0)
        out = sl.read_file(os.path.join(self.d, os.path.basename(path)[1:]))
        sam = sl.read_file(SAMPLE)
        self.assertEqual(_names(out), _names(sam))
        diff = [o for s, o in zip(sam, out) if s.to_bytes() != o.to_bytes()]
        self.assertEqual({o.kind for o in diff}, {"L1", "L2"})
        self.assertEqual({sl.cstr(o.s.cary_name) for o in diff if o.kind == "L2"}, {"A5", "A6"})


if __name__ == "__main__":
    unittest.main()
