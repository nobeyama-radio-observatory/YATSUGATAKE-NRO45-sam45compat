# SPDX-License-Identifier: BSD-3-Clause
"""rebeam: v0.3.0 より前の YKSP のロギングのビームの番号を直す。"""

import ctypes as C
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from sam45compat import lc as lcm
from sam45compat import mapping as mp
from sam45compat import rebeam as rb
from sam45compat import samlog as sl
from test_lc import MIRROR, _write
from test_mapping import _obs
from test_yksp import _sam45_records


def _old_yksp_l1(recv, beams, arrays):
    """v0.2.x の LC が書いた形の L1: 写し元の欄を丸ごと写し（ビームも同じ）、肩代わりの Array だけ使う。"""
    obs = _obs(bw2=31.25, k0_2=258048, arrays=[i for i in arrays if i < 16], arrays2=[i for i in arrays if i >= 16])
    o = obs.s
    for idx, b in zip(arrays, beams):
        name = recv + ("" if recv == "H40" else "HV"[idx % 2])
        C.memmove(C.addressof(o.cfe_type[idx]), name.encode().ljust(10, b"\0"), 10)
        o.imlt_no[idx] = b
    obs = lcm.mirror_arrays(obs, MIRROR)
    return mp.restrict_arrays(obs, [4, 5, 6, 7, 20, 21, 22, 23])


class TestRebeam(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, obs, log_id="20261113201714"):
        d = os.path.join(self.dir, "srv", "grp", "prj", "logging")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"YKSP.obs.grp.prj.{log_id}")
        _write(path, _sam45_records(obs, [4, 5]))
        return path

    def test_fix_7bee(self):
        # 7BEE: A01〜A04 = 1,1,2,2、A17〜A20 = 1,1,2,2（2 つ目の窓）。A05〜A08・A21〜A24 は 3,3,4,4 になる
        obs = _old_yksp_l1("MULT7", [1, 1, 2, 2, 1, 1, 2, 2], [0, 1, 2, 3, 16, 17, 18, 19])
        self.assertEqual(list(obs.s.imlt_no[4:8]), [1, 1, 2, 2])
        new, changes, problems = rb.fix_l1(obs, MIRROR)
        self.assertEqual(problems, [])
        self.assertEqual(list(new.s.imlt_no[4:8]), [3, 3, 4, 4])
        self.assertEqual(list(new.s.imlt_no[20:24]), [3, 3, 4, 4])
        self.assertEqual(len(changes), 8)
        # 直したものはもう直さない
        self.assertEqual(rb.fix_l1(new, MIRROR)[0], None)

    def test_single_beam_untouched(self):
        obs = _old_yksp_l1("H40", [0, 0, 0, 0], [0, 1, 2, 3])
        self.assertEqual(rb.fix_l1(obs, MIRROR), (None, [], []))

    def test_patch_in_place(self):
        obs = _old_yksp_l1("TMULT", [1, 1, 2, 2], [0, 1, 2, 3])
        path = self._file(obs)
        before = sl.read_file(path)
        old = rb.read_l1(path)
        new, _, _ = rb.fix_l1(old, MIRROR)
        bdir = os.path.join(self.dir, "backup")
        rb.patch_l1(path, old, new, bdir)
        after = sl.read_file(path)
        self.assertEqual(list(after[1].s.imlt_no[4:8]), [3, 3, 4, 4])
        # L1 のほかは変わらない
        self.assertEqual(len(after), len(before))
        for x, y in zip(before[2:], after[2:]):
            self.assertEqual(x.to_bytes(), y.to_bytes())
        # 元の L1 が退避にある
        with open(os.path.join(bdir, os.path.basename(path) + ".L1"), "rb") as f:
            self.assertEqual(f.read(), before[1].to_bytes())

    def test_main_apply(self):
        obs = _old_yksp_l1("MULT7", [1, 1, 2, 2], [0, 1, 2, 3])
        path = self._file(obs)
        root = os.path.join(self.dir, "srv")
        otf = os.path.join(root, "grp", "prj", "otf")
        os.makedirs(otf)
        open(os.path.join(otf, "obs.SRC.20261113201714.08.Y"), "w").close()
        cfg = os.path.join(self.dir, "cfg.json")
        with open(cfg, "w") as f:
            f.write('{"mirror": %s, "deliver_root": "%s"}' % (str(MIRROR).replace("'", '"'), root))
        out = io.StringIO()
        with redirect_stdout(out):
            rb.main(["--config", cfg])                     # 並べるだけ
        self.assertIn("otf", out.getvalue())
        self.assertEqual(list(rb.read_l1(path).s.imlt_no[4:8]), [1, 1, 2, 2])
        with redirect_stdout(io.StringIO()):
            rb.main(["--config", cfg, "--apply", "--backup", os.path.join(self.dir, "bk")])
        self.assertEqual(list(rb.read_l1(path).s.imlt_no[4:8]), [3, 3, 4, 4])


if __name__ == "__main__":
    unittest.main()
