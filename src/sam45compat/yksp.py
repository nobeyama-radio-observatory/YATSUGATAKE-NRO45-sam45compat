# SPDX-License-Identifier: BSD-3-Clause
"""YKSP のロギング（SAM45 と同じ形式）を作る。

docs/design/standin-study.md の「YKSP のロギング」:
- L0: SAM45 のものを写す（中の名前・バックエンドの種類は SAM45 のまま）
- L1: 写して、f2 の書き換え（mapping.rewrite_l1）と、肩代わりする Array だけの設定（restrict_arrays）
- L2: SAM45 の 1 積分ごとに、肩代わりする Array の L2 を 1 つずつ。付随の欄は同じ積分の SAM45 の L2 から写し
  （同じ Array の L2 があればそれ、無ければ L1 を埋めた元の Array（A05 なら A01）、それも無ければ最初の L2。
  SAM45 のロギングには A05〜A08・A21〜A24 が含まれない。2026-11-10）、
  名前・番号・スペクトル・idmy_flag・dtsys を書き換える。ZERO は 8192。取れない Array は idmy_flag = 1
- ED: SAM45 が ED を書いたら書く

どの段階も YKSP のロギングだけのファイルを書き、観測の後にマージして観測生データ（YKSP）にする（2026-11-10）。
合体（combine.py）は 2026-11-10 にやめたが、コードは残している。
"""

from __future__ import annotations

import ctypes
import datetime as _dt
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from . import mapping as mp
from . import samlog as sl
from .binning import DumpSource

ZERO_VALUE = 8192.0      # SAM45 の ZERO の値（全 ch で一定）
SCAN_ZERO = "ZERO"
SCAN_R = "R"
SCAN_SKY = "SKY"
# Tsys の R の温度 [K]。サンプルの SAM45 の dtsys から逆算すると 4 Array とも ≒ 293.0 K（±0.2）。
# SAM45 LC の式・温度の出どころは未確認（docs/design/standin-study.md の「Tsys」）
T_LOAD_K = 293.0


def tsys_k(r_mean: float, sky_mean: float, ifatt_db: float, t_load_k: float = T_LOAD_K) -> float:
    """Tsys = T_R·SKY / (R·10^(IFATT/10) − SKY)。値は ZERO を引いた後の ch の平均。
    サンプルの SAM45 の dtsys を 2 % 以内（T_R 293 K で 0.1 % 以内）で再現する。"""
    r = r_mean * 10 ** (ifatt_db / 10)
    if r <= sky_mean:
        return 0.0
    return t_load_k * sky_mean / (r - sky_mean)


def set_spectrum(d: sl.LogDat, spec) -> None:
    """L2 の fary_data にスペクトルを入れる（little-endian の float32）。4096 ch より少なければ（ichannel が 2048 など）
    前から詰めて残りを 0 にする（SAM45 と同じ。2026-11-12 の観測で確認）。"""
    arr = np.ascontiguousarray(spec, dtype="<f4")
    if arr.ndim != 1 or not 0 < len(arr) <= sl.CHMAX_LOGGING:
        raise ValueError(f"スペクトルの形 {arr.shape}")
    if len(arr) < sl.CHMAX_LOGGING:
        arr = np.concatenate([arr, np.zeros(sl.CHMAX_LOGGING - len(arr), dtype="<f4")])
    ctypes.memmove(ctypes.addressof(d) + sl.LogDat.fary_data.offset, arr.tobytes(), arr.nbytes)


_JST = _dt.timezone(_dt.timedelta(hours=9))


def jst_to_mjdsec(text: str) -> Optional[float]:
    """cint_sttm（JST の YYYYMMDDHHMMSS、OTF は .ffff まで）→ MJD の起点からの秒。読めなければ None。"""
    head, _, frac = text.strip().partition(".")
    try:
        t = _dt.datetime.strptime(head, "%Y%m%d%H%M%S").replace(tzinfo=_JST)
        return sl.datetime_to_mjdsec(t) + (float("0." + frac) if frac else 0.0)
    except ValueError:
        return None


