# SPDX-License-Identifier: BSD-3-Clause
"""lc（暫定 LC の本体）と tools/replay の試験。"""

import os
import sys
import tempfile
import unittest

from sam45compat import lc as lcm
from sam45compat import mapping as mp
from sam45compat import samlog as sl
from sam45compat.fake import FakeSpectrometer
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


class TestLC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.cfg = lcm.LcConfig(log_dir=self.dir, ready_timeout_s=5.0)
        self.recs = _sam45_records(_obs(arrays=range(8)), range(8))
        self.sam = os.path.join(self.dir, NAME)
        self.yksp = os.path.join(self.dir, NAME.replace(".SAM45.", ".YKSP."))

    def tearDown(self):
        self.tmp.cleanup()

    def _run_growing(self, lc, now=0.0):
        """SAM45 のロギングを 1 レコードずつ書き足しながら LC を回す。"""
        for k in range(1, len(self.recs) + 1):
            _write(self.sam, self.recs[:k])
            lc.step(now)
        lc.step(now)

    def test_whole_observation(self):
        spec = FakeSpectrometer()
        note = Notifier()
        lc = lcm.LC(self.cfg, spec, note)
        self._run_growing(lc)
        out = sl.read_file(self.yksp)
        self.assertEqual([r.kind for r in out], ["L0", "L1"] + ["L2"] * 16 + ["ED"])
        self.assertEqual(spec.calls[0][0], "prepare")
        self.assertEqual(spec.calls[0][2], "fine")
        self.assertIn("A0.if=3000", spec.calls[0][3])
        self.assertEqual(spec.calls[-1], ("finish", "20260101000000"))
        self.assertEqual(note.sent, [])
        self.assertEqual(lc.sessions, {})

    def test_version_log(self):
        """観測ごとに版を 1 行記録する（試験運用で、どの版で取ったかを後から引くため）。"""
        self.cfg.version_log = os.path.join(self.dir, "versions.tsv")
        lc = lcm.LC(self.cfg, FakeSpectrometer())
        self.assertIn("sam45compat", lc.software)
        self._run_growing(lc)
        with open(self.cfg.version_log) as f:
            rows = [x.rstrip("\n").split("\t") for x in f]
        self.assertEqual(rows[0][:4], ["t_start", "log_id", "kind", "sam45compat"])
        self.assertEqual(len(rows), 2)
        r = dict(zip(rows[0], rows[1]))
        self.assertEqual((r["log_id"], r["bit"], r["bit_usr"]), ("20260101000000", "fine", "-"))   # 模擬は ID が無い
        self.assertEqual(r["sam45compat"], lc.software["sam45compat"])
        v = lc.recent[0]["versions"]
        self.assertEqual(v["yatsugatake"], lc.software["yatsugatake"])

    def test_closes_integration_without_waiting_next(self):
        # 1 積分ぶんの L2（8 Array）が揃った時点で YKSP の L2 を書く（次の積分を待たない）
        lc = lcm.LC(self.cfg, FakeSpectrometer())
        _write(self.sam, self.recs[:2 + 8])    # L0・L1・ZERO の 8 個
        lc.step(0.0)
        self.assertEqual(sum(r.kind == "L2" for r in sl.read_file(self.yksp)), 4)
        lc.close()

    def test_waits_for_dumps_then_timeout(self):
        spec = FakeSpectrometer()
        spec.dumps().latest = None             # ダンプがまだ届いていない
        lc = lcm.LC(self.cfg, spec)
        _write(self.sam, self.recs[:2 + 16])   # ZERO と R
        lc.step(0.0)
        n = lambda: sum(r.kind == "L2" for r in sl.read_file(self.yksp))
        self.assertEqual(n(), 4)                # ZERO は待たない、R は待つ
        lc.step(1.0)
        self.assertEqual(n(), 4)
        lc.step(10.0)                           # 待ちすぎ → ある分で束ねる
        self.assertEqual(n(), 8)
        lc.close()

    def test_notifies_time_loss_once(self):
        # 観測の途中で分光計が時刻を失ったら、観測の終わりを待たずに 1 回だけ知らせる
        spec = FakeSpectrometer()
        note = Notifier()
        lc = lcm.LC(self.cfg, spec, note)
        _write(self.sam, self.recs[:2 + 8])    # ZERO まで
        lc.step(0.0)
        self.assertEqual(note.sent, [])
        spec.dumps().n_notime = 3
        self._run_growing(lc)
        self.assertEqual(sum("時刻を失った" in t for t in note.sent), 1)

    def test_prepare_failure_passes_through(self):
        note = Notifier()
        lc = lcm.LC(self.cfg, FakeSpectrometer(fail="ボードが応答しない"), note)
        self._run_growing(lc)
        out = sl.read_file(self.yksp)
        l2 = [r for r in out if r.kind == "L2"]
        self.assertEqual(len(l2), 16)
        self.assertTrue(all(r.s.idmy_flag == 1 for r in l2))
        self.assertEqual(sum("準備に失敗" in t for t in note.sent), 1)
        self.assertEqual(out[-1].kind, "ED")

    def test_skip_existing_and_other_files(self):
        _write(self.sam, self.recs)
        open(os.path.join(self.dir, "readme.txt"), "w").close()
        lc = lcm.LC(self.cfg, FakeSpectrometer())
        lc.step(0.0)
        self.assertFalse(os.path.exists(self.yksp))   # 起動前からあった観測は扱わない

    def test_prefix_for_stage2_input(self):
        cfg = lcm.LcConfig(log_dir=self.dir, in_prefix="_")
        lc = lcm.LC(cfg, FakeSpectrometer())
        _write(os.path.join(self.dir, NAME), self.recs)          # 頭の無いものは見ない
        _write(os.path.join(self.dir, "_" + NAME), self.recs)
        lc.step(0.0)
        lc.step(0.0)
        self.assertTrue(os.path.exists(self.yksp))

    def test_config_json(self):
        p = os.path.join(os.path.dirname(__file__), "..", "config", "example.json")
        cfg = lcm.LcConfig.from_json(p)
        self.assertEqual(cfg.assign["A21"], mp.Slot("A", 1))
        self.assertEqual(cfg.ifatt_mode, "follow")


