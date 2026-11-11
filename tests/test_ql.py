# SPDX-License-Identifier: BSD-3-Clause
"""ql（QL の JSON）の試験。"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np

from sam45compat import lc as lcm
from sam45compat import mapping as mp
from sam45compat import ql
from sam45compat.fake import FakeSpectrometer
from sam45compat.notify import Notifier
from test_lc import NAME, _write
from test_mapping import _obs
from test_yksp import _sam45_records


def _res(array, spec, tsys=0.0, valid=True):
    return SimpleNamespace(array=array, valid=valid, spec=None if spec is None else np.asarray(spec, float),
                           record=SimpleNamespace(s=SimpleNamespace(dtsys=tsys)))


class TestIntegrationQl(unittest.TestCase):
    def setUp(self):
        self.plan = mp.plan(_obs().s)          # A05〜A08（添字 4〜7）
        self.iq = ql.IntegrationQl("20260101000000", self.plan, nbin=1024)

    def test_ta(self):
        off = np.full(4096, 100.0)
        on = off.copy()
        on[2048:2052] = 110.0                  # 線: (110 − 100) / 100 = 0.1
        self.iq.add("ON", "x.1", [_res(4, on, 300.0)])        # OFF の前の ON は使えない
        self.assertEqual(self.iq.ta, {})
        self.iq.add("OFF", "x.2", [_res(4, off, 300.0), _res(5, off, 0.0)])
        self.iq.add("ON", "x.3", [_res(4, on, 300.0), _res(5, on, 0.0), _res(6, on, 300.0)])
        self.assertEqual(self.iq.n_on, 1)
        a = self.iq.ta[4]
        self.assertEqual((a["unit"], a["tsys"], a["scan_id"]), ("K", 300.0, "x.3"))
        self.assertEqual(len(a["spec"]), 1024)
        self.assertAlmostEqual(max(a["spec"]), 30.0)          # 0.1 × 300 K（4 ch を 1 点に）
        self.assertEqual(min(a["spec"]), 0.0)
        b = self.iq.ta[5]                                     # Tsys がまだ無い: 比のまま
        self.assertEqual((b["unit"], b["tsys"]), ("ratio", None))
        self.assertAlmostEqual(max(b["spec"]), 0.1)
        self.assertNotIn(6, self.iq.ta)                       # OFF の無い窓
        snap = self.iq.snapshot()
        self.assertEqual([w["array"] for w in snap], ["A05", "A06", "A07", "A08"])
        self.assertIsNone(snap[2]["spec"])
        self.assertIn("sky0_ghz", snap[0])

    def test_skips_invalid_and_other_scans(self):
        off = np.full(4096, 100.0)
        self.iq.add("OFF", "x.1", [_res(4, off, valid=False)])
        self.iq.add("R", "x.2", [_res(4, off)])
        self.iq.add("ON", "x.3", [_res(4, off)])
        self.assertEqual(self.iq.ta, {})
        self.assertEqual(self.iq.last_scan, "ON")

    def test_reduce(self):
        self.assertEqual(ql.reduce(np.arange(8.0), 4), [0.5, 2.5, 4.5, 6.5])
        self.assertEqual(ql.reduce([1.234567, float("nan")], 0), [1.2346, None])


class TestQlWriter(unittest.TestCase):
    def test_through_lc(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ql.json")
            cfg = lcm.LcConfig(log_dir=d, ready_timeout_s=5.0, ql_path=path, ql_s=3600)
            lc = lcm.LC(cfg, FakeSpectrometer(), Notifier())
            try:
                recs = _sam45_records(_obs(arrays=range(8)), range(8), scans=("ZERO", "R", "SKY", "OFF", "ON"))
                sam = os.path.join(d, NAME)
                for k in range(1, len(recs) - 9):             # ON の L2 の途中まで（観測中）
                    _write(sam, recs[:k])
                    lc.step(0.0)
                lc.ql_writer.write()
                with open(path) as f:
                    j = json.load(f)
                self.assertEqual(j["log_id"], "20260101000000")
                rt = j["realtime"]
                self.assertEqual([w["array"] for w in rt["windows"]], ["A05", "A06", "A07", "A08"])
                self.assertEqual(len(rt["windows"][0]["spec"]), 1024)
                self.assertGreater(rt["windows"][0]["n"], 0)
                self.assertTrue(j["integration"]["current"])
                # 終わり: 生のスペクトルは消え、Ta* は残る（模擬は ON と OFF が同じなので 0）
                _write(sam, recs)
                lc.step(0.0)
                lc.step(0.0)
                lc.ql_writer.write()
                with open(path) as f:
                    j = json.load(f)
                self.assertIsNone(j["realtime"])
                it = j["integration"]
                self.assertEqual((it["log_id"], it["current"], it["n_on"]), ("20260101000000", False, 1))
                w0 = it["windows"][0]
                self.assertEqual((w0["unit"], w0["tsys"]), ("K", w0["tsys"]))
                self.assertEqual(max(abs(v) for v in w0["spec"]), 0.0)
            finally:
                lc.close()


if __name__ == "__main__":
    unittest.main()
