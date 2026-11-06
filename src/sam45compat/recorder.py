# SPDX-License-Identifier: BSD-3-Clause
"""specd のデータの口から 10.24 ms のダンプを受け、観測ごとの一時ファイルに溜める。

- 記録の形は YATSUGATAKE の s45proto（v2、fmt = float32、IF の昇順）。external/YATSUGATAKE/sw/pynq/s45proto.py を使う
- ACK（書き終えた最後の seq）を 0.2 秒おきに返す。specd はそこまでを溜まりから消す
- ダンプは窓ごとのファイル（float32 × 4096 の行）に書き足し、時刻の索引だけを手元に持つ。
  10.24 ms で 8 窓は ≒ 12.8 MB/s（1 時間で ≒ 46 GB）なので、メモリには持たない。観測が終わったら消す
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

from .binning import TINT_NS, Binned, bin_interval

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


def key_of(core: int, s: int) -> str:
    return f"{'ABCD'[core]}{s}"


class DiskDumps:
    """観測 1 つぶんのダンプ（窓ごとのファイル）。binning.DumpSource として束ねに使う。"""

    def __init__(self, path: str, expect: Dict[str, Tuple[float, float]], tint_ns: int = TINT_NS):
        """expect: 窓の鍵 → (記録の if_mhz, df [MHz])。この窓だけを溜め、軸を照らす。"""
        self.path = path
        self.expect = dict(expect)
        self.tint_ns = tint_ns
        os.makedirs(path, exist_ok=True)
        self._f = {k: open(os.path.join(path, f"{k}.f32"), "wb") for k in self.expect}
        self._t: Dict[str, list] = {k: [] for k in self.expect}
        self._fl: Dict[str, list] = {k: [] for k in self.expect}
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
        if d["utc_ns"] == 0 or d["health"] & P.H_NOTIME:
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
            self._f[k].write(data.tobytes())
            self._t[k].append(int(d["utc_ns"]))
            self._fl[k].append(fl)

    def interval(self, key: str, st_ns: int, ed_ns: int) -> Binned:
        with self._lock:
            if key not in self._f:
                return Binned(None, 0, 0)
            self._f[key].flush()
            t = np.asarray(self._t[key], dtype=np.int64)
            fl = np.asarray(self._fl[key], dtype=np.int64)
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
        spec = mm[: (len(mm) // NCH) * NCH].reshape(-1, NCH)
        if rows is not None:
            spec = spec[rows]
        return bin_interval(t, spec, st_ns, ed_ns, fl, self.tint_ns)

    def latest_end_ns(self) -> Optional[int]:
        with self._lock:
            ends = [v[-1] + self.tint_ns if v else None for v in self._t.values()]
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


class Recorder(threading.Thread):
    """データの口に 1 本つなぎ、記録を今の DiskDumps に渡し続ける。切れたらつなぎ直す。"""

    def __init__(self, host: str, port: int, retry_s: float = 2.0):
        super().__init__(daemon=True, name="recorder")
        self.host, self.port, self.retry_s = host, port, retry_s
        self.store: Optional[DiskDumps] = None
        self.last_seq: Optional[int] = None
        self.n_spec = 0
        self.n_drop_events = 0
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
        st.add(P.decode(rtype, pl, ver))
        self.n_spec += 1