def integration_key(d) -> Tuple[str, float, float, str]:
    """SAM45 の L2 から、同じ積分を表す鍵（スキャン ID・開始・終了・スキャンの種類。時刻は MJD の起点からの秒）。

    OTF の ON は dint_stmjd・dint_edmjd が 0 で、時刻は cint_sttm（JST、積分の中心、ms まで）にしか無い
    （2026-11-12 に 20261112202705 で確かめた。cint_edtm は空）。そのときは開始・終了とも中心の時刻にする
    （幅 0。区間の幅は Integration.width_s で足す）。"""
    st, ed = float(d.dint_stmjd), float(d.dint_edmjd)
    if st == 0.0 and ed == 0.0:
        c = jst_to_mjdsec(sl.cstr(d.cint_sttm))
        if c is not None:
            st = ed = c
    return (sl.cstr(d.cscanid), st, ed, sl.cstr(d.cscan_type))


@dataclass
class Integration:
    """SAM45 の 1 積分（同じ鍵の L2 の組）。"""
    key: Tuple[str, float, float, str]
    records: List[sl.Record] = field(default_factory=list)
    width_s: float = 0.0        # 鍵が中心の時刻だけ（OTF。開始 = 終了）のときの区間の幅（L1 の diptim）

    @property
    def centered(self) -> bool:
        return self.key[1] == self.key[2]

    @property
    def st_ns(self) -> int:
        """区間の始め。OTF は中心 − diptim / 2（2026-11-12）。"""
        return sl.mjdsec_to_unix_ns(self.key[1] - (self.width_s / 2 if self.centered else 0.0))

    @property
    def ed_ns(self) -> int:
        return sl.mjdsec_to_unix_ns(self.key[2] + (self.width_s / 2 if self.centered else 0.0))

    @property
    def scan_type(self) -> str:
        return self.key[3]


class IntegrationGrouper:
    """SAM45 の L2 を積分ごとにまとめる。鍵が変わった時点で、前の積分を閉じて返す。
    width_s: 中心の時刻だけの積分（OTF）の区間の幅。L1 の diptim を入れる（set_obs）。"""

    def __init__(self, width_s: float = 0.0):
        self.cur: Optional[Integration] = None
        self.width_s = width_s

    def set_obs(self, obs) -> None:
        """L1 から OTF の積分の幅（diptim [s]）を読む。"""
        self.width_s = float(obs.diptim)

    def feed(self, rec: sl.Record) -> List[Integration]:
        if rec.kind != "L2":
            return []
        k = integration_key(rec.s)
        if self.cur is None:
            self.cur = Integration(k, [rec], self.width_s)
            return []
        if k == self.cur.key:
            self.cur.records.append(rec)
            return []
        done, self.cur = self.cur, Integration(k, [rec], self.width_s)
        return [done]

    def flush(self) -> List[Integration]:
        done, self.cur = self.cur, None
        return [done] if done else []


@dataclass
class L2Result:
    record: sl.Record
    array: int
    valid: bool
    reason: str = ""
    spec: Optional[np.ndarray] = None   # 書いたスペクトル（L2 の順。QL 用。ZERO・取れないときは None）


