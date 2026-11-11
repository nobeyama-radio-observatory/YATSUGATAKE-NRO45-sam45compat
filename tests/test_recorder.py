# SPDX-License-Identifier: BSD-3-Clause
"""recorder.DiskDumps の試験（ボードは要らない）。"""

import tempfile
import time
import unittest

import numpy as np

from sam45compat import recorder as rc
from sam45compat.binning import TINT_NS

STEP = 2621440          # tint 1 つぶんのビート（値は何でもよい）


def _dump(dump_t, utc_ns=0, health=0, s=0, val=1.0):
    return dict(core=0, s=s, nch=rc.NCH, fmt=rc.P.FMT_F32, flags=0, sat=0, utc_ns=utc_ns, health=health,
                if_mhz=1000.0, df_hz=1e6, dump_t=dump_t,
                data=np.full(rc.NCH, val, dtype="<f4"))


class TestNoPps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_without_no_pps_dumps_are_unusable(self):
        dd = rc.DiskDumps(self.tmp.name + "/a", {"A0": (1000.0, 1.0)})
        for i in range(10):
            dd.add(_dump(1000 + i * STEP, health=rc.P.H_NOTIME))
        self.assertEqual(dd.n_notime, 10)
        self.assertIsNone(dd.interval("A0", 0, 2**62).spec)
        dd.close()

    def test_no_pps_times_from_host_clock(self):
        dd = rc.DiskDumps(self.tmp.name + "/b", {"A0": (1000.0, 1.0), "A1": (1000.0, 1.0)}, no_pps=True)
        t0 = time.time_ns()
        # ダンプ 5 は A0 で落ちた（差が倍）
        for i in range(20):
            for s in (0, 1):
                if s == 0 and i == 5:
                    continue
                dd.add(_dump(1000 + i * STEP, s=s, val=float(i)))
        t1 = time.time_ns()
        h0 = dd._anchor[0]
        self.assertTrue(t0 - TINT_NS <= h0 <= t1 - TINT_NS)
        self.assertEqual(dd.n_notime, 0)
        end = dd.latest_end_ns()
        self.assertEqual(end, h0 + 20 * TINT_NS)
        # 窓どうしで同じ時刻（同じ dump_t は同じ番号）
        b0 = dd.interval("A0", h0, h0 + 10 * TINT_NS)
        b1 = dd.interval("A1", h0, h0 + 10 * TINT_NS)
        self.assertEqual(b1.n, 10)
        self.assertEqual(b0.n, 9)                               # 落ちたダンプの分だけ少ない
        self.assertAlmostEqual(float(b1.spec[0]), np.mean(range(10)), places=4)
        self.assertAlmostEqual(float(b0.spec[0]), np.mean([i for i in range(10) if i != 5]), places=4)
        dd.close()


class TestRefWatch(unittest.TestCase):
    def test_marks(self):
        w = rc.RefWatch()
        self.assertEqual((w.info()["n"], w.info()["t_int"]), (0, None))
        w.add(0, t=100.0)
        w.add(rc.H_PPS_INT, t=101.0)                 # 1PPS の間隔が外れた（10 MHz なし）
        w.add(rc.H_PPS_MISS | rc.H_NOANCHOR, t=102.0)
        w.add(rc.P.H_FORCED, t=103.0)                 # サーバーの印は見ない
        i = w.info()
        self.assertEqual((i["n"], i["t_last"]), (4, 103.0))
        self.assertEqual((i["t_int"], i["n_int"]), (101.0, 1))
        self.assertEqual((i["t_miss"], i["n_miss"]), (102.0, 1))
        self.assertEqual((i["t_glitch"], i["n_glitch"]), (None, 0))


if __name__ == "__main__":
    unittest.main()
