# SPDX-License-Identifier: BSD-3-Clause
"""mapping の試験。模擬の L1 と、SAM45_SAMPLE があれば実際の L1 で。"""

import os
import unittest

import numpy as np

from sam45compat import mapping as mp
from sam45compat import samlog as sl

SAMPLE = os.environ.get("SAM45_SAMPLE")


def _obs(bw1=31.25, bw2=None, k0_1=258048, k0_2=None, arrays=(4, 5, 6, 7), arrays2=(), nch=4096):
    """模擬の L1。窓 1（A01〜A16）と窓 2（A17〜A32）。sky は USB 風に IF の昇順で下がる（サンプルと同じ向き）。
    nch: Array の ch 数（ichannel。2026-11-12 の 2 窓の観測は 2048）。"""
    rec = sl.new_record("L1")
    o = rec.s
    sl.set_cstr(o, "csub_array", "OFF")
    o.in_spec_window[0] = 1 if bw2 is None else 2
    for spw, bw, k0 in ((0, bw1, k0_1), (1, bw2, k0_2)):
        if bw is None:
            continue
        avg = int(round(bw / mp.SAM45_DK_MHZ / nch))
        o.istart_chan[0][spw] = k0
        o.iend_chan[0][spw] = k0 + nch * avg - 1
        o.ichan_avg[0][spw] = avg
    for idx in list(arrays) + list(arrays2):
        spw = 0 if idx < 16 else 1
        bw = bw1 if spw == 0 else bw2
        o.iary_usefg[idx] = 1
        o.ichannel[idx] = nch
        o.dbebw[idx] = bw * 1e6
        o.iary_ifatt[idx] = 5
        # dfqdat: ch 4096 と ch 1、sky = 46122 MHz − IF
        avg = o.ichan_avg[0][spw]
        k0 = o.istart_chan[0][spw]
        if_1 = mp.SAM45_IF_LO_MHZ + (k0 + (avg - 1) / 2) * mp.SAM45_DK_MHZ
        if_n = mp.SAM45_IF_LO_MHZ + (k0 + (nch - 1) * avg + (avg - 1) / 2) * mp.SAM45_DK_MHZ
        o.dfqdat_ch[idx][0], o.dfqdat_ch[idx][1] = nch, 1
        o.dfqdat_fq[idx][0] = (46122.0 - if_n) * 1e6
        o.dfqdat_fq[idx][1] = (46122.0 - if_1) * 1e6
        o.dfqdat_f0[idx] = (46122.0 - (if_1 + if_n) / 2) * 1e6
    o.iary_num = len(arrays) + len(arrays2)
    return rec


class TestNch2048(unittest.TestCase):
    """SAM45 の Array が 2048 ch（2026-11-12 の 2 窓の観測: 62.5 MHz を 8 ch ずつ束ねて 2048 ch）。"""

    def test_plan(self):
        o = _obs(bw1=62.5, k0_1=217024, bw2=62.5, k0_2=413824, arrays=(4,), arrays2=(20,), nch=2048)
        self.assertEqual(o.s.ichan_avg[0][0], 8)
        p = mp.plan(o.s)
        self.assertEqual((sorted(p.windows), p.skipped), ([4, 20], {}))
        w = p.windows[4]
        self.assertEqual((w.bw_mhz, w.nbin, w.nch_out, len(w.sky_hz)), (64.0, 2, 2048, 2048))
        self.assertAlmostEqual(w.out_df_mhz, 64 / 2048)
        # L2 の ch c の sky は、SAM45 の ch c の sky と半 ch 以内（中心は記録の ch の格子に丸める）
        s = mp.sam45_window(o.s, 4, mp.Sam45IfModel())
        sam_sky = s.sky_a + s.sky_b * s.if_of_ch * 1e6
        self.assertLess(np.max(np.abs(w.sky_hz - sam_sky)) / 1e6, 0.03 * 64 / 2 + w.df_mhz)   # 帯域の差（2.4 %）の半分ほど
        spec = np.arange(4096.0)
        out = w.l2_order(spec)
        self.assertEqual(len(out), 2048)
        self.assertEqual(out[0], 0.5 if not w.reverse else 4094.5)                             # 隣り合う 2 ch の平均
        l1 = mp.rewrite_l1(o, p, mp.IFATT_FOLLOW).s
        self.assertAlmostEqual(l1.dbechwid[4], 64e6 / 2048)
        self.assertEqual(l1.ichannel[4], 2048)

    def test_bad_nch(self):
        o = _obs(arrays=(4,))
        o.s.ichannel[4] = 3000
        self.assertIn("ch 数が 3000", mp.plan(o.s).skipped[4])


