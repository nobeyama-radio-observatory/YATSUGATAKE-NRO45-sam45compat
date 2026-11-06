# SPDX-License-Identifier: BSD-3-Clause
"""10.24 ms のダンプを SAM45 の積分の区間に束ねる（docs/design/standin-study.md の t2）。

ダンプ k の区間は [t_k, t_k + tint)（t_k は YATSUGATAKE の記録の utc_ns = 実効の区間の始まり）。
SAM45 の積分 [st, ed) には、**区間の中心が [st, ed) に入るダンプ**を使う。端のずれは最大 tint/2（≒ 5 ms）。
値は使ったダンプの**平均**（積分時間によらない大きさ。スケールは YkspBuilder で掛ける）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Protocol

import numpy as np

TINT_NS = 10_240_000


@dataclass
class Binned:
    spec: Optional[np.ndarray]   # 平均（float64）。ダンプが 1 つも無ければ None
    n: int                       # 使ったダンプの数
    n_expected: int              # 区間に入るはずの数
    flags: int = 0               # 使ったダンプの FLAGS の OR（飽和など）

    @property
    def coverage(self) -> float:
        return self.n / self.n_expected if self.n_expected else 0.0


def expected_count(st_ns: int, ed_ns: int, phase_ns: int, tint_ns: int = TINT_NS) -> int:
    """格子 t = phase + m·tint の上で、中心 t + tint/2 が [st, ed) に入るダンプの数。"""
    c0 = phase_ns + tint_ns // 2
    lo = -((c0 - st_ns) // tint_ns)          # ceil((st − c0) / tint)
    hi = -((c0 - ed_ns) // tint_ns)          # ceil((ed − c0) / tint)
    return max(0, hi - lo)


def select(t_ns: np.ndarray, st_ns: int, ed_ns: int, tint_ns: int = TINT_NS) -> np.ndarray:
    """中心が [st, ed) に入るダンプの番号（t_ns は昇順）。"""
    c = t_ns + tint_ns // 2
    i0 = int(np.searchsorted(c, st_ns, side="left"))
    i1 = int(np.searchsorted(c, ed_ns, side="left"))
    return np.arange(i0, i1)


def bin_interval(t_ns: np.ndarray, spec: np.ndarray, st_ns: int, ed_ns: int,
                 flags: Optional[np.ndarray] = None, tint_ns: int = TINT_NS) -> Binned:
    """1 つの窓のダンプの並び（t_ns 昇順、spec は (n, nch)）を [st, ed) に束ねる。"""
    if len(t_ns) == 0:
        return Binned(None, 0, 0)
    phase = int(t_ns[0] % tint_ns)
    n_exp = expected_count(st_ns, ed_ns, phase, tint_ns)
    idx = select(t_ns, st_ns, ed_ns, tint_ns)
    if len(idx) == 0:
        return Binned(None, 0, n_exp)
    f = int(np.bitwise_or.reduce(flags[idx])) if flags is not None and len(idx) else 0
    return Binned(spec[idx].astype(np.float64).mean(axis=0), len(idx), n_exp, f)


class DumpSource(Protocol):
    """窓ごとのダンプを持つもの（記録の一時ファイル・試験の模擬）。窓の鍵は 'A0' など。"""

    def interval(self, key: str, st_ns: int, ed_ns: int) -> Binned: ...

    def latest_end_ns(self) -> Optional[int]:
        """受け取り済みのダンプの区間の終わりの最も遅いもの（全窓の最小）。何も無ければ None。"""
        ...


class MemoryDumps:
    """手元の配列に持つダンプ（試験と、短い記録の読み直し用）。"""

    def __init__(self, tint_ns: int = TINT_NS):
        self.tint_ns = tint_ns
        self._t: Dict[str, list] = {}
        self._s: Dict[str, list] = {}
        self._f: Dict[str, list] = {}
        self._cache: Dict[str, tuple] = {}

    def add(self, key: str, t_ns: int, spec, flags: int = 0) -> None:
        self._t.setdefault(key, []).append(int(t_ns))
        self._s.setdefault(key, []).append(np.asarray(spec, dtype=np.float32))
        self._f.setdefault(key, []).append(int(flags))
        self._cache.pop(key, None)

    def _arrays(self, key: str):
        if key not in self._cache:
            t = np.asarray(self._t.get(key, []), dtype=np.int64)
            order = np.argsort(t, kind="stable")
            s = np.stack(self._s[key])[order] if key in self._s else np.zeros((0, 0), np.float32)
            f = np.asarray(self._f.get(key, []), dtype=np.int64)[order]
            self._cache[key] = (t[order], s, f)
        return self._cache[key]

    def interval(self, key: str, st_ns: int, ed_ns: int) -> Binned:
        t, s, f = self._arrays(key)
        return bin_interval(t, s, st_ns, ed_ns, f, self.tint_ns)

    def latest_end_ns(self) -> Optional[int]:
        ends = [max(v) + self.tint_ns for v in self._t.values() if v]
        return min(ends) if ends else None

    def drop_before(self, t_ns: int) -> None:
        """t_ns より前に終わるダンプを捨てる（長い観測で手元を増やさない）。"""
        for k in list(self._t):
            keep = [i for i, t in enumerate(self._t[k]) if t + self.tint_ns > t_ns]
            self._t[k] = [self._t[k][i] for i in keep]
            self._s[k] = [self._s[k][i] for i in keep]
            self._f[k] = [self._f[k][i] for i in keep]
            self._cache.pop(k, None)
