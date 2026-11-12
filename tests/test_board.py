# SPDX-License-Identifier: BSD-3-Clause
"""board（specd の制御）と recorder（データの口）の試験。

YATSUGATAKE の specd を PL なし（--fake）で起こして相手にする。specd の --fake は SAM45-Fine の形の記録を、
本物と同じ間隔（tint）と今の時刻で作る。bit は "default" の 1 つだけ。
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import numpy as np

from sam45compat import lc as lcm
from sam45compat import mapping as mp
from sam45compat import samlog as sl
from sam45compat.board import BoardConfig, BoardSpectrometer
from sam45compat.notify import Notifier
from sam45compat.recorder import F_AXIS
from test_mapping import _obs

PYNQ = os.path.join(os.path.dirname(__file__), "..", "external", "YATSUGATAKE", "sw", "pynq")
T = 10_240_000


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(os.path.exists(os.path.join(PYNQ, "specd.py")), "submodule が無い")
class TestBoardFake(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cport, cls.dport = _free_port(), _free_port()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.log = open(os.path.join(cls.tmp.name, "specd.log"), "w")
        cls.proc = subprocess.Popen(
            [sys.executable, "specd.py", "--fake", "--cpu", "-1", "--rt", "0",
             "--ctrl-port", str(cls.cport), "--data-port", str(cls.dport)],
            cwd=PYNQ, stdout=cls.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.cport), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            cls.proc.kill()
            raise unittest.SkipTest("specd --fake が起きない")
        cfg = BoardConfig(host="127.0.0.1", ctl_port=cls.cport, data_port=cls.dport,
                          bits={"fine": "default", "wide": "default"},
                          tmp_dir=os.path.join(cls.tmp.name, "dumps"))
        cls.spec = BoardSpectrometer(cfg)

    @classmethod
    def tearDownClass(cls):
        cls.spec.close()
        cls.proc.terminate()
        cls.proc.wait(10)
        cls.log.close()
        cls.tmp.cleanup()

    def _wait(self, cond, timeout=10.0):
        t_e = time.time() + timeout
        while time.time() < t_e:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_1_prepare_record_bin(self):
        plan = mp.plan(_obs(bw2=125.0, k0_2=100000, arrays2=(20, 21)).s)
        self.spec.prepare(plan, "20261106000001")
        cmds = self.spec.commands
        self.assertNotIn("LOAD bit=default", cmds)          # 今の bit と同じなら載せ替えない
        self.assertIn("ID", cmds)                            # 版の記録のため、観測ごとに ID を読む
        self.assertEqual(self.spec.ident["bit"], "default")
        self.assertIn("version", self.spec.ident)
        self.assertTrue(any(c.startswith("SET ") and "A0.if=3000" in c and "A1.bw=128" in c for c in cmds))
        st = self.spec.dumps()
        self.assertTrue(self._wait(lambda: st.latest_end_ns() is not None and st.counts()["A0"] > 150))
        self.assertEqual(sorted(st.counts()), ["A0", "A1", "B0", "B1", "C0", "D0"])
        # 今から 1.5〜0.5 秒前の 1 秒を束ねる（≒ 98 ダンプ）
        now = time.time_ns()
        b = st.interval("A0", now - 1_500_000_000, now - 500_000_000)
        self.assertGreaterEqual(b.n, 96)
        self.assertGreaterEqual(b.coverage, 0.99)
        self.assertEqual(b.flags & F_AXIS, 0)                # 軸は予定どおり
        self.assertEqual(b.spec.shape, (4096,))
        self.assertEqual(st.n_axis_bad, 0)
        path = st.path
        self.spec.finish("20261106000001")
        self.assertFalse(os.path.exists(path))               # 一時ファイルは消える

    def test_3_status_nowait(self):
        """状態のページの問い合わせ: LC の命令が制御の口を使っている間は待たずに飛ばす。人がつないでいれば ERR BUSY。"""
        from sam45compat.board import Control, SpecdError
        st = self.spec.status_nowait()
        self.assertIn(st["state"], ("IDLE", "RUN", "ARMED"))
        self.assertIn("tp_dbm", st)
        with self.spec._lock:
            self.assertIsNone(self.spec.status_nowait())
        c = Control("127.0.0.1", self.cport)       # ほかの人の接続（specctl など）
        try:
            with self.assertRaises(SpecdError):
                self.spec.status_nowait()
        finally:
            c.close()
        self.assertIsNotNone(self.spec.status_nowait())
        info = self.spec.recorder_info()
        self.assertIn("connected", info)
        self.assertIn("n_int", info["ref"])

    def test_2_lc_end_to_end(self):
        """暫定 LC ＋ specd --fake: 今の時刻の積分を持つ SAM45 のロギングを、積分が終わるたびに書き足す。"""
        with tempfile.TemporaryDirectory() as d:
            cfg = lcm.LcConfig(log_dir=d, ready_timeout_s=3.0)
            note = Notifier()
            lc = lcm.LC(cfg, self.spec, note)
            obs = _obs(arrays=range(8))
            ctl = sl.new_record("L0")
            name = os.path.join(d, "20261106000002.SAM45.obs.grp.prj")
            recs = [ctl, obs]
            with open(name, "wb") as f:
                for r in recs:
                    f.write(r.to_bytes())
            lc.step()
            t0 = int(time.time()) + 1                         # 次の秒の境界から 1 秒の積分を 4 つ
            unix0 = 40587 * 86400
            for n, sc in enumerate(("ZERO", "R", "SKY", "ON")):
                st, ed = t0 + n, t0 + n + 1
                while time.time() < ed + 0.2:
                    lc.step()
                    time.sleep(0.05)
                with open(name, "ab") as f:
                    for a in range(8):
                        r = sl.new_record("L2")
                        sl.set_cstr(r.s, "cary_name", sl.array_logname(a))
                        sl.set_cstr(r.s, "cscan_type", sc)
                        sl.set_cstr(r.s, "cscanid", f"x.{n:05d}")
                        r.s.dint_stmjd, r.s.dint_edmjd = st + unix0, ed + unix0
                        f.write(r.to_bytes())
                lc.step()
            with open(name, "ab") as f:
                f.write(sl.new_record("ED").to_bytes())
            for _ in range(20):
                lc.step()
                if not lc.sessions:
                    break
                time.sleep(0.1)
            out = sl.read_file(name.replace(".SAM45.", ".YKSP."))
            l2 = [r for r in out if r.kind == "L2"]
            self.assertEqual(len(l2), 16)
            self.assertEqual(out[-1].kind, "ED")
            self.assertTrue(all(r.s.idmy_flag == 0 for r in l2), note.sent)
            on = [r for r in l2 if sl.cstr(r.s.cscan_type) == "ON"][0]
            self.assertGreater(float(np.mean(on.s.fary_data)), 0)
            self.assertEqual(note.sent, [])


if __name__ == "__main__":
    unittest.main()