class TestFine(unittest.TestCase):
    def test_center_window(self):
        p = mp.plan(_obs().s)
        self.assertEqual(p.kind, "fine")
        self.assertEqual(sorted(p.windows), [4, 5, 6, 7])
        self.assertEqual(p.skipped, {})
        w = p.windows[4]
        self.assertEqual(w.slot, mp.Slot("A", 0))
        self.assertEqual(w.bw_mhz, 32.0)
        self.assertAlmostEqual(w.df_mhz, 32 / 4096)
        self.assertAlmostEqual(w.if_mhz, 3000.0, places=6)
        self.assertEqual(w.set_keys, {"A0.if": "3000", "A0.bw": "32"})
        self.assertFalse(w.reverse)   # SAM45 の ch は IF の昇順（仮定 ascending）
        # sky = 46122 − IF: ch 1 は IF の低い端
        self.assertAlmostEqual(w.sky_hz[0], (46122.0 - (3000 - 2047 * 32 / 4096)) * 1e6, delta=1)
        self.assertLess(w.sky_hz[-1], w.sky_hz[0])
        args = p.set_args()
        self.assertIn("tint=0.01024", args)
        self.assertIn("fmt=f32", args)
        self.assertIn("D0.if=3000", args)

    def test_two_windows(self):
        k0_2 = 100000
        p = mp.plan(_obs(bw2=125.0, k0_2=k0_2, arrays2=(20, 21)).s)
        self.assertEqual(sorted(p.windows), [4, 5, 6, 7, 20, 21])
        w = p.windows[20]
        self.assertEqual(w.slot, mp.Slot("A", 1))
        self.assertEqual(w.bw_mhz, 128.0)
        req = mp.SAM45_IF_LO_MHZ + (k0_2 + 4096 * 8 / 2 - 0.5) * mp.SAM45_DK_MHZ
        self.assertLess(abs(w.if_mhz - req), 128 / 4096 / 2 + 1e-9)   # 格子に丸めた

    def test_outside_yatsugatake_band(self):
        # SAM45 の IF 2000〜2048 MHz は YATSUGATAKE に無い
        p = mp.plan(_obs(k0_1=0).s)
        self.assertEqual(p.windows, {})
        self.assertEqual(sorted(p.skipped), [4, 5, 6, 7])

    def test_descending_model_reverses(self):
        p = mp.plan(_obs().s, ifm=mp.Sam45IfModel(ascending=False))
        self.assertTrue(p.windows[4].reverse)


class TestSubarray(unittest.TestCase):
    def test_sub_index(self):
        o = _obs().s
        self.assertEqual(mp._sub_of(o, 4), 0)       # OFF は全部 0
        self.assertEqual(mp._sub_of(o, 12), 0)
        sl.set_cstr(o, "csub_array", "ON")
        for idx, sub in ((0, 0), (7, 0), (8, 1), (15, 1), (20, 0), (23, 0), (24, 1), (31, 1)):
            self.assertEqual(mp._sub_of(o, idx), sub, sl.array_label(idx))

    def test_on_uses_subarray_0_for_targets(self):
        rec = _obs()
        o = rec.s
        sl.set_cstr(o, "csub_array", "ON")
        # サブアレイ 1 に別の窓（帯域 125 MHz）を入れても、A05〜A08（サブアレイ 0）は 31.25 MHz のまま
        o.istart_chan[1][0], o.iend_chan[1][0], o.ichan_avg[1][0] = 0, 4096 * 16 - 1, 16
        o.in_spec_window[1] = 1
        p = mp.plan(o)
        self.assertEqual(sorted(p.windows), [4, 5, 6, 7])
        self.assertEqual(p.windows[4].bw_mhz, 32.0)