class TestBklogLayout(unittest.TestCase):
    """45m の bklog の形: 名前は '<装置>.<指示書>.<group>.<project>.<ログ ID>'、PSW と OTF で置き場所が分かれる。"""

    def test_name_id_last(self):
        n = sl.LogName.parse("SAM45.nmlh40.csv26.squint.20261108213141")
        self.assertEqual((n.log_id, n.backend, n.obs_file, n.group, n.project, n.id_last),
                         ("20261108213141", "SAM45", "nmlh40", "csv26", "squint", True))
        self.assertEqual(str(n.with_backend("YKSP")), "YKSP.nmlh40.csv26.squint.20261108213141")
        self.assertEqual(str(n.with_prefix("_")), "_SAM45.nmlh40.csv26.squint.20261108213141")
        self.assertEqual(sl.LogName.parse("_SAM45.a.b.c.20261108213141").prefix, "_")
        self.assertFalse(sl.LogName.parse(NAME).id_last)
        with self.assertRaises(ValueError):
            sl.LogName.parse("SAM45.nmlh40.csv26.squint.2026110821314")

    def test_two_dirs_and_mirror(self):
        with tempfile.TemporaryDirectory() as d:
            dirs = {k: os.path.join(d, k) for k in ("sam45psw", "sam45otf", "yksppsw", "ykspotf")}
            for k in ("sam45psw", "sam45otf"):
                os.makedirs(dirs[k])
            cfg = lcm.LcConfig(watch=[[dirs["sam45psw"], dirs["yksppsw"]], [dirs["sam45otf"], dirs["ykspotf"]]],
                               mirror={"A05": "A01", "A06": "A02", "A07": "A03", "A08": "A04"})
            lc = lcm.LC(cfg, FakeSpectrometer())
            # SAM45 のロギングには A01〜A04 だけ（A05〜A08 は含まれない。2026-11-10）
            recs = _sam45_records(_obs(arrays=range(4)), range(4))
            for r in recs:
                if r.kind == "L2":
                    r.s.dweather[1] = 900.0 + r.s.iary_no
            _write(os.path.join(dirs["sam45otf"], "SAM45.otfobs.grp.prj.20261110000001"), recs)
            with open(os.path.join(dirs["sam45psw"], "README"), "w") as f:
                f.write("x")                                   # ロギングでない名前は見ない
            lc.step(0.0)
            lc.step(0.0)
            self.assertEqual(os.listdir(d) and sorted(os.listdir(dirs["ykspotf"])),
                             ["YKSP.otfobs.grp.prj.20261110000001"])
            self.assertFalse(os.path.exists(dirs["yksppsw"]))
            out = sl.read_file(os.path.join(dirs["ykspotf"], "YKSP.otfobs.grp.prj.20261110000001"))
            self.assertEqual([r.kind for r in out], ["L0", "L1"] + ["L2"] * 16 + ["ED"])
            self.assertEqual([i for i in range(32) if out[1].s.iary_usefg[i]], [4, 5, 6, 7])
            self.assertEqual(out[1].s.dbebw[4], 32e6)
            self.assertTrue(all(r.s.idmy_flag == 0 for r in out if r.kind == "L2"))
            # L2 の付随の欄は、埋めた元の Array（A05 なら A01）の L2 から
            l2 = [r.s for r in out if r.kind == "L2"]
            self.assertEqual([(sl.cstr(x.cary_name), x.dweather[1]) for x in l2[:4]],
                             [("A5", 941.0), ("A6", 942.0), ("A7", 943.0), ("A8", 944.0)])
            self.assertEqual(lc.sessions, {})

    def test_mirror_second_window(self):
        # 2 つ目の窓: A21〜A24 は A17〜A20 から（SAM45 のロギングには A01〜A04・A17〜A20 だけ。2026-11-10）
        obs = _obs(bw1=31.25, bw2=31.25, k0_2=262144, arrays=range(4), arrays2=range(16, 20))
        mirror = {"A05": "A01", "A06": "A02", "A07": "A03", "A08": "A04",
                  "A21": "A17", "A22": "A18", "A23": "A19", "A24": "A20"}
        self.assertEqual(lcm.mirror_sources(obs, mirror),
                         {4: 0, 5: 1, 6: 2, 7: 3, 20: 16, 21: 17, 22: 18, 23: 19})
        o = lcm.mirror_arrays(obs, mirror).s
        self.assertEqual([i for i in range(32) if o.iary_usefg[i]], [0, 1, 2, 3, 4, 5, 6, 7, 16, 17, 18, 19, 20, 21, 22, 23])
        self.assertEqual(o.iary_num, 16)
        self.assertEqual(o.dfqdat_f0[20], obs.s.dfqdat_f0[16])
        self.assertEqual(obs.s.iary_usefg[20], 0)                       # 元は変えない
        plan = mp.plan(o)
        self.assertEqual(sorted(plan.windows), [4, 5, 6, 7, 20, 21, 22, 23])
        # SAM45 のロギングに含まれていれば、その設定を使う（埋めない）
        obs2 = _obs(arrays=range(8))
        self.assertEqual(lcm.mirror_sources(obs2, mirror), {})

    def test_nch_2048_two_windows(self):
        """2026-11-12 の観測と同じ形: サブアレイ分割 ON・2 窓・62.5 MHz を 2048 ch。SAM45 のロギングは A01〜A04・A17〜A20 だけ。"""
        import numpy as np
        obs = _obs(bw1=62.5, k0_1=217024, bw2=62.5, k0_2=413824, arrays=range(4), arrays2=range(16, 20), nch=2048)
        o = obs.s
        sl.set_cstr(o, "csub_array", "ON")
        o.in_spec_window[1] = 2
        for spw in range(2):
            o.istart_chan[1][spw], o.iend_chan[1][spw], o.ichan_avg[1][spw] = \
                o.istart_chan[0][spw], o.iend_chan[0][spw], o.ichan_avg[0][spw]
        mirror = {"A05": "A01", "A06": "A02", "A07": "A03", "A08": "A04",
                  "A21": "A17", "A22": "A18", "A23": "A19", "A24": "A20"}
        tmp = tempfile.TemporaryDirectory()
        cfg = lcm.LcConfig(log_dir=tmp.name, ready_timeout_s=5.0, mirror=mirror,
                           ql_path=os.path.join(tmp.name, "ql.json"), ql_s=3600)
        lc = lcm.LC(cfg, FakeSpectrometer())
        try:
            recs = _sam45_records(obs, list(range(4)) + list(range(16, 20)), scans=("ZERO", "R", "SKY", "OFF", "ON"))
            sam = os.path.join(tmp.name, NAME)
            for k in range(1, len(recs) + 1):
                _write(sam, recs[:k])
                lc.step(0.0)
            lc.step(0.0)
            out = sl.read_file(os.path.join(tmp.name, NAME.replace(".SAM45.", ".YKSP.")))
            l1 = out[1].s
            arrays = [4, 5, 6, 7, 20, 21, 22, 23]
            self.assertEqual([i for i in range(32) if l1.iary_usefg[i]], arrays)
            self.assertEqual({l1.ichannel[i] for i in arrays}, {2048})
            self.assertAlmostEqual(l1.dbechwid[4], 64e6 / 2048)
            l2 = [r.s for r in out if r.kind == "L2"]
            self.assertEqual(len(l2), 5 * 8)
            self.assertTrue(all(x.idmy_flag == 0 for x in l2))
            for x in l2:
                d = np.frombuffer(bytes(x.fary_data), dtype="<f4")
                self.assertTrue(np.all(d[:2048] != 0) and np.all(d[2048:] == 0))    # 前の 2048 個に詰め、残りは 0（SAM45 と同じ）
            self.assertEqual(lc.recent[0]["arrays"], ["A05", "A06", "A07", "A08", "A21", "A22", "A23", "A24"])
            lc.ql_writer.write()
            import json
            with open(cfg.ql_path) as f:
                it = json.load(f)["integration"]
            w0 = it["windows"][0]
            self.assertEqual((it["n_on"], len(it["windows"]), len(w0["spec"]), w0["nch"]), (1, 8, 1024, 2048))
        finally:
            lc.close()
            tmp.cleanup()

    def test_unreadable_dir_does_not_stop(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = lcm.LcConfig(watch=[[os.path.join(d, "none"), os.path.join(d, "out")]])
            lc = lcm.LC(cfg, FakeSpectrometer())
            lc.step(0.0)                                       # 置き場所が無くても落ちない
            self.assertEqual(lc.sessions, {})
            # 起動の後にマウントされた: そのときにあるものは扱わず、その後にできたものだけを扱う
            os.makedirs(os.path.join(d, "none"))
            recs = _sam45_records(_obs(arrays=range(8)), range(8))
            _write(os.path.join(d, "none", "SAM45.old.grp.prj.20261110000001"), recs)
            lc.step(0.0)
            self.assertFalse(os.path.exists(os.path.join(d, "out", "YKSP.old.grp.prj.20261110000001")))
            _write(os.path.join(d, "none", "SAM45.new.grp.prj.20261110000002"), recs)
            lc.step(0.0)
            lc.step(0.0)
            self.assertTrue(os.path.exists(os.path.join(d, "out", "YKSP.new.grp.prj.20261110000002")))


@unittest.skipUnless(SAMPLE, "SAM45_SAMPLE が無い")
class TestReplaySample(unittest.TestCase):
    def test_replay_through_lc(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = lcm.LcConfig(log_dir=d)
            spec = FakeSpectrometer()
            lc = lcm.LC(cfg, spec)
            path = replay.replay(SAMPLE, d, speed=0, log_id="20261106120000",
                                 on_chunk=lambda p: lc.step(0.0))
            lc.step(0.0)
            yname = os.path.join(d, os.path.basename(path).replace(".SAM45.", ".YKSP."))
            out = sl.read_file(yname)
            self.assertEqual(sum(r.kind == "L2" for r in out), 52 * 2)
            self.assertEqual(out[-1].kind, "ED")
            sam = sl.read_file(path)
            with open(SAMPLE, "rb") as f:
                self.assertEqual(b"".join(r.to_bytes() for r in sam), f.read())


if __name__ == "__main__":
    unittest.main()
