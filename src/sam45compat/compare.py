# SPDX-License-Identifier: BSD-3-Clause
"""段階 1 の比較: 同じ IF を入れた SAM45 の Array と YKSP の Array を比べる（docs/plan.md の 2-6）。

  python3 -m sam45compat.compare <SAM45 のロギング> <YKSP のロギング> [--pair A01:A05 ...] [--npz out.npz] [--png out.png]

合体したロギング（段階 1.1）なら、同じファイルを 2 回渡す。対の右側（YKSP）はゼロ点が 0 なので ZERO を引かない
（ZERO に入っている 8192 は SAM45 に合わせて書いただけ）。左側（SAM45）は ZERO を引く。

比べるもの（対ごと）:
- 周波数軸: 中心の sky・ch 間隔（SAM45 は mapping.sam45_window、YKSP は L1 の dfqdat を丸めずに書いたもの）
- 時刻: 積分の鍵（スキャン ID・開始・終了・種類）の一致
- 値: スキャンの種類ごとの YKSP / SAM45 の比（ch の平均）と、その時間変化（積分ごとの比のばらつき）
- R / SKY の比（各ファイルの iary_ifatt で R を戻した後）と Tsys（ファイルの dtsys と、式で出し直した値）
- 較正したスペクトル（PSW）: Ta* = T_R·(ON − OFF) / (R·10^(IFATT/10) − OFF)。ON ごとに時刻の最も近い OFF・直前の R を使い、
  ON について平均。YKSP を SAM45 の sky の軸に補間して、差の rms・ピーク・線の中心のずれ
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import mapping as mp
from . import samlog as sl
from . import yksp as yk

DEFAULT_PAIRS = {"A01": "A05", "A02": "A06", "A03": "A07", "A04": "A08",
                 "A17": "A21", "A18": "A22", "A19": "A23", "A20": "A24"}


# ---- 軸 ----

def sky_axis_sam45(obs, idx: int, ifm: mp.Sam45IfModel = mp.Sam45IfModel()) -> np.ndarray:
    w = mp.sam45_window(obs, idx, ifm)
    return w.sky_a + w.sky_b * w.if_of_ch * 1e6


def sky_axis_dfqdat(obs, idx: int) -> np.ndarray:
    """L1 の dfqdat の 2 点を結ぶ直線（YKSP は丸めずに書いている）。"""
    (c0, c1), (f0, f1) = obs.dfqdat_ch[idx], obs.dfqdat_fq[idx]
    c = np.arange(1, sl.CHMAX_LOGGING + 1)
    return f0 + (c - c0) * (f1 - f0) / (c1 - c0)


# ---- ロギングを Array ごとに ----

@dataclass
class ArrayData:
    idx: int
    keys: List[tuple] = field(default_factory=list)
    spec: List[np.ndarray] = field(default_factory=list)
    tsys: List[float] = field(default_factory=list)
    flag: List[int] = field(default_factory=list)

    def by_key(self) -> Dict[tuple, int]:
        return {k: i for i, k in enumerate(self.keys)}


def collect(records: Sequence[sl.Record], arrays) -> Tuple[sl.Record, Dict[int, ArrayData]]:
    arrays = set(arrays)
    obs = next(r for r in records if r.kind == "L1")
    out = {a: ArrayData(a) for a in arrays}
    for r in records:
        if r.kind != "L2":
            continue
        a = sl.array_index(sl.cstr(r.s.cary_name))
        if a in out:
            d = out[a]
            d.keys.append(yk.integration_key(r.s))
            d.spec.append(np.array(r.s.fary_data, dtype=np.float64))
            d.tsys.append(float(r.s.dtsys))
            d.flag.append(int(r.s.idmy_flag))
    return obs, out


def zero_level(d: ArrayData) -> np.ndarray:
    z = [s for k, s in zip(d.keys, d.spec) if k[3] == "ZERO"]
    return np.mean(z, axis=0) if z else np.zeros(sl.CHMAX_LOGGING)


def calibrated(d: ArrayData, zero: np.ndarray, ifatt_db: float, t_load_k: float) -> Optional[np.ndarray]:
    """PSW の Ta* の平均（ON について）。R・OFF・ON が無ければ None。"""
    g = 10 ** (ifatt_db / 10)
    rows = [(k, s - zero, f) for k, s, f in zip(d.keys, d.spec, d.flag) if not f]
    offs = [(k[1], s) for k, s, _ in rows if k[3] == "OFF"]
    out = []
    last_r = None
    for k, s, _ in rows:
        if k[3] == "R":
            last_r = s * g
        elif k[3] == "ON" and last_r is not None and offs:
            t_off, off = min(offs, key=lambda o: abs(o[0] - k[1]))
            den = last_r - off
            with np.errstate(divide="ignore", invalid="ignore"):
                out.append(t_load_k * (s - off) / den)
    return np.nanmean(out, axis=0) if out else None


# ---- 比べる ----

@dataclass
class PairResult:
    left: str
    right: str
    lines: List[str] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)
    arrays: Dict[str, np.ndarray] = field(default_factory=dict)


def _center(ax: np.ndarray) -> float:
    return float((ax[2047] + ax[2048]) / 2)


def compare_pair(obs_l, dl: ArrayData, obs_r, dr: ArrayData, t_load_k: float,
                 ifm: mp.Sam45IfModel) -> PairResult:
    L, R = sl.array_label(dl.idx), sl.array_label(dr.idx)
    pr = PairResult(L, R)
    m = pr.metrics
    say = pr.lines.append
    # 軸
    ax_l = sky_axis_sam45(obs_l, dl.idx, ifm)
    ax_r = sky_axis_dfqdat(obs_r, dr.idx)
    m["center_l_hz"], m["center_r_hz"] = _center(ax_l), _center(ax_r)
    m["df_l_hz"], m["df_r_hz"] = float(abs(ax_l[1] - ax_l[0])), float(abs(ax_r[1] - ax_r[0]))
    say(f"軸: 中心 {L} {m['center_l_hz'] / 1e9:.6f} GHz / {R} {m['center_r_hz'] / 1e9:.6f} GHz"
        f"（差 {(m['center_r_hz'] - m['center_l_hz']) / 1e3:+.2f} kHz）、"
        f"ch 間隔 {m['df_l_hz']:.2f} / {m['df_r_hz']:.2f} Hz、"
        f"向き {'同じ' if np.sign(ax_l[1] - ax_l[0]) == np.sign(ax_r[1] - ax_r[0]) else '逆'}")
    # 時刻
    kl, kr = dl.by_key(), dr.by_key()
    common = [k for k in dl.keys if k in kr]
    m["n_left"], m["n_right"], m["n_common"] = len(dl.keys), len(dr.keys), len(common)
    m["n_right_invalid"] = int(sum(dr.flag))
    say(f"積分: {L} {len(dl.keys)}・{R} {len(dr.keys)}・共通 {len(common)}（{R} の無効 {m['n_right_invalid']}）")
    # 値の比
    zl = zero_level(dl)
    zr = np.zeros(sl.CHMAX_LOGGING)                       # YKSP のゼロ点は 0
    ratios: Dict[str, List[float]] = {}
    for k in common:
        sl_ = dl.spec[kl[k]] - zl
        sr_ = dr.spec[kr[k]] - zr
        if k[3] == "ZERO" or dr.flag[kr[k]]:
            continue
        ratios.setdefault(k[3], []).append(float(np.mean(sr_) / np.mean(sl_)))
    for sc, v in sorted(ratios.items()):
        v = np.asarray(v)
        m[f"ratio_{sc}"] = float(v.mean())
        m[f"ratio_{sc}_cv"] = float(v.std() / v.mean()) if len(v) > 1 else 0.0
        say(f"値の比 {R}/{L}（{sc}）: {v.mean():.4g}（積分ごとのばらつき {m[f'ratio_{sc}_cv'] * 100:.3f} %、{len(v)} 個）")
    # R/SKY と Tsys
    att_l, att_r = float(obs_l.iary_ifatt[dl.idx]), float(obs_r.iary_ifatt[dr.idx])
    for side, d, z, att in (("l", dl, zl, att_l), ("r", dr, zr, att_r)):
        rr = [np.mean(s - z) for k, s in zip(d.keys, d.spec) if k[3] == "R"]
        ss = [np.mean(s - z) for k, s in zip(d.keys, d.spec) if k[3] == "SKY"]
        if rr and ss:
            m[f"y_{side}"] = float(rr[0] * 10 ** (att / 10) / ss[0])
            m[f"tsys_calc_{side}"] = yk.tsys_k(rr[0], ss[0], att, t_load_k)
        ts = [t for k, t in zip(d.keys, d.tsys) if k[3] not in ("ZERO", "R") and t > 0]
        if ts:
            m[f"tsys_file_{side}"] = float(np.mean(ts))
    if "y_l" in m and "y_r" in m:
        say(f"R/SKY（IFATT を戻した後）: {L} {m['y_l']:.4f} / {R} {m['y_r']:.4f}（IFATT {att_l:g} / {att_r:g} dB）")
        say(f"Tsys（式）: {L} {m['tsys_calc_l']:.1f} K / {R} {m['tsys_calc_r']:.1f} K、"
            f"ファイルの dtsys: {L} {m.get('tsys_file_l', 0):.1f} / {R} {m.get('tsys_file_r', 0):.1f} K")
    # 較正したスペクトル
    ta_l = calibrated(dl, zl, att_l, t_load_k)
    ta_r = calibrated(dr, zr, att_r, t_load_k)
    pr.arrays.update({f"{L}_sky_hz": ax_l, f"{R}_sky_hz": ax_r})
    if ta_l is not None and ta_r is not None:
        o_l, o_r = np.argsort(ax_l), np.argsort(ax_r)
        ta_r_on_l = np.interp(ax_l, ax_r[o_r], ta_r[o_r], left=np.nan, right=np.nan)
        pr.arrays.update({f"{L}_ta": ta_l, f"{R}_ta": ta_r, f"{R}_ta_on_{L}": ta_r_on_l})
        n = len(ax_l)
        core = slice(n // 20, n - n // 20)                # 端の 5 % を除く
        diff = (ta_r_on_l - ta_l)[core]
        ok = np.isfinite(diff)
        m["ta_diff_mean_k"] = float(np.mean(diff[ok]))
        m["ta_diff_rms_k"] = float(np.std(diff[ok]))
        noise = np.std(np.diff(ta_l[core])) / np.sqrt(2)
        m["ta_noise_l_k"] = float(noise)
        p = int(np.nanargmax(ta_l[core])) + core.start
        m["peak_l_k"], m["peak_r_k"] = float(ta_l[p]), float(ta_r_on_l[p])
        say(f"Ta*: 差の平均 {m['ta_diff_mean_k'] * 1e3:+.2f} mK・差の rms {m['ta_diff_rms_k'] * 1e3:.2f} mK"
            f"（{L} の ch の雑音 {noise * 1e3:.2f} mK）、ピーク {L} {m['peak_l_k']:.4f} K / {R} {m['peak_r_k']:.4f} K"
            f" at {ax_l[p] / 1e9:.6f} GHz")
        if m["peak_l_k"] > 5 * noise:
            w = slice(max(p - 20, 0), min(p + 21, n))
            def cen(t):
                t = np.clip(np.nan_to_num(t[w]), 0, None)
                return float(np.sum(ax_l[w] * t) / np.sum(t)) if np.sum(t) > 0 else np.nan
            m["line_shift_hz"] = cen(ta_r_on_l) - cen(ta_l)
            say(f"線の中心のずれ（{R} − {L}、ピークの ±20 ch の重心）: {m['line_shift_hz'] / 1e3:+.3f} kHz")
    else:
        say("Ta*: R・OFF・ON が揃わない（PSW でない）ので出さない")
    return pr


def compare(sam: Sequence[sl.Record], yksp: Sequence[sl.Record], pairs: Dict[str, str] = None,
            t_load_k: float = yk.T_LOAD_K, ifm: mp.Sam45IfModel = mp.Sam45IfModel()) -> List[PairResult]:
    pairs = DEFAULT_PAIRS if pairs is None else pairs
    obs_l = next(r for r in sam if r.kind == "L1").s
    obs_r = next(r for r in yksp if r.kind == "L1").s
    want = {sl.array_index(a): sl.array_index(b) for a, b in pairs.items()
            if obs_l.iary_usefg[sl.array_index(a)] and obs_r.iary_usefg[sl.array_index(b)]}
    _, dl = collect(sam, want.keys())
    _, dr = collect(yksp, want.values())
    return [compare_pair(obs_l, dl[a], obs_r, dr[b], t_load_k, ifm) for a, b in sorted(want.items())]


def _png(results: List[PairResult], path: str) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    fig, axs = plt.subplots(len(results), 1, figsize=(10, 3 * len(results)), squeeze=False)
    for ax, r in zip(axs[:, 0], results):
        L, R = r.left, r.right
        if f"{L}_ta" in r.arrays:
            x = r.arrays[f"{L}_sky_hz"] / 1e9
            ax.plot(x, r.arrays[f"{L}_ta"], lw=0.6, label=f"SAM45 {L}")
            ax.plot(x, r.arrays[f"{R}_ta_on_{L}"], lw=0.6, label=f"YKSP {R}")
            ax.set_xlabel("sky [GHz]")
            ax.set_ylabel("Ta* [K]")
            ax.legend()
        ax.set_title(f"{L} / {R}")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    return True


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="段階 1: SAM45 と YKSP を比べる")
    ap.add_argument("sam45")
    ap.add_argument("yksp", help="YKSP のロギング（合体したものなら sam45 と同じファイル）")
    ap.add_argument("--pair", action="append", default=None, help="SAM45:YKSP の Array（既定 A01:A05 … A20:A24）")
    ap.add_argument("--t-load", type=float, default=yk.T_LOAD_K)
    ap.add_argument("--sam45-if-descending", action="store_true")
    ap.add_argument("--npz", default=None, help="軸と Ta* を npz で書く（図は手元で）")
    ap.add_argument("--png", default=None, help="図（matplotlib があれば）")
    a = ap.parse_args(argv)
    pairs = dict(p.split(":") for p in a.pair) if a.pair else None
    res = compare(sl.read_file(a.sam45), sl.read_file(a.yksp), pairs, a.t_load,
                  mp.Sam45IfModel(ascending=not a.sam45_if_descending))
    if not res:
        raise SystemExit("比べる対が無い（両方で使っている Array の対が無い）")
    for r in res:
        print(f"== {r.left}（SAM45）/ {r.right}（YKSP）")
        for line in r.lines:
            print("  " + line)
    if a.npz:
        np.savez(a.npz, **{k: v for r in res for k, v in r.arrays.items()})
        print(f"→ {a.npz}")
    if a.png:
        print(f"→ {a.png}" if _png(res, a.png) else "matplotlib が無いので図は出さない（--npz で手元で描く）")


if __name__ == "__main__":
    main()
