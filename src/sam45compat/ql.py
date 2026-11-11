# SPDX-License-Identifier: BSD-3-Clause
"""QL: 肩代わりする窓のスペクトルを JSON に書き、Apache で配る（http://<サーバー>/ql/。2026-11-11）。

- 生のスペクトル: 今の観測の 10.24 ms のダンプ（recorder.DiskDumps）の直近 ql_avg_s 秒の平均を、ql_s ごとに書く。
  観測していない間はボードのデータを溜めていないので出さない
- 積分ごと: L2 を作るたびに（lc.Session）、窓ごとに直近の OFF を覚え、ON が来たら Ta* = Tsys × (ON − OFF) / OFF を作る。
  Tsys（SKY の後に決まる。yksp.YkspBuilder）がまだ無ければ (ON − OFF) / OFF のまま出す。観測が終わっても最後の値を残す
- どちらも L2 の ch の順（ch 1..4096）で、ql_nbin 点に平均して減らす。横軸は天空周波数（ch 1 と ch 4096 の値を添える）
- 速度の横軸のため、L1 の dstrv（天体の速度 [m/s]）と Array ごとの dcent_freq（静止周波数の既定）を添える（vel_info）。
  45m のロギングの周波数はドップラー追尾した系（dstrv で動く天体の線が静止周波数に来る）なので、
  V = dstrv + c·(1 − f / f_rest)（電波の定義）。サンプル（R Cas の SiO v=1、2026-09-10）で 24.95 km/s に出ることを確かめた
- 表示用。ここが失敗しても LC は止めない（status.py と同じ）

設計は docs/design/status-page.md の「QL」。
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from typing import TYPE_CHECKING, Dict, List, Optional

import numpy as np

from . import mapping as mp
from . import samlog as sl

if TYPE_CHECKING:
    from .lc import LC

log = logging.getLogger("sam45compat")

VERSION = 1
SCAN_ON = "ON"
SCAN_OFF = "OFF"


def reduce(spec: np.ndarray, nbin: int) -> List[Optional[float]]:
    """4096 ch を nbin 点に平均し、有効数字 5 桁の数の並びにする（有限でない値は None）。"""
    a = np.asarray(spec, dtype=np.float64)
    if nbin and len(a) % nbin == 0 and len(a) > nbin:
        a = a.reshape(nbin, -1).mean(axis=1)
    return [float(f"{v:.5g}") if math.isfinite(v) else None for v in a.tolist()]


def vel_info(obs, plan: mp.Plan) -> Dict[str, object]:
    """速度の横軸に要るもの（L1 から）。ivdef・ivref は値をそのまま添える（意味の対応は未確認。サンプルは 1・1）。"""
    return {"vsrc_kms": float(obs.dstrv) / 1e3, "vdef": int(obs.ivdef), "vref": int(obs.ivref),
            "object": sl.cstr(obs.cobj_name),
            "rest_ghz": {i: float(obs.dcent_freq[i]) / 1e9 for i in plan.windows}}


def axis(w: mp.YkWindow, vel: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """窓の名前と、横軸（L2 の ch 1 と ch 4096 の天空周波数 [GHz]、IF [MHz]、静止周波数の既定 [GHz]）。"""
    ifs = w.record_if_axis()
    if w.reverse:
        ifs = ifs[::-1]
    sky = np.asarray(w.sky_hz, dtype=float)
    return {"array": sl.array_label(w.array), "adc": w.slot.adc, "stream": w.slot.stream,
            "sky0_ghz": round(float(sky[0]) / 1e9, 9), "sky1_ghz": round(float(sky[-1]) / 1e9, 9),
            "if0_mhz": round(float(ifs[0]), 6), "if1_mhz": round(float(ifs[-1]), 6),
            "rest_ghz": (vel or {}).get("rest_ghz", {}).get(w.array)}


class IntegrationQl:
    """1 つの観測の、積分ごとの Ta*（lc.Session が L2 を作るたびに add する）。"""

    def __init__(self, log_id: str, plan: mp.Plan, nbin: int = 1024, obs=None):
        """obs: L1（LogObs。速度の横軸のため。無ければ速度を出さない）。"""
        self.log_id = log_id
        self.plan = plan
        self.vel = vel_info(obs, plan) if obs is not None else None
        self.nbin = nbin
        self.off: Dict[int, np.ndarray] = {}          # 窓 → 直近の OFF（L2 の順）
        self.t_off: Dict[int, float] = {}
        self.ta: Dict[int, Dict[str, object]] = {}    # 窓 → 直近の ON の Ta*（表示用に減らしたもの）
        self.n_on = 0
        self.last_scan: Optional[str] = None
        self.t_last: Optional[float] = None

    def add(self, scan: str, scan_id: str, results, t: Optional[float] = None) -> None:
        t = time.time() if t is None else t
        self.last_scan, self.t_last = scan, t
        if scan not in (SCAN_ON, SCAN_OFF):
            return
        on = False
        for r in results:
            if r.array not in self.plan.windows or not r.valid or r.spec is None:
                continue
            spec = np.asarray(r.spec, dtype=np.float64)
            if scan == SCAN_OFF:
                self.off[r.array] = spec
                self.t_off[r.array] = t
                continue
            off = self.off.get(r.array)
            if off is None:
                continue
            with np.errstate(divide="ignore", invalid="ignore"):
                ratio = (spec - off) / off
            tsys = float(r.record.s.dtsys)
            if not math.isfinite(tsys):
                tsys = 0.0
            self.ta[r.array] = {"t": t, "t_off": self.t_off[r.array], "scan_id": scan_id,
                                "tsys": tsys if tsys > 0 else None, "unit": "K" if tsys > 0 else "ratio",
                                "spec": reduce(ratio * tsys if tsys > 0 else ratio, self.nbin)}
            on = True
        if on:
            self.n_on += 1

    def snapshot(self) -> List[Dict[str, object]]:
        out = []
        for i, w in sorted(self.plan.windows.items(), key=lambda x: (x[1].slot.adc, x[1].slot.stream)):
            d = axis(w, self.vel)
            d.update(self.ta.get(i) or {"spec": None})
            out.append(d)
        return out


def realtime(session, avg_s: float, nbin: int) -> Optional[Dict[str, object]]:
    """今の観測の、直近 avg_s 秒の生のスペクトル（窓ごと）。束ねる用意ができていなければ None。"""
    b = session.builder
    if b is None or not b.plan.windows:
        return None
    dumps = b.dumps
    last = dumps.latest_end_ns()
    if last is None:
        return None
    st = last - int(avg_s * 1e9)
    vel = session.ql.vel if getattr(session, "ql", None) is not None else None
    wins = []
    for i, w in sorted(b.plan.windows.items(), key=lambda x: (x[1].slot.adc, x[1].slot.stream)):
        d = axis(w, vel)
        bn = dumps.interval(w.slot.key, st, last)
        if bn.spec is None:
            d.update({"spec": None, "n": 0})
        else:
            d.update({"spec": reduce(w.l2_order(bn.spec), nbin), "n": bn.n, "n_expected": bn.n_expected,
                      "flags": bn.flags})
        wins.append(d)
    return {"t_end": last / 1e9, "avg_s": avg_s, "windows": wins}


class QlWriter:
    """QL の JSON を定期的に書く（別のスレッド。status.StatusWriter と同じ書き方）。"""

    def __init__(self, lc: "LC", path: str, interval_s: float = 1.0, avg_s: float = 1.0, nbin: int = 1024):
        self.lc = lc
        self.path = path
        self.interval_s = interval_s
        self.avg_s = avg_s
        self.nbin = nbin
        self._stop = threading.Event()
        self._err_logged = False

    def start(self) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        if not os.path.isdir(d):
            log.error("QL の置き場所が無い: %s（QL は書かない）", d)
            return
        threading.Thread(target=self._run, daemon=True, name="ql").start()
        log.info("QL を書く: %s（%.1f s ごと、直近 %.1f s の平均）", self.path, self.interval_s, self.avg_s)

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> Dict[str, object]:
        d: Dict[str, object] = {"version": VERSION, "t": time.time(), "interval_s": self.interval_s}
        cur = None
        for s in list(self.lc.sessions.values()):
            if s.builder is not None and s.builder.plan.windows and not s.done:
                cur = s
                break
        if cur is not None:
            d["log_id"] = cur.name.log_id
            d["phase"] = cur.phase
            d["bit"] = cur.builder.plan.kind
            try:
                d["realtime"] = realtime(cur, self.avg_s, self.nbin)
            except Exception as e:          # 束ねの失敗（時刻の順など）で QL を止めない
                d["realtime"] = None
                d["realtime_error"] = repr(e)
        else:
            d["realtime"] = None
        iq = cur.ql if cur is not None and cur.ql is not None else self.lc.ql_last
        vel = iq.vel if iq is not None else None
        d["vel"] = None if vel is None else {k: v for k, v in vel.items() if k != "rest_ghz"}
        if iq is not None:
            d["integration"] = {"log_id": iq.log_id, "current": cur is not None and iq is cur.ql,
                                "n_on": iq.n_on, "last_scan": iq.last_scan, "t_last": iq.t_last,
                                "windows": iq.snapshot()}
        else:
            d["integration"] = None
        return d

    def write(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.snapshot(), f, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.write()
                self._err_logged = False
            except Exception as e:
                if not self._err_logged:
                    log.error("QL を書けない: %s", e)
                    self._err_logged = True
            self._stop.wait(max(0.0, self.interval_s - (time.monotonic() - t0)))
