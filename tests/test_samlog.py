# SPDX-License-Identifier: BSD-3-Clause
"""samlog の試験。

実際のロギングを使う試験は、環境変数 SAM45_SAMPLE にファイルの場所を入れたときだけ走る
（サンプルはリポジトリに入れない）。
"""

import ctypes as C
import datetime as dt
import os
import tempfile
import unittest

from sam45compat import samlog as sl

SAMPLE = os.environ.get("SAM45_SAMPLE")


def _synthetic(n_ary=(4, 5), n_int=3, with_end=True):
    """模擬のロギング（L0・L1・L2 × n_int × Array・ED）。"""
    recs = []
    ctl = sl.new_record("L0")
    sl.set_cstr(ctl.s, "cversion", "V004.1")
    sl.set_cstr(ctl.s, "clog_name", "SAM45.obs.grp.prj.20260101000000")
    sl.set_cstr(ctl.s, "cbe_type", "SAM45")
    recs.append(ctl)
    obs = sl.new_record("L1")
    for a in n_ary:
        obs.s.iary_usefg[a] = 1
        obs.s.ichannel[a] = 4096
    obs.s.iary_num = len(n_ary)
    recs.append(obs)
    for k in range(n_int):
        for a in n_ary:
            d = sl.new_record("L2")
            sl.set_cstr(d.s, "cary_name", sl.array_logname(a))
            d.s.iary_no = 41 + a
            d.s.dint_stmjd = 5.0e9 + 5 * k
            d.s.dint_edmjd = 5.0e9 + 5 * k + 5
            for i in range(0, 4096, 512):
                d.s.fary_data[i] = k * 100 + a + i
            recs.append(d)
    if with_end:
        recs.append(sl.new_record("ED"))
    return recs


class TestLayout(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(sl.SIZES, {"L0": 88, "L1": 8472, "L2": 17064, "ED": 8})

    def test_new_record_head(self):
        r = sl.new_record("L2")
        raw = r.to_bytes()
        self.assertEqual(raw[:4], b"L2\0\0")
        self.assertEqual(int.from_bytes(raw[4:8], "little"), 17056)
        self.assertEqual(sl.new_record("ED").s.head.irec_len, 0)


class TestRoundTrip(unittest.TestCase):
    def test_synthetic(self):
        recs = _synthetic()
        buf = b"".join(r.to_bytes() for r in recs)
        back = list(sl.iter_records(buf))
        self.assertEqual([r.kind for r in back], [r.kind for r in recs])
        self.assertEqual(b"".join(r.to_bytes() for r in back), buf)
        self.assertEqual(sl.cstr(back[2].s.cary_name), "A5")
        self.assertEqual(back[3].s.fary_data[512], 512 + 5)

    def test_partial_returns_only_complete(self):
        buf = b"".join(r.to_bytes() for r in _synthetic())
        cut = sl.SIZES["L0"] + sl.SIZES["L1"] + 100  # L2 の途中
        self.assertEqual([r.kind for r in sl.iter_records(buf[:cut])], ["L0", "L1"])

    def test_bad_length(self):
        buf = bytearray(sl.new_record("L0").to_bytes())
        buf[4] = 1
        with self.assertRaises(sl.SamLogError):
            list(sl.iter_records(bytes(buf)))

    def test_set_cstr_overflow(self):
        r = sl.new_record("L2")
        with self.assertRaises(ValueError):
            sl.set_cstr(r.s, "cary_name", "A100x")


class TestFollower(unittest.TestCase):
    def test_growing_file(self):
        recs = _synthetic(n_int=2)
        buf = b"".join(r.to_bytes() for r in recs)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "log")
            fol = sl.Follower(path)
            # 少しずつ書き足し、レコードの途中で切れても正しく追いかけるか
            steps = [0, 50, sl.SIZES["L0"] + sl.SIZES["L1"], 30000, 50000, len(buf) - 4, len(buf)]
            got = []
            for end in steps:
                with open(path, "wb") as f:
                    f.write(buf[:end])
                got += fol.poll()
            self.assertEqual([r.kind for r in got], [r.kind for r in recs])
            self.assertTrue(fol.done)
            self.assertEqual(fol.n_dat, 4)
            self.assertEqual(fol.poll(), [])


