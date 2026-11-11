# SPDX-License-Identifier: BSD-3-Clause
"""status（状態のページの JSON）の試験。模擬の分光計で LC を回し、書いた JSON を読む。

ボード（specd）への問い合わせは test_board.py。
"""

import json
import os
import tempfile
import time
import unittest

from sam45compat import lc as lcm
from sam45compat import status as stm
from sam45compat.fake import FakeSpectrometer
from sam45compat.notify import Notifier
from test_lc import NAME, _write
from test_mapping import _obs
from test_yksp import _sam45_records


class _Busy:
    """status_nowait を持つ分光計の代わり（返す値を決められる）。"""

    def __init__(self, ret=None, exc=None):
        self.ret, self.exc = ret, exc

    def status_nowait(self, timeout):
        if self.exc:
            raise self.exc
        return self.ret


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.path = os.path.join(self.dir, "status.json")
        self.recs = _sam45_records(_obs(arrays=range(8)), range(8))
        self.sam = os.path.join(self.dir, NAME)

    def tearDown(self):
        self.tmp.cleanup()

    def _read(self):
        with open(self.path) as f:
            return json.load(f)

    def test_observation_and_recent(self):
        cfg = lcm.LcConfig(log_dir=self.dir, ready_timeout_s=5.0)   # status_path なし: スレッドは起こさない
        lc = lcm.LC(cfg, FakeSpectrometer(), Notifier())
        self.assertIsNone(lc.status_writer)
        w = stm.StatusWriter(lc, self.path)
        # 観測の途中（最後の積分と ED の前）
        for k in range(1, len(self.recs) - 3):
            _write(self.sam, self.recs[:k])
            lc.step(0.0)
        w.write()
        d = self._read()
        self.assertEqual(d["version"], stm.VERSION)
        self.assertLess(abs(d["t"] - time.time()), 5)
        self.assertEqual(d["board"], {"mode": "fake"})
        (s,) = d["lc"]["sessions"]
        self.assertEqual(s["log_id"], "20260101000000")
        self.assertEqual(s["phase"], "記録中")
        self.assertEqual(s["bit"], "fine")
        self.assertEqual(s["arrays"], ["A05", "A06", "A07", "A08"])
        self.assertGreater(s["n_l2"], 0)
        self.assertEqual(d["lc"]["recent"], [])
        win = d["lc"]["windows"]
        self.assertEqual((win["log_id"], win["current"], win["bit"]), ("20260101000000", True, "fine"))
        self.assertEqual([(x["adc"], x["stream"], x["array"]) for x in win["windows"]],
                         [("A", 0, "A05"), ("B", 0, "A06"), ("C", 0, "A07"), ("D", 0, "A08")])
        x = win["windows"][0]
        self.assertAlmostEqual(x["if_mhz"], 3000.0, delta=1.0)               # _obs の既定（A0.if=3000）
        self.assertLess(x["if_lo_mhz"], x["if_mhz"])
        self.assertLess(x["if_mhz"], x["if_hi_mhz"])
        self.assertLess(x["sky_lo_ghz"], x["sky_hi_ghz"])
        self.assertIn("A0.if=", x["set"])
        self.assertTrue(d["lc"]["dirs"][0]["readable"])
        self.assertEqual(d["lc"]["disks"][0]["path"], self.dir)
        # 終わり
        _write(self.sam, self.recs)
        lc.step(0.0)
        lc.step(0.0)
        w.write()
        d = self._read()
        self.assertEqual(d["lc"]["sessions"], [])
        (r,) = d["lc"]["recent"]
        self.assertEqual((r["phase"], r["n_l2"], r["n_invalid"]), ("終わり", 16, 0))
        self.assertEqual((d["lc"]["windows"]["log_id"], d["lc"]["windows"]["current"]), ("20260101000000", False))
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_prepare_failure_and_notices(self):
        cfg = lcm.LcConfig(log_dir=self.dir, ready_timeout_s=5.0)
        lc = lcm.LC(cfg, FakeSpectrometer(fail="ボードが応答しない"), Notifier())
        _write(self.sam, self.recs[:2])
        lc.step(0.0)
        d = stm.StatusWriter(lc, self.path).snapshot()
        self.assertEqual(d["lc"]["sessions"][0]["phase"], "準備に失敗")
        self.assertTrue(any("準備に失敗" in n["subject"] for n in d["lc"]["notices"]))
        lc.close()
        for x in lc.sessions.values():
            x.close()

    def test_thread_writes(self):
        cfg = lcm.LcConfig(log_dir=self.dir, status_path=self.path, status_s=0.05)
        lc = lcm.LC(cfg, FakeSpectrometer(), Notifier())
        try:
            t_e = time.time() + 5
            while not os.path.exists(self.path) and time.time() < t_e:
                time.sleep(0.02)
            t1 = self._read()["t"]
            time.sleep(0.2)
            self.assertGreater(self._read()["t"], t1)        # 書き直し続ける
        finally:
            lc.close()

    def test_missing_dir_does_not_stop_lc(self):
        cfg = lcm.LcConfig(log_dir=self.dir, status_path=os.path.join(self.dir, "no", "status.json"))
        with self.assertLogs("sam45compat", "ERROR"):
            lc = lcm.LC(cfg, FakeSpectrometer(), Notifier())
        lc.step(0.0)
        lc.close()

    def test_board_poller(self):
        p = stm.BoardPoller(_Busy(ret={"state": "RUN"}))
        p.poll()
        self.assertEqual((p.status, p.error, p.busy), ({"state": "RUN"}, None, False))
        t_ok = p.t_ok
        p.spec = _Busy(ret=None)                  # LC が制御の口を使っている: 前の値を残す
        p.poll()
        self.assertEqual((p.status, p.busy, p.t_ok), ({"state": "RUN"}, True, t_ok))
        p.spec = _Busy(exc=OSError("Connection refused"))
        p.poll()
        s = p.snapshot()
        self.assertEqual((s["mode"], s["error"], s["busy"]), ("board", "Connection refused", False))
        self.assertEqual(s["status"], {"state": "RUN"})    # 最後に取れた値（t_ok で古さがわかる）


if __name__ == "__main__":
    unittest.main()
