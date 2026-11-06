# SPDX-License-Identifier: BSD-3-Clause
"""compare（段階 1 の比較）の試験。実際のロギング（SAM45_SAMPLE）を使う。"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import numpy as np

from sam45compat import binning as bn
from sam45compat import compare as cp
from sam45compat import mapping as mp
from sam45compat import samlog as sl
from sam45compat import yksp as yk

SAMPLE = os.environ.get("SAM45_SAMPLE")


class ResampledDumps:
    """SAM45 の A05 のスペクトル（ZERO を引いたもの）を、YKSP の窓の軸に補間して返す模擬のダンプ。
    同じ信号を YATSUGATAKE で受けたときの理想の形（帯域の形・量子化の違いは無い）。"""

    def __init__(self, sam_records, plan, scale=1.0):
        obs = sam_records[1].s
        _, d = cp.collect(sam_records, [4])
        self.d = d[4]
        self.zero = cp.zero_level(self.d)
        self.ax = cp.sky_axis_sam45(obs, 4)
        self.order = np.argsort(self.ax)
        self.idx = self.d.by_key()
        self.plan = plan
        self.scale = scale

    def interval(self, key, st, ed):
        w = next(w for w in self.plan.windows.values() if w.slot.key == key)
        k = next(k for k in self.idx if yk.Integration(k).st_ns == st and yk.Integration(k).ed_ns == ed)
        s = self.d.spec[self.idx[k]] - self.zero
        l2 = np.interp(w.sky_hz, self.ax[self.order], s[self.order])     # L2 の ch の順
        rec = l2[::-1] if w.reverse else l2                              # 記録（IF の昇順）に戻す
        n = (ed - st) // bn.TINT_NS
        return bn.Binned(rec * self.scale, n, n)

    def latest_end_ns(self):
        return 2 ** 62


@unittest.skipUnless(SAMPLE, "SAM45_SAMPLE が無い")
class TestCompareSample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sam = sl.read_file(SAMPLE)

    def test_same_array_is_identical(self):
        # 同じファイル・同じ Array（右側も ZERO を引かないので、値の比は ZERO の分だけ 1 からずれる）
        r = cp.compare(self.sam, self.sam, {"A05": "A05"})[0]
        self.assertEqual(r.metrics["n_common"], 52)
        self.assertAlmostEqual(r.metrics["center_l_hz"], 43.122e9, delta=1e3)

    def test_resampled_yksp_matches(self):
        """SAM45 の A05 を YKSP の軸に補間して YKSP のロギングを作り、比べると一致する（軸の向き・中心・較正の確かめ）。"""
        plan = mp.plan(self.sam[1].s)
        dumps = ResampledDumps(self.sam, plan, scale=1000.0)
        b = yk.YkspBuilder(plan, mp.IFATT_FOLLOW, dumps)
        yksp, _ = yk.build_offline(self.sam, b)
        r = cp.compare(self.sam, yksp, {"A05": "A05"})[0]
        m = r.metrics
        # ch の並びの真ん中どうしの差は 1 ch 以内（YKSP の窓の中心は送る 2047 番目の ch で、真ん中から半 ch ずれる）
        self.assertLess(abs(m["center_r_hz"] - m["center_l_hz"]), m["df_r_hz"])
        self.assertAlmostEqual(m["df_r_hz"], 7812.5)
        self.assertAlmostEqual(m["ratio_ON"], 1000.0, delta=5.0)            # 帯域が 2.4 % 広いぶんだけ平均がずれうる
        self.assertLess(m["ratio_ON_cv"], 0.01)
        self.assertAlmostEqual(m["y_r"], m["y_l"], delta=0.02 * m["y_l"])
        self.assertAlmostEqual(m["tsys_calc_r"], m["tsys_calc_l"], delta=0.02 * m["tsys_calc_l"])
        self.assertAlmostEqual(m["tsys_file_r"], m["tsys_calc_r"], delta=0.01)
        # 補間で分解能が少し鈍るので、差の rms は ch の雑音の程度
        self.assertLess(m["ta_diff_rms_k"], 2.0 * m["ta_noise_l_k"])
        self.assertLess(abs(m["ta_diff_mean_k"]), m["ta_noise_l_k"])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with redirect_stdout(out):
                cp.main([SAMPLE, SAMPLE, "--pair", "A05:A06", "--npz", os.path.join(d, "c.npz"),
                         "--png", os.path.join(d, "c.png")])
            text = out.getvalue()
            self.assertIn("A05（SAM45）/ A06（YKSP）", text)
            self.assertIn("Ta*", text)
            z = np.load(os.path.join(d, "c.npz"))
            self.assertIn("A05_ta", z.files)


if __name__ == "__main__":
    unittest.main()