class TestHelpers(unittest.TestCase):
    def test_array_names(self):
        self.assertEqual(sl.array_index("A05"), 4)
        self.assertEqual(sl.array_index("A5"), 4)
        self.assertEqual(sl.array_label(20), "A21")
        self.assertEqual(sl.array_logname(4), "A5")
        with self.assertRaises(ValueError):
            sl.array_index("A33")

    def test_mjdsec(self):
        # サンプルの ZERO の開始: 5295750206 = 2026-09-10 09:43:26 UTC（JST 18:43:26）
        t = sl.mjdsec_to_datetime(5295750206)
        self.assertEqual(t, dt.datetime(2026, 9, 10, 9, 43, 26, tzinfo=dt.timezone.utc))
        self.assertEqual(sl.datetime_to_mjdsec(t), 5295750206)
        self.assertEqual(sl.mjdsec_to_unix_ns(5295750206), int(t.timestamp()) * 10**9)

    def test_logname(self):
        n = sl.LogName.parse("/x/20260910183522.SAM45.rcash40.csv26.squint")
        self.assertEqual(n.log_id, "20260910183522")
        self.assertEqual(str(n.with_backend("YKSP")), "20260910183522.YKSP.rcash40.csv26.squint")
        p = sl.LogName.parse("_20260910183522.SAM45.rcash40.csv26.squint")
        self.assertEqual(p.prefix, "_")
        self.assertEqual(str(p.with_prefix("")), "20260910183522.SAM45.rcash40.csv26.squint")


@unittest.skipUnless(SAMPLE, "SAM45_SAMPLE が無い")
class TestSample(unittest.TestCase):
    """実際のロギング（2026-09-10、PSW、A05・A06・A13・A14）。"""

    @classmethod
    def setUpClass(cls):
        with open(SAMPLE, "rb") as f:
            cls.buf = f.read()
        cls.recs = list(sl.iter_records(cls.buf))

    def test_whole_file(self):
        kinds = [r.kind for r in self.recs]
        self.assertEqual(kinds[0], "L0")
        self.assertEqual(kinds[1], "L1")
        self.assertEqual(kinds[-1], "ED")
        self.assertEqual(self.recs[-1].offset + sl.SIZES["ED"], len(self.buf))

    def test_bytes_identical(self):
        self.assertEqual(b"".join(r.to_bytes() for r in self.recs), self.buf)

    def test_header_values(self):
        ctl, obs = self.recs[0].s, self.recs[1].s
        self.assertEqual(sl.cstr(ctl.cbe_type), "SAM45")
        self.assertEqual(sl.cstr(ctl.cversion), "V004.1")
        used = [sl.array_label(i) for i in range(sl.ARYMAX) if obs.iary_usefg[i]]
        self.assertEqual(used, ["A05", "A06", "A13", "A14"])
        self.assertEqual(obs.iary_num, 4)
        self.assertEqual(obs.ichannel[4], 4096)
        self.assertAlmostEqual(obs.dbebw[4], 31.25e6)
        self.assertEqual(sl.cstr(obs.csid_type[4]), "USB")
        self.assertEqual(obs.iary_ifatt[4], 5)

    def test_data_records(self):
        dats = [r for r in self.recs if r.kind == "L2"]
        self.assertEqual(len(dats), 208)
        names = {sl.cstr(r.s.cary_name) for r in dats}
        self.assertEqual(names, {"A5", "A6", "A13", "A14"})
        for r in dats:
            self.assertEqual(r.s.iary_no, 40 + int(sl.cstr(r.s.cary_name)[1:]))
            span = r.s.dint_edmjd - r.s.dint_stmjd
            self.assertEqual(span, r.s.iinteg_time)
        zero = dats[0]
        self.assertEqual(sl.cstr(zero.s.cscan_type), "ZERO")
        self.assertTrue(all(v == 8192 for v in zero.s.fary_data))

    def test_follower_whole(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, os.path.basename(SAMPLE))
            fol = sl.Follower(path)
            got = []
            for end in range(0, len(self.buf) + 1, 100_000):
                with open(path, "wb") as f:
                    f.write(self.buf[:end])
                got += fol.poll()
            with open(path, "wb") as f:
                f.write(self.buf)
            got += fol.poll()
            self.assertEqual(len(got), len(self.recs))
            self.assertTrue(fol.done)


if __name__ == "__main__":
    unittest.main()
