# SPDX-License-Identifier: BSD-3-Clause
"""binning と yksp の試験。"""

import os
import unittest

import numpy as np

from sam45compat import binning as bn
from sam45compat import mapping as mp
from sam45compat import samlog as sl
from sam45compat import yksp as yk
from test_mapping import _obs

SAMPLE = os.environ.get("SAM45_SAMPLE")
T = bn.TINT_NS


class TestBinning(unittest.TestCase):
    def test_center_rule_and_count(self):
        phase = 3_000_000                       # 秒の境界から 3 ms ずれた格子
        t0 = 10**18 + phase
        t = t0 + np.arange(1000) * T
        spec = np.arange(1000, dtype=np.float32)[:, None] * np.ones((1, 4))
        st, ed = 10**18 + 1_000_000_000, 10**18 + 6_000_000_000   # 5 s
        b = bn.bin_interval(t, spec, st, ed)
        c = t + T // 2
        want = np.where((c >= st) & (c < ed))[0]
        self.assertEqual(b.n, len(want))
        self.assertEqual(b.n_expected, len(want))
        self.assertAlmostEqual(b.coverage, 1.0)
        np.testing.assert_allclose(b.spec, spec[want].mean(axis=0))
        self.assertIn(b.n, (488, 489))           # 5 s / 10.24 ms = 488.3

    def test_missing_dumps(self):
        t = 10**18 + np.arange(100) * T
        keep = np.ones(100, bool); keep[40:50] = False
        b = bn.bin_interval(t[keep], np.ones((keep.sum(), 2)), int(t[0]), int(t[-1]) + T)
        self.assertEqual(b.n_expected, 100)
        self.assertEqual(b.n, 90)

    def test_memory_dumps(self):
        m = bn.MemoryDumps()
        for k in range(10):
            m.add("A0", 1000 + k * T, np.full(4, k), flags=4 if k == 5 else 0)
            m.add("B0", 1000 + k * T, np.full(4, 2 * k))
        b = m.interval("A0", 1000, 1000 + 10 * T)
        self.assertEqual(b.n, 10)
        self.assertEqual(b.flags, 4)
        self.assertAlmostEqual(b.spec[0], 4.5)
        self.assertEqual(m.latest_end_ns(), 1000 + 10 * T)
        m.drop_before(1000 + 5 * T)
        self.assertEqual(m.interval("A0", 0, 10**9).n, 5)


class FakeDumps:
    """どの区間を聞かれても、窓ごとに決まった形のスペクトル（記録の IF の昇順）を返す。"""

    def __init__(self, level=None):
        self.level = level or {}
        self.asked = []

    def interval(self, key, st, ed):
        self.asked.append((key, st, ed))
        n = (ed - st) // T
        spec = self.level.get(key, 1.0) * (1 + np.arange(4096) / 4096.0)
        return bn.Binned(spec, n, n)

    def latest_end_ns(self):
        return 2**62


def _sam45_records(obs_rec, arrays, scans=("ZERO", "R", "SKY", "ON")):
    """模擬の SAM45 のロギング（L0・L1・積分ごとに arrays の L2・ED）。"""
    ctl = sl.new_record("L0")
    sl.set_cstr(ctl.s, "cbe_type", "SAM45")
    recs = [ctl, obs_rec]
    t = 5.3e9
    for n, sc in enumerate(scans):
        for a in arrays:
            d = sl.new_record("L2")
            sl.set_cstr(d.s, "cary_name", sl.array_logname(a))
            d.s.iary_no = 41 + a
            sl.set_cstr(d.s, "cscan_type", sc)
            sl.set_cstr(d.s, "cscanid", f"x.{n:05d}")
            d.s.dint_stmjd, d.s.dint_edmjd = t, t + 5
            d.s.iinteg_time = 5
            d.s.dtsys = 300.0
            d.s.dweather[0] = 14.3
            recs.append(d)
        t += 8
    recs.append(sl.new_record("ED"))
    return recs