class TestWide(unittest.TestCase):
    def test_wide_1000(self):
        p = mp.plan(_obs(bw1=1000.0, k0_1=0).s)
        self.assertEqual(p.kind, "wide")
        w = p.windows[4]
        self.assertEqual(w.bw_mhz, 1024.0)
        self.assertAlmostEqual(w.df_mhz, 0.25)
        self.assertEqual(w.set_keys["A.b"], "2")
        # SAM45 の窓 2000〜3000 MHz の中心 2500 → S0 は範囲の端（IF 2048〜3072）に寄る
        ax = w.record_if_axis()
        self.assertGreaterEqual(ax[0], 2048.0)

    def test_wide_2000_one_window(self):
        p = mp.plan(_obs(bw1=2000.0, k0_1=0, bw2=2000.0, k0_2=0, arrays=(4,), arrays2=(20,)).s)
        self.assertIn(4, p.windows)
        self.assertEqual(p.windows[4].set_keys, {"A.b": "4", "A0.s0": "0"})
        self.assertIn(20, p.skipped)

    def test_mixed_takes_fine(self):
        p = mp.plan(_obs(bw1=1000.0, k0_1=0, bw2=62.5, k0_2=200000, arrays=(4,), arrays2=(20,)).s)
        self.assertEqual(p.kind, "fine")
        self.assertIn(20, p.windows)
        self.assertIn(4, p.skipped)

    def test_wide_axis_matches_yatsugatake_formula(self):
        # YATSUGATAKE の wide.py: IF = 4096 − (S0 + j + (B−1)/(2B))·fs/N、記録は j の逆順
        for bw, b in ((2000.0, 4), (1000.0, 2), (500.0, 1)):
            p = mp.plan(_obs(bw1=bw, k0_1=50000, arrays=(4,)).s)
            w = p.windows[4]
            s0 = int(w.set_keys["A0.s0"])
            n = 32768 // b
            j = np.arange(4096)
            if_stream = 4096 - (s0 + j + (b - 1) / (2 * b)) * 4096 / n
            np.testing.assert_allclose(w.record_if_axis(), if_stream[::-1], atol=1e-9)


class TestRewrite(unittest.TestCase):
    def test_rewrite_and_ifatt(self):
        rec = _obs()
        p = mp.plan(rec.s)
        fol = mp.rewrite_l1(rec, p, mp.IFATT_FOLLOW)
        ign = mp.rewrite_l1(rec, p, mp.IFATT_IGNORE)
        self.assertEqual(rec.s.dbebw[4], 31.25e6)            # 元は変えない
        self.assertEqual(fol.s.dbebw[4], 32e6)
        self.assertAlmostEqual(fol.s.dbechwid[4], 7812.5)
        self.assertEqual(fol.s.iary_ifatt[4], 5)
        self.assertEqual(ign.s.iary_ifatt[4], 0)
        w = p.windows[4]
        self.assertAlmostEqual(fol.s.dfqdat_fq[4][0], w.sky_hz[4095])  # ch 4096
        self.assertAlmostEqual(fol.s.dfqdat_fq[4][1], w.sky_hz[0])     # ch 1
        self.assertEqual(len(fol.to_bytes()), sl.SIZES["L1"])

    def test_restrict(self):
        rec = _obs()
        r = mp.restrict_arrays(rec, [4, 5])
        self.assertEqual([i for i in range(32) if r.s.iary_usefg[i]], [4, 5])
        self.assertEqual(r.s.iary_num, 2)


@unittest.skipUnless(SAMPLE, "SAM45_SAMPLE が無い")
class TestSample(unittest.TestCase):
    def test_sample_plan(self):
        recs = sl.read_file(SAMPLE)
        obs = recs[1]
        p = mp.plan(obs.s)
        self.assertEqual(p.kind, "fine")
        self.assertEqual(sorted(p.windows), [4, 5])          # A05・A06（A07・A08 は使っていない）
        w = p.windows[4]
        self.assertAlmostEqual(w.if_mhz, 3000.0, places=6)
        self.assertEqual(w.bw_mhz, 32.0)
        # SAM45 の軸の傾き: sky と IF は傾き −1（ch 1 が高い sky）
        s = mp.sam45_window(obs.s, 4, mp.Sam45IfModel())
        self.assertEqual(s.sky_b, -1.0)
        self.assertAlmostEqual(s.if_center_mhz, 3000.0, delta=0.01)
        # YATSUGATAKE の中心の sky は SAM45 の中心とほぼ同じ
        sam_center = s.sky_a + s.sky_b * s.if_center_mhz * 1e6
        self.assertAlmostEqual(sam_center, obs.s.dfqdat_f0[4], delta=1e-3)
        # YATSUGATAKE の記録の if_mhz（送る 2047 番目）は窓の中心 IF 3000 MHz
        self.assertAlmostEqual(w.sky_hz[2047], obs.s.dfqdat_f0[4] + (s.if_center_mhz - 3000.0) * 1e6, delta=1)
        # SAM45 の丸めた dfqdat（10 kHz）と、計算した端の sky の差は 10 kHz 以内
        self.assertLess(abs(s.sky_a + s.sky_b * s.if_of_ch[0] * 1e6 - obs.s.dfqdat_fq[4][1]), 10e3)
        out = mp.rewrite_l1(obs, p, mp.IFATT_FOLLOW)
        self.assertEqual(out.s.dbebw[13], obs.s.dbebw[13])   # A14 は触らない


if __name__ == "__main__":
    unittest.main()
