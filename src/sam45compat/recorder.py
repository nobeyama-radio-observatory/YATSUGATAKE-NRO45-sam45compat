# SPDX-License-Identifier: BSD-3-Clause
"""specd のデータの口から 10.24 ms のダンプを受け、観測ごとの一時ファイルに溜める。

- 記録の形は YATSUGATAKE の s45proto（v2、fmt = float32、IF の昇順）。external/YATSUGATAKE/sw/pynq/s45proto.py を使う
- ACK（書き終えた最後の seq）を 0.2 秒おきに返す。specd はそこまでを溜まりから消す
- ダンプは窓ごとのファイル（float32 × 4096 の行）に書き足し、時刻の索引だけを手元に持つ。
  10.24 ms で 8 窓は ≒ 12.8 MB/s（1 時間で ≒ 46 GB）なので、メモリには持たない。観測が終わったら消す
- no_pps（1PPS なしの試験、2026-11-11）: specd は時刻を答えない（utc_ns = 0）。最初のダンプを受けたときの
  この計算機の時計を起点にし、ビート（dump_t）の差から tint 単位の番号を出して時刻を付ける。
  ずれはネットワークと specd の溜まりの遅れぶん（数 ms〜数十 ms）。流れの確かめ用で、科学には使えない
"""

from __future__ import annotations

import logging
import os
import select
import shutil
import socket
import sys
import threading
import time
from typing import Dict, Optional, Tuple

import numpy as np

from .binning import TINT_NS, Binned, bin_interval, expected_count

_PYNQ = os.path.join(os.path.dirname(__file__), "..", "..", "external", "YATSUGATAKE", "sw", "pynq")
if os.path.isdir(_PYNQ) and _PYNQ not in sys.path:
    sys.path.append(os.path.abspath(_PYNQ))
import s45proto as P  # noqa: E402

log = logging.getLogger("sam45compat")

NCH = 4096
# 記録に付ける印（窓の FLAGS は下位 16 bit を使う）
F_SAT = 1 << 16       # 18 bit で飽和した点がある
F_NOTIME = 1 << 17    # 時刻を答えられない（utc_ns = 0）
F_AXIS = 1 << 18      # 軸（if_mhz・df）が予定と違う
WIN_FLAGS_MASK = 0x7EF   # 窓の FLAGS の [4]（FFT IP に待たされた）は正常（specrecv と同じ）
DEFAULT_FLAG_MASK = WIN_FLAGS_MASK | F_SAT | F_NOTIME | F_AXIS
# 記録の health の [3:0]（DUMP_H。コアの hw/common/time_core.v の ev_out。ダンプの間の OR）
H_PPS_MISS = 1 << 0     # 1PPS が来ていない（1.5 s）
H_PPS_INT = 1 << 1      # 1PPS の間隔が 256,000,000 ± PPS_TOL（既定 1）ビートを外れた = fs が 1PPS と同じ基準に乗っていない
H_PPS_GLITCH = 1 << 2   # 1PPS のグリッチ
H_NOANCHOR = 1 << 3     # 時刻の原点が無い


def key_of(core: int, s: int) -> str:
    return f"{'ABCD'[core]}{s}"


class _Grow:
    """書き足していく int64 の並び（倍々に広げる）。view() は今の長さぶん。"""

    def __init__(self):
        self._a = np.empty(1024, dtype=np.int64)
        self.n = 0

    def append(self, v: int) -> None:
        if self.n == len(self._a):
            b = np.empty(len(self._a) * 2, dtype=np.int64)
            b[: self.n] = self._a[: self.n]
            self._a = b
        self._a[self.n] = v
        self.n += 1

    def view(self) -> np.ndarray:
        return self._a[: self.n]