class TestBuilder(unittest.TestCase):
    def setUp(self):
        # SAM45 の観測は A01〜A08（段階 1: A01〜A04 と同じ IF を A05〜A08 に）
        self.obs = _obs(arrays=range(8))
        self.plan = mp.plan(self.obs.s)
        self.dumps = FakeDumps({"A0": 2.0})

    def test_offline_file(self):
        sam = _sam45_records(self.obs, range(8))
        b = yk.YkspBuilder(self.plan, mp.IFATT_FOLLOW, self.dumps, scale=10.0)
        out, res = yk.build_offline(sam, b)
        kinds = [r.kind for r in out]
        self.assertEqual(kinds, ["L0", "L1"] + ["L2"] * 16 + ["ED"])    # 4 積分 × A05〜A08
        l1 = out[1].s
        self.assertEqual([i for i in range(32) if l1.iary_usefg[i]], [4, 5, 6, 7])
        self.assertEqual(l1.iary_num, 4)
        self.assertEqual(l1.dbebw[4], 32e6)
        self.assertEqual(l1.iary_ifatt[4], 5)                            # 従うモード
        self.assertEqual(self.obs.s.iary_usefg[0], 1)                     # 元は変えない
        names = [sl.cstr(r.s.cary_name) for r in out if r.kind == "L2"]
        self.assertEqual(names[:4], ["A5", "A6", "A7", "A8"])
        zero, r_a5 = out[2].s, out[6].s
        self.assertEqual(sl.cstr(zero.cscan_type), "ZERO")
        self.assertTrue(all(v == 8192.0 for v in zero.fary_data))
        self.assertEqual(sl.cstr(r_a5.cscan_type), "R")
        self.assertEqual(r_a5.iary_no, 45)
        self.assertEqual(r_a5.idmy_flag, 0)
        self.assertEqual(r_a5.dtsys, 0.0)                                # 最初の SKY までは 0
        self.assertEqual(r_a5.dweather[0], 14.3)                          # 付随の欄は SAM45 から
        self.assertAlmostEqual(r_a5.fary_data[0], 2.0 * 10.0, places=4)   # 記録の昇順のまま（reverse でない）
        self.assertTrue(all(r.valid for r in res))
        # ZERO は束ねない
        self.assertEqual(len(self.dumps.asked), 3 * 4)
        # 書き出すと SAM45 と同じ形で読み直せる
        buf = b"".join(r.to_bytes() for r in out)
        self.assertEqual([r.kind for r in sl.iter_records(buf)], kinds)

    def test_template_from_same_array(self):
        # SAM45 のロギングに肩代わりの Array の L2 があれば、付随の欄は同じ Array の L2 から写す
        sam = _sam45_records(self.obs, range(8))
        for r in sam:
            if r.kind == "L2":
                r.s.dweather[1] = 900.0 + r.s.iary_no          # Array ごとに違う値
        b = yk.YkspBuilder(self.plan, mp.IFATT_FOLLOW, self.dumps)
        out, _ = yk.build_offline(sam, b)
        l2 = [r.s for r in out if r.kind == "L2"]
        self.assertEqual([d.dweather[1] for d in l2[:4]], [945.0, 946.0, 947.0, 948.0])
        self.assertEqual([d.iary_no for d in l2[:4]], [45, 46, 47, 48])
        # 同じ Array の L2 が無ければ、最初の L2 から写す
        sam = _sam45_records(self.obs, range(4))
        for r in sam:
            if r.kind == "L2":
                r.s.dweather[1] = 900.0 + r.s.iary_no
        out, _ = yk.build_offline(sam, yk.YkspBuilder(self.plan, mp.IFATT_FOLLOW, self.dumps))
        self.assertEqual([r.s.dweather[1] for r in out if r.kind == "L2"][:4], [941.0] * 4)
        # L1 を埋めた元の Array を渡せば、その L2 から写す（A05←A01 … A08←A04。2026-11-10）
        b = yk.YkspBuilder(self.plan, mp.IFATT_FOLLOW, self.dumps, template_src={4: 0, 5: 1, 6: 2, 7: 3})
        out, _ = yk.build_offline(sam, b)
        l2 = [r.s for r in out if r.kind == "L2"]
        self.assertEqual([d.dweather[1] for d in l2[:4]], [941.0, 942.0, 943.0, 944.0])
        self.assertEqual([sl.cstr(d.cary_name) for d in l2[:4]], ["A5", "A6", "A7", "A8"])
        self.assertEqual([d.iary_no for d in l2[:4]], [45, 46, 47, 48])

    def test_tsys(self):
        class Levels(FakeDumps):
            def interval(self, key, st, ed):
                n = (ed - st) // T
                return bn.Binned(np.full(4096, levels[self.scan]), n, n)
        levels = {"R": 3.0, "SKY": 1.0, "ON": 1.1}
        d = Levels()
        b = yk.YkspBuilder(self.plan, mp.IFATT_IGNORE, d)
        out = b.header(sl.new_record("L0"), self.obs)
        res = []
        for sc in ("ZERO", "R", "SKY", "ON"):
            d.scan = sc
            integ = yk.Integration(("x", 5.3e9, 5.3e9 + 5, sc),
                                   [r for r in _sam45_records(self.obs, [4], (sc,)) if r.kind == "L2"])
            res.append(b.integration(integ)[0].record.s.dtsys)
        # IFATT を無視（0 dB）: Tsys = 293·1/(3 − 1) = 146.5
        self.assertEqual(res[:2], [0.0, 0.0])
        self.assertAlmostEqual(res[2], 146.5)
        self.assertAlmostEqual(res[3], 146.5)

    def test_tsys_formula_reproduces_sam45(self):
        # サンプルの A05: ZERO を引いた R・SKY の平均と IFATT 5 dB、SAM45 の dtsys 359.16
        self.assertAlmostEqual(yk.tsys_k(37759.98, 65742.23, 5.0), 359.16, delta=0.5)

    def test_ignore_ifatt(self):
        b = yk.YkspBuilder(self.plan, mp.IFATT_IGNORE, self.dumps)
        hdr = b.header(sl.new_record("L0"), self.obs)
        self.assertEqual(hdr[1].s.iary_ifatt[4], 0)

    def test_skipped_array_flagged(self):
        obs = _obs(bw1=31.25, bw2=1000.0, k0_2=0, arrays=range(8), arrays2=(20,))
        p = mp.plan(obs.s)
        self.assertIn(20, p.skipped)
        b = yk.YkspBuilder(p, mp.IFATT_FOLLOW, self.dumps)
        out, res = yk.build_offline(_sam45_records(obs, list(range(8)) + [20]), b)
        a21 = [r for r in res if r.array == 20]
        self.assertEqual(len(a21), 4)
        self.assertTrue(all(not r.valid and r.record.s.idmy_flag == 1 for r in a21))

    def test_coverage_flag(self):
        class Short(FakeDumps):
            def interval(self, key, st, ed):
                n = (ed - st) // T
                return bn.Binned(np.ones(4096), n // 2, n)
        b = yk.YkspBuilder(self.plan, mp.IFATT_FOLLOW, Short())
        out, res = yk.build_offline(_sam45_records(self.obs, range(8)), b)
        r = [x for x in res if sl.cstr(x.record.s.cscan_type) == "R"][0]
        self.assertFalse(r.valid)
        self.assertEqual(r.record.s.idmy_flag, 1)

    def test_grouper(self):
        g = yk.IntegrationGrouper()
        recs = [r for r in _sam45_records(self.obs, range(8)) if r.kind == "L2"]
        done = []
        for r in recs:
            done += g.feed(r)
        done += g.flush()
        self.assertEqual([len(i.records) for i in done], [8, 8, 8, 8])
        self.assertEqual(done[1].ed_ns - done[1].st_ns, 5 * 10**9)


    def test_otf_center_time(self):
        """OTF の ON は dint_stmjd・dint_edmjd が 0 で、cint_sttm（JST の中心、ms まで）だけ（20261112202705 の形。2026-11-12）。"""
        self.assertEqual(yk.jst_to_mjdsec("20261112202831"), 5301199711.0)   # 同じ観測の ZERO の dint_stmjd
        self.assertAlmostEqual(yk.jst_to_mjdsec("20261112202949.0500"), 5301199789.05, places=6)
        self.assertIsNone(yk.jst_to_mjdsec(""))
        self.obs.s.diptim = 0.1
        sam = _sam45_records(self.obs, range(8), scans=("ZERO", "R", "SKY", "OFF"))[:-1]
        for k in range(3):
            for a in range(8):
                d = sl.new_record("L2")
                sl.set_cstr(d.s, "cary_name", sl.array_logname(a))
                sl.set_cstr(d.s, "cscan_type", "ON")
                sl.set_cstr(d.s, "cscanid", "x.00005")
                sl.set_cstr(d.s, "cint_sttm", f"20261112202949.{5 + 10 * k:02d}00")
                d.s.iinteg_time = 20
                sam.append(d)
        sam.append(sl.new_record("ED"))
        dumps = FakeDumps()
        b = yk.YkspBuilder(self.plan, mp.IFATT_FOLLOW, dumps)
        out, res = yk.build_offline(sam, b)
        on = [r for r in res if sl.cstr(r.record.s.cscan_type) == "ON"]
        self.assertEqual(len(on), 3 * 4)                                  # 積分ごとに分かれる（3 積分 × A05〜A08）
        self.assertTrue(all(r.valid for r in on))
        c0 = sl.mjdsec_to_unix_ns(5301199789.05)
        spans = sorted({(st, ed) for _, st, ed in dumps.asked if st >= c0 - 10**9})
        want = [(c0 - 50_000_000 + k * 100_000_000, c0 + 50_000_000 + k * 100_000_000) for k in range(3)]
        self.assertEqual(len(spans), 3)
        for got, w in zip(spans, want):                                  # MJD の秒（double）の丸めで 1 µs ほどずれる
            self.assertLess(max(abs(got[0] - w[0]), abs(got[1] - w[1])), 2_000)
        # diptim が 0 なら幅を作れないので無効（ダンプを探さない）
        self.obs.s.diptim = 0.0
        out, res = yk.build_offline(sam, yk.YkspBuilder(self.plan, mp.IFATT_FOLLOW, FakeDumps()))
        on = [r for r in res if sl.cstr(r.record.s.cscan_type) == "ON"]
        self.assertTrue(all(not r.valid and "幅が 0" in r.reason for r in on))


@unittest.skipUnless(SAMPLE, "SAM45_SAMPLE が無い")
class TestSampleBuild(unittest.TestCase):
    def test_build_from_sample(self):
        sam = sl.read_file(SAMPLE)
        p = mp.plan(sam[1].s)
        b = yk.YkspBuilder(p, mp.IFATT_FOLLOW, FakeDumps())
        out, res = yk.build_offline(sam, b)
        n_integ = len({yk.integration_key(r.s) for r in sam if r.kind == "L2"})
        self.assertEqual(n_integ, 52)
        self.assertEqual(sum(r.kind == "L2" for r in out), 52 * 2)        # A05・A06
        sam_a5 = [r for r in sam if r.kind == "L2" and sl.cstr(r.s.cary_name) == "A5"]
        yk_a5 = [r for r in out if r.kind == "L2" and sl.cstr(r.s.cary_name) == "A5"]
        for s, y in zip(sam_a5, yk_a5):
            self.assertEqual(yk.integration_key(s.s), yk.integration_key(y.s))
            self.assertEqual(sl.cstr(s.s.cint_sttm), sl.cstr(y.s.cint_sttm))


if __name__ == "__main__":
    unittest.main()
