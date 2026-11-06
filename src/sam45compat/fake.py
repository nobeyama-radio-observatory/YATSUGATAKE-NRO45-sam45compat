# SPDX-License-Identifier: BSD-3-Clause
"""模擬の分光計（試験と、望遠鏡・ボードなしの再生用）。

どの区間を聞かれても、窓ごとに決まった形のスペクトル（記録の IF の昇順）を返す。
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from . import mapping as mp
from .binning import TINT_NS, Binned


class FakeDumps:
    def __init__(self, level: Optional[Dict[str, float]] = None):
        self.level = level or {}
        self.asked: List[tuple] = []
        self.latest: Optional[int] = 2 ** 62     # None にすると「まだ届いていない」

    def interval(self, key: str, st_ns: int, ed_ns: int) -> Binned:
        self.asked.append((key, st_ns, ed_ns))
        n = max(0, (ed_ns - st_ns) // TINT_NS)
        spec = self.level.get(key, 1.0) * (1.0 + np.arange(4096) / 4096.0)
        return Binned(spec, n, n)

    def latest_end_ns(self) -> Optional[int]:
        return self.latest


class FakeSpectrometer:
    """lc.Spectrometer の模擬。prepare に失敗させることもできる。"""

    def __init__(self, fail: Optional[str] = None):
        self.fail = fail
        self.calls: List[tuple] = []
        self._dumps = FakeDumps()

    def prepare(self, plan: mp.Plan, log_id: str) -> None:
        self.calls.append(("prepare", log_id, plan.kind, tuple(plan.set_args())))
        if self.fail:
            raise RuntimeError(self.fail)

    def finish(self, log_id: str) -> None:
        self.calls.append(("finish", log_id))

    def dumps(self) -> FakeDumps:
        return self._dumps