class DiskDumps:
    """観測 1 つぶんのダンプ（窓ごとのファイル）。binning.DumpSource として束ねに使う。

    時刻のあるダンプは、窓ごとに時刻・ファイルの行・FLAGS を numpy の並びで持ち、積分の区間に入るものだけを
    二分探索で切り出す（1 回の束ねの重さが観測の長さによらない）。2026-11-13 までは呼ぶたびに観測の初めからの
    全部を配列に直していて、8 窓の OTF が 20 分を越えると LC が受けに追いつかず、specd の溜まりがあふれた（DROP）。
    """

    def __init__(self, path: str, expect: Dict[str, Tuple[float, float]], tint_ns: int = TINT_NS,
                 no_pps: bool = False):
        """expect: 窓の鍵 → (記録の if_mhz, df [MHz])。この窓だけを溜め、軸を照らす。"""
        self.no_pps = no_pps
        self._dt: Dict[str, list] = {k: [] for k in expect}      # no_pps: ダンプの dump_t（ビート）
        self._anchor: Optional[Tuple[int, int]] = None            # no_pps: (この計算機の時計 [ns], dump_t)
        self._step: Optional[int] = None                          # no_pps: tint 1 つぶんのビート
        self.path = path
        self.expect = dict(expect)
        self.tint_ns = tint_ns
        os.makedirs(path, exist_ok=True)
        self._f = {k: open(os.path.join(path, f"{k}.f32"), "wb") for k in self.expect}
        self._t: Dict[str, list] = {k: [] for k in self.expect}
        self._fl: Dict[str, list] = {k: [] for k in self.expect}
        # 時刻のあるダンプだけの索引（no_pps でないとき）: 時刻・ファイルの行・FLAGS。時刻が昇順でなければ印
        self._vt: Dict[str, _Grow] = {k: _Grow() for k in self.expect}
        self._vrow: Dict[str, _Grow] = {k: _Grow() for k in self.expect}
        self._vfl: Dict[str, _Grow] = {k: _Grow() for k in self.expect}
        self._unordered: Dict[str, bool] = {k: False for k in self.expect}
        self._lock = threading.Lock()
        self.n_axis_bad = 0
        self.n_notime = 0

    def add(self, d: dict) -> None:
        """s45proto.decode の辞書（SPEC v2）を 1 つ足す。"""
        k = key_of(d["core"], d["s"])
        if k not in self.expect:
            return
        if d["nch"] != NCH or d["fmt"] != P.FMT_F32:
            raise ValueError(f"{k}: nch {d['nch']}・fmt {d['fmt']}（4096・float32 のはず）")
        fl = d["flags"] & WIN_FLAGS_MASK
        if d["sat"]:
            fl |= F_SAT
        if self.no_pps:
            pass                                  # 時刻は dump_t から付ける（_times）
        elif d["utc_ns"] == 0 or d["health"] & P.H_NOTIME:
            fl |= F_NOTIME
            self.n_notime += 1
        if_mhz, df = self.expect[k]
        if abs(d["if_mhz"] - if_mhz) > 1e-6 or abs(d["df_hz"] / 1e6 - df) > 1e-9:
            fl |= F_AXIS
            if self.n_axis_bad == 0:
                log.error("%s: 記録の軸が予定と違う（if %.6f・df %.3f Hz、予定 if %.6f・df %.3f Hz）",
                          k, d["if_mhz"], d["df_hz"], if_mhz, df * 1e6)
            self.n_axis_bad += 1
        data = np.ascontiguousarray(d["data"], dtype="<f4")
        with self._lock:
            if self.no_pps and self._anchor is None:
                # ダンプは区間が終わってから届くので、受けた時刻から tint 1 つぶん戻したところを区間の始まりとする
                self._anchor = (time.time_ns() - self.tint_ns, int(d["dump_t"]))
            self._f[k].write(data.tobytes())
            if not self.no_pps and not fl & F_NOTIME:
                t = int(d["utc_ns"])
                vt = self._vt[k]
                if vt.n and t <= int(vt.view()[-1]):
                    self._unordered[k] = True
                vt.append(t)
                self._vrow[k].append(len(self._t[k]))
                self._vfl[k].append(fl)
            self._t[k].append(int(d["utc_ns"]))
            self._dt[k].append(int(d["dump_t"]))
            self._fl[k].append(fl)

    def _times(self, key: str) -> list:
        """窓 key のダンプの時刻（_lock を持って呼ぶ）。no_pps なら dump_t から作る。"""
        if not self.no_pps:
            return self._t[key]
        dts = self._dt[key]
        if not dts or self._anchor is None:
            return []
        if self._step is None:
            # tint 1 つぶんのビート = 窓の中で隣り合うダンプの差の一番小さいもの（落ちたダンプは差が倍になる）
            diffs = [b - a for v in self._dt.values() for a, b in zip(v, v[1:]) if b > a]
            if not diffs:
                return [self._anchor[0]]          # まだ 1 つずつしか無い（差がわからない）
            self._step = min(diffs)
        h0, d0 = self._anchor
        n = np.rint((np.asarray(dts, dtype=np.int64) - d0) / self._step).astype(np.int64)
        return list(h0 + n * self.tint_ns)

    def interval(self, key: str, st_ns: int, ed_ns: int) -> Binned:
        if not self.no_pps:
            return self._interval_indexed(key, st_ns, ed_ns)
        with self._lock:
            if key not in self._f:
                return Binned(None, 0, 0)
            self._f[key].flush()
            t = np.asarray(self._times(key), dtype=np.int64)
            fl = np.asarray(self._fl[key][: len(t)], dtype=np.int64)
        ok = (fl & F_NOTIME) == 0
        if not ok.all():                 # 時刻の無いダンプは区間に当てられない
            t, fl, rows = t[ok], fl[ok], np.nonzero(ok)[0]
        else:
            rows = None
        if len(t) == 0:
            return Binned(None, 0, 0)
        order_ok = bool(np.all(np.diff(t) > 0))
        if not order_ok:
            raise ValueError(f"{key}: ダンプの時刻が昇順でない")
        mm = np.memmap(os.path.join(self.path, f"{key}.f32"), dtype="<f4", mode="r")
        spec = mm[: (len(mm) // NCH) * NCH].reshape(-1, NCH)[: len(fl) if rows is None else len(ok)]
        if rows is not None:
            spec = spec[rows]
        return bin_interval(t, spec, st_ns, ed_ns, fl, self.tint_ns)

    def _interval_indexed(self, key: str, st_ns: int, ed_ns: int) -> Binned:
        """時刻のあるダンプの索引から、中心が [st, ed) に入るものだけを読む（binning.bin_interval と同じ結果）。"""
        tint = self.tint_ns
        with self._lock:
            if key not in self._f:
                return Binned(None, 0, 0)
            if self._unordered[key]:
                raise ValueError(f"{key}: ダンプの時刻が昇順でない")
            vt = self._vt[key].view()
            if len(vt) == 0:
                return Binned(None, 0, 0)
            n_exp = expected_count(st_ns, ed_ns, int(vt[0] % tint), tint)
            # 中心 t + tint/2 が st 以上 ⇔ t が st − tint/2 以上（整数なので同じ）
            i0 = int(np.searchsorted(vt, st_ns - tint // 2, side="left"))
            i1 = int(np.searchsorted(vt, ed_ns - tint // 2, side="left"))
            if i1 <= i0:
                return Binned(None, 0, n_exp)
            rows = self._vrow[key].view()[i0:i1].copy()
            f = int(np.bitwise_or.reduce(self._vfl[key].view()[i0:i1]))
            self._f[key].flush()
        mm = np.memmap(os.path.join(self.path, f"{key}.f32"), dtype="<f4", mode="r")
        spec = mm[: (len(mm) // NCH) * NCH].reshape(-1, NCH)
        r0, r1 = int(rows[0]), int(rows[-1]) + 1
        sel = spec[r0:r1] if r1 - r0 == len(rows) else spec[rows]
        return Binned(sel.astype(np.float64).mean(axis=0), len(rows), n_exp, f)

    def latest_end_ns(self) -> Optional[int]:
        with self._lock:
            ends = []
            for k in self._t:
                v = self._times(k)
                ends.append(v[-1] + self.tint_ns if v else None)
        if not ends or any(e is None for e in ends):
            return None
        return min(ends)

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {k: len(v) for k, v in self._t.items()}

    def close(self, remove: bool = True) -> None:
        with self._lock:
            for f in self._f.values():
                f.close()
            self._f = {}
        if remove:
            shutil.rmtree(self.path, ignore_errors=True)


class RefWatch:
    """周波数の基準（10 MHz）が効いているかを、受けた記録の health の 1PPS の間隔の印で見る（状態のページ用。2026-11-11）。

    10 MHz が来ていない（または specd が --clkin 0 でない）と fs は 1PPS に対して ppm 単位でずれ、1PPS の間隔
    （256 MHz のビートで数える）が ±1 ビート（±3.9 ppb）を外れる。コアの ADR 0006・0008。
    RUN の間（記録が来ている間）しか判定できず、1PPS が無いと判定できない。LMK04828 のホールドオーバがあるので、
    10 MHz を抜いてもすぐには外れない。
    """

    def __init__(self):
        self.n = 0
        self.t_last: Optional[float] = None
        self.t: Dict[str, Optional[float]] = {"int": None, "miss": None, "glitch": None}
        self.cnt: Dict[str, int] = {"int": 0, "miss": 0, "glitch": 0}

    def add(self, health: int, t: Optional[float] = None) -> None:
        t = time.time() if t is None else t
        self.n += 1
        self.t_last = t
        for k, bit in (("int", H_PPS_INT), ("miss", H_PPS_MISS), ("glitch", H_PPS_GLITCH)):
            if health & bit:
                self.t[k] = t
                self.cnt[k] += 1

    def info(self) -> Dict[str, object]:
        return {"n": self.n, "t_last": self.t_last,
                **{f"t_{k}": v for k, v in self.t.items()}, **{f"n_{k}": v for k, v in self.cnt.items()}}


class Recorder(threading.Thread):
    """データの口に 1 本つなぎ、記録を今の DiskDumps に渡し続ける。切れたらつなぎ直す。"""

    def __init__(self, host: str, port: int, retry_s: float = 2.0):
        super().__init__(daemon=True, name="recorder")
        self.host, self.port, self.retry_s = host, port, retry_s
        self.store: Optional[DiskDumps] = None
        self.last_seq: Optional[int] = None
        self.n_spec = 0
        self.n_drop_events = 0
        self.ref = RefWatch()
        self.connected = False
        self._stop = threading.Event()
        self._started_store = False

    def set_store(self, store: Optional[DiskDumps]) -> None:
        """次の START の EVENT から後の記録を store に溜める（None で溜めない）。"""
        self._started_store = False
        self.store = store

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except (OSError, EOFError, ValueError) as e:
                if not self._stop.is_set():
                    log.warning("データの口: %s。%.0f 秒後につなぎ直す", e, self.retry_s)
            self.connected = False
            self._stop.wait(self.retry_s)

    def _session(self) -> None:
        s = socket.create_connection((self.host, self.port), timeout=10)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        self.connected = True
        f = s.makefile("rb")
        t_ack = 0.0
        try:
            while not self._stop.is_set():
                if self.last_seq and time.monotonic() > t_ack:
                    s.sendall(int(self.last_seq).to_bytes(8, "little"))
                    t_ack = time.monotonic() + 0.2
                if not select.select([s], [], [], 0.2)[0]:
                    continue
                h = f.read(P.HDR.size)
                if len(h) < P.HDR.size:
                    raise EOFError("specd が切った")
                rtype, plen, crc, seq = P.parse_header(h)
                ver = P.header_ver(h)
                pl = f.read(plen)
                if len(pl) < plen:
                    raise EOFError("記録の途中で切れた")
                if seq and self.last_seq is not None and seq <= self.last_seq:
                    continue                      # 送り直し
                if seq:
                    self.last_seq = seq
                if P.crc32(pl) != crc:
                    log.error("データの口: CRC が合わない（seq %d）", seq)
                    continue
                self._record(rtype, pl, ver)
        finally:
            if self.last_seq:
                try:
                    s.sendall(int(self.last_seq).to_bytes(8, "little"))
                except OSError:
                    pass
            s.close()

    def _record(self, rtype: int, pl: bytes, ver: int) -> None:
        if rtype == P.T_EVENT:
            ev = P.decode(rtype, pl, ver)
            if ev.get("ev") == "START":
                self._started_store = True
            elif ev.get("ev") == "SKIP" and ev.get("why") == "CLEAR":
                pass                              # 自分で打った CLEAR で捨てた分
            elif ev.get("ev") in ("DROP", "SKIP", "ERROR"):
                self.n_drop_events += 1
                log.warning("specd の EVENT: %s", ev)
            return
        if rtype != P.T_SPEC or ver != 2:
            return
        st = self.store
        if st is None or not self._started_store:
            return
        d = P.decode(rtype, pl, ver)
        self.ref.add(int(d["health"]))
        st.add(d)
        self.n_spec += 1
