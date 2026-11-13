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


class TestIndexed(unittest.TestCase):
    """時刻のあるダンプの束ね（索引で切り出す。2026-11-13）が、全部を配列にして束ねるのと同じ結果になる。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _fill(self, path, n, notime=(), drop=(), t0=1_794_000_000_000_000_000 + 1234):
        dd = rc.DiskDumps(path, {"A0": (1000.0, 1.0)})
        rng = np.random.default_rng(1)
        ts, vals, fls = [], [], []
        for i in range(n):
            if i in drop:
                continue
            v = float(rng.normal())
            d = _dump(i, utc_ns=t0 + i * TINT_NS, val=v)
            if i in notime:
                d["health"] = rc.P.H_NOTIME
                d["utc_ns"] = 0
            else:
                d["flags"] = 1 if i % 7 == 0 else 0
                ts.append(t0 + i * TINT_NS)
                vals.append(v)
                fls.append(d["flags"])
            dd.add(d)
        return dd, np.array(ts, dtype=np.int64), np.array(vals), np.array(fls, dtype=np.int64)

    def test_same_as_full(self):
        from sam45compat.binning import bin_interval
        t0 = 1_794_000_000_000_000_000 + 1234
        dd, ts, vals, fls = self._fill(self.tmp.name + "/c", 500, notime={3, 4, 200}, drop={10, 11, 12, 300})
        spec = np.repeat(vals[:, None], rc.NCH, axis=1).astype("<f4")
        for st_i, ed_i in ((0, 100), (2.5, 13.7), (-50, 5), (9, 13), (10, 12), (480, 600), (600, 700), (0, 500)):
            st, ed = int(t0 + st_i * TINT_NS), int(t0 + ed_i * TINT_NS)
            a = dd.interval("A0", st, ed)
            b = bin_interval(ts, spec, st, ed, fls, TINT_NS)
            self.assertEqual((a.n, a.n_expected, a.flags), (b.n, b.n_expected, b.flags), (st_i, ed_i))
            if b.spec is None:
                self.assertIsNone(a.spec)
            else:
                np.testing.assert_allclose(a.spec, b.spec, rtol=1e-6)
        dd.close()

    def test_unordered_raises(self):
        dd = rc.DiskDumps(self.tmp.name + "/d", {"A0": (1000.0, 1.0)})
        for t in (3, 2):
            dd.add(_dump(t, utc_ns=1_794_000_000_000_000_000 + t * TINT_NS))
        with self.assertRaises(ValueError):
            dd.interval("A0", 0, 2**62)
        dd.close()

    def test_cost_does_not_grow(self):
        """1 回の束ねの重さが、溜まったダンプの数によらない（2026-11-13 の夜は数に比例して LC が遅れた）。

        一時ファイルを何 GB も書かないよう、索引の前に古いダンプの索引を 200 万個足して比べる（行は 0 を指す）。
        """
        t0 = 1_794_000_000_000_000_000
        dd, _, _, _ = self._fill(self.tmp.name + "/e", 1000, t0=t0)
        st, ed = t0 + 980 * TINT_NS, t0 + 990 * TINT_NS

        def cost():
            c = time.perf_counter()
            for _ in range(50):
                b = dd.interval("A0", st, ed)
            self.assertEqual(b.n, 10)
            return (time.perf_counter() - c) / 50

        small = cost()
        n_old = 2_000_000
        for name, pre in (("_vt", t0 - (n_old - np.arange(n_old)) * TINT_NS),
                          ("_vrow", np.zeros(n_old, dtype=np.int64)), ("_vfl", np.zeros(n_old, dtype=np.int64))):
            g = getattr(dd, name)["A0"]
            g._a = np.concatenate([pre.astype(np.int64), g.view()])
            g.n = len(g._a)
        self.assertEqual(dd.interval("A0", t0 - 5 * TINT_NS, t0).n, 5)    # 足した索引も引ける
        big = cost()
        self.assertLess(big, small * 5 + 2e-4, (small, big))
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