class YkspBuilder:
    """SAM45 のロギングのレコードから、YKSP のレコードを作る。"""

    def __init__(self, plan: mp.Plan, ifatt_mode: str, dumps: DumpSource,
                 scale: float = 1.0, min_coverage: float = 0.9, flag_mask: int = 0,
                 t_load_k: Optional[float] = T_LOAD_K, template_src: Optional[Dict[int, int]] = None):
        self.plan = plan
        self.template_src = dict(template_src or {})   # 肩代わりの Array → L2 の付随の欄を写す元の Array
        self.ifatt_mode = ifatt_mode
        self.dumps = dumps
        self.scale = scale
        self.min_coverage = min_coverage
        self.flag_mask = flag_mask          # この FLAGS が立ったダンプを含む積分は無効にする
        self.targets = sorted(set(plan.windows) | set(plan.skipped))
        self.t_load_k = t_load_k            # None なら dtsys を 0 のままにする
        self._ifatt: Dict[int, float] = {}  # 書いた L1 の iary_ifatt
        self._r: Dict[int, float] = {}      # 直近の R の平均
        self._tsys: Dict[int, float] = {}

    # ---- 頭 ----

    def header(self, ctl: sl.Record, obs: sl.Record, only_targets: bool = True) -> List[sl.Record]:
        """L0 と書き換えた L1。only_targets で肩代わりする Array だけの L1（YKSP のロギング。False は合体用）。"""
        l1 = mp.rewrite_l1(obs, self.plan, self.ifatt_mode)
        self._ifatt = {i: float(l1.s.iary_ifatt[i]) for i in self.targets}
        if only_targets:
            l1 = mp.restrict_arrays(l1, self.targets)
        return [sl.copy_record(ctl), l1]

    # ---- 積分 ----

    def ready(self, integ: Integration, margin_ns: int = 0) -> bool:
        """この積分を束ねられるだけのダンプが届いたか。"""
        if not self.plan.windows or integ.scan_type == SCAN_ZERO:
            return True
        last = self.dumps.latest_end_ns()
        return last is not None and last >= integ.ed_ns + margin_ns

    def integration(self, integ: Integration) -> List[L2Result]:
        """1 積分ぶんの、肩代わりする Array の L2（Array の順）。"""
        by_name = {sl.cstr(r.s.cary_name): r.s for r in integ.records}
        out = []
        for idx in self.targets:
            tmpl = by_name.get(sl.array_logname(idx))
            if tmpl is None and idx in self.template_src:
                tmpl = by_name.get(sl.array_logname(self.template_src[idx]))
            if tmpl is None:
                tmpl = integ.records[0].s
            rec = sl.Record("L2", sl.LogDat.from_buffer_copy(bytes(tmpl)))
            d = rec.s
            sl.set_cstr(d, "cary_name", sl.array_logname(idx))
            d.iary_no = 41 + idx
            valid, reason, spec = self._spectrum(idx, integ)
            d.dtsys = self._update_tsys(idx, integ.scan_type, spec if valid else None)
            d.idmy_flag = 0 if valid else 1
            set_spectrum(d, np.zeros(sl.CHMAX_LOGGING) if spec is None else spec)
            out.append(L2Result(rec, idx, valid, reason,
                                None if integ.scan_type == SCAN_ZERO else spec))
        return out

    def _update_tsys(self, idx: int, scan: str, spec) -> float:
        """SAM45 と同じく、R を覚え、SKY で Tsys を出し、以後のスキャンにその値を書く（最初の SKY までは 0）。
        YATSUGATAKE の値は ZERO が 0（8192 は SAM45 に合わせて書くだけ）なので、引かずに使う。"""
        if self.t_load_k is None:
            return 0.0
        if spec is not None and scan == SCAN_R:
            self._r[idx] = float(np.mean(spec))
        elif spec is not None and scan == SCAN_SKY and idx in self._r:
            self._tsys[idx] = tsys_k(self._r[idx], float(np.mean(spec)),
                                     self._ifatt.get(idx, 0.0), self.t_load_k)
        return self._tsys.get(idx, 0.0)

    def _spectrum(self, idx: int, integ: Integration):
        if idx in self.plan.skipped:
            return False, self.plan.skipped[idx], None
        w = self.plan.windows[idx]
        if integ.scan_type == SCAN_ZERO:
            return True, "", np.full(w.nch_out, ZERO_VALUE)
        if integ.st_ns >= integ.ed_ns:
            return False, "積分の時刻が無い（区間の幅が 0）", None
        b = self.dumps.interval(w.slot.key, integ.st_ns, integ.ed_ns)
        if b.spec is None:
            return False, "ダンプが無い", None
        spec = w.l2_order(b.spec) * self.scale
        if b.coverage < self.min_coverage:
            return False, f"ダンプが足りない（{b.n}/{b.n_expected}）", spec
        if b.flags & self.flag_mask:
            return False, f"FLAGS 0x{b.flags:x}", spec
        return True, "", spec

    # ---- 終わり ----

    @staticmethod
    def end() -> sl.Record:
        return sl.new_record("ED")


def build_offline(sam_records: Iterable[sl.Record], builder: YkspBuilder,
                  only_targets: bool = True) -> Tuple[List[sl.Record], List[L2Result]]:
    """記録済みの SAM45 のロギング全体から、YKSP のロギング全体を作る（段階 1 の読み直し・試験用）。"""
    out: List[sl.Record] = []
    results: List[L2Result] = []
    grouper = IntegrationGrouper()
    ctl = obs = None

    def emit(integs):
        for integ in integs:
            rs = builder.integration(integ)
            results.extend(rs)
            out.extend(r.record for r in rs)

    for rec in sam_records:
        if rec.kind == "L0":
            ctl = rec
        elif rec.kind == "L1":
            obs = rec
            grouper.set_obs(obs.s)
            out.extend(builder.header(ctl, obs, only_targets))
        elif rec.kind == "L2":
            emit(grouper.feed(rec))
        elif rec.kind == "ED":
            emit(grouper.flush())
            out.append(builder.end())
    return out, results
