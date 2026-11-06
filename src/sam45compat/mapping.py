# SPDX-License-Identifier: BSD-3-Clause
"""SAM45 の L1 の設定 → YATSUGATAKE の bit と SET、と f2 の L1 の書き換え。

方針は docs/design/standin-study.md の「周波数軸」「値と IFATT」。

- SAM45: 4000 MSPS、第 2 ナイキスト（IF 2000〜4000 MHz）を 524,288 ch に分け、窓ごとに
  istart_chan〜iend_chan を ichan_avg 個ずつ束ねて 4096 ch を出す
- YATSUGATAKE: 4096 MSPS、IF 2048〜4096 MHz。SAM45-Fine（窓 256〜16 MHz、ADC ごとに 2 窓）と
  SAM45-Wide（2048 / 1024 / 512 MHz、束ね B = 4 / 2 / 1、B は ADC ごとに 1 つ）
- 記録の軸は IF(i) = if_mhz ＋ (i − 2047)·df（IF の昇順、YATSUGATAKE の interface.md 6.）
- 帯域はどれも YATSUGATAKE のほうが 2.4 % 広く、ch 間隔が合わない。f2 で、差し替える Array の
  L1 の周波数の欄を YATSUGATAKE の実際の値に書き換える
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import samlog as sl

NCH = 4096

# ---- SAM45 ----
SAM45_IF_LO_MHZ = 2000.0
SAM45_IF_HI_MHZ = 4000.0
SAM45_NRAW = 524288
SAM45_DK_MHZ = (SAM45_IF_HI_MHZ - SAM45_IF_LO_MHZ) / SAM45_NRAW   # 3.8147 kHz

# SAM45 の帯域 [MHz] → YATSUGATAKE の (bit の種類, 幅 [MHz])
BW_MAP = {
    2000.0: ("wide", 2048.0),
    1000.0: ("wide", 1024.0),
    500.0: ("wide", 512.0),
    250.0: ("fine", 256.0),
    125.0: ("fine", 128.0),
    62.5: ("fine", 64.0),
    31.25: ("fine", 32.0),
    15.625: ("fine", 16.0),
}

# ---- YATSUGATAKE ----
YK_FS_MHZ = 4096.0
YK_IF_LO_MHZ = 2048.0
YK_IF_HI_MHZ = 4096.0
WIDE_N_PHYS = 32768
WIDE_B = {2048.0: 4, 1024.0: 2, 512.0: 1}


class MappingError(ValueError):
    pass


@dataclass(frozen=True)
class Sam45IfModel:
    """SAM45 の 524,288 ch の番号 k と IF の対応。

    **未確認**（docs/plan.md の調査）: 向き（k が増えると IF が上がるか）と、ch の中心の位置
    （k_offset。k の ch の中心が k + k_offset）。サンプルの窓は中央（3 GHz）にあり、向きでは区別できない。
    段階 1 の同じ IF の比較で確かめる。
    """
    ascending: bool = True
    k_offset: float = 0.0

    def if_mhz(self, k):
        x = (np.asarray(k, dtype=float) + self.k_offset) * SAM45_DK_MHZ
        return SAM45_IF_LO_MHZ + x if self.ascending else SAM45_IF_HI_MHZ - x


@dataclass(frozen=True)
class Slot:
    """肩代わりする Array の受け持ち: YATSUGATAKE の ADC（'A'〜'D'）と窓（流れ 0 / 1）。"""
    adc: str
    stream: int

    @property
    def key(self) -> str:
        return f"{self.adc}{self.stream}"


# 既定の割り当て（README の「目的」）: IF 入力 5〜8 → ADC_A〜D、1 つ目の窓 → 流れ 0、2 つ目 → 流れ 1
DEFAULT_ASSIGN: Dict[str, Slot] = {
    "A05": Slot("A", 0), "A06": Slot("B", 0), "A07": Slot("C", 0), "A08": Slot("D", 0),
    "A21": Slot("A", 1), "A22": Slot("B", 1), "A23": Slot("C", 1), "A24": Slot("D", 1),
}


@dataclass
class Sam45Window:
    """SAM45 の 1 Array の窓（L1 から読んだもの）。ch は 1 始まり（L1 の dfqdat_ch と同じ）。"""
    index: int
    bw_mhz: float
    k_start: int
    k_end: int
    avg: int
    if_of_ch: np.ndarray        # 出力の ch 1..4096 の IF [MHz]
    sky_a: float                # sky [Hz] = sky_a ＋ sky_b·IF [MHz]·1e6
    sky_b: float

    @property
    def if_center_mhz(self) -> float:
        return float((self.if_of_ch[0] + self.if_of_ch[-1]) / 2)

    @property
    def if_ascending(self) -> bool:
        """出力の ch が増えると IF が上がるか。"""
        return bool(self.if_of_ch[-1] > self.if_of_ch[0])


@dataclass
class YkWindow:
    """YATSUGATAKE の 1 窓の予定。"""
    array: int                  # 肩代わりする Array の添字
    slot: Slot
    kind: str                   # "fine" / "wide"
    bw_mhz: float
    df_mhz: float
    if_mhz: float               # 記録の if_mhz（送る 2047 番目の ch の IF）
    set_keys: Dict[str, str]
    reverse: bool               # True なら記録（IF の昇順）を逆にして L2 に入れる
    sky_hz: np.ndarray          # L2 の ch 1..4096 の sky [Hz]（reverse を考えた後）

    def record_if_axis(self) -> np.ndarray:
        """記録の ch の IF [MHz]（IF の昇順）。"""
        return self.if_mhz + (np.arange(NCH) - 2047) * self.df_mhz

    def l2_order(self, spec: np.ndarray) -> np.ndarray:
        """記録のスペクトル（IF の昇順）→ L2 の ch の順。"""
        return spec[::-1] if self.reverse else spec


@dataclass
class Plan:
    kind: Optional[str]                              # 使う bit の種類（"fine" / "wide"）。何も取れなければ None
    windows: Dict[int, YkWindow] = field(default_factory=dict)
    skipped: Dict[int, str] = field(default_factory=dict)   # 取れない Array → 理由（idmy_flag = 1 にする）

    def set_args(self, tint_s: float = 0.01024, extra: Optional[Dict[str, str]] = None) -> List[str]:
        """specd の SET に渡す key=val の並び。"""
        args = {"tint": f"{tint_s:g}", "fmt": "f32"}
        for w in self.windows.values():
            args.update(w.set_keys)
        if extra:
            args.update(extra)
        return [f"{k}={v}" for k, v in args.items()]


# ---- SAM45 の窓を読む ----

def _spw_of(index: int) -> int:
    """1 つ目の窓 = A01〜A16、2 つ目の窓 = A17〜A32。"""
    return 0 if index < 16 else 1


def _sub_of(obs, index: int) -> int:
    """サブアレイ: A01〜A08（と 2 つ目の窓の A17〜A24）が 0、A09〜A16（A25〜A32）が 1。
    分割が OFF のときは全部 0（サブアレイ 0 の設定を使う）。"""
    mode = sl.cstr(obs.csub_array)
    if mode in ("OFF", ""):
        return 0
    if mode != "ON":
        raise MappingError(f"サブアレイ分割の値 {mode!r} を知らない")
    return 0 if index % 16 < 8 else 1


def sam45_window(obs, index: int, ifm: Sam45IfModel) -> Sam45Window:
    """L1（LogObs）から Array の窓を読む。"""
    if not obs.iary_usefg[index]:
        raise MappingError(f"{sl.array_label(index)} は使われていない")
    if obs.ichannel[index] != NCH:
        raise MappingError(f"{sl.array_label(index)} の ch 数が {obs.ichannel[index]}")
    sub, spw = _sub_of(obs, index), _spw_of(index)
    if spw >= obs.in_spec_window[sub]:
        raise MappingError(f"{sl.array_label(index)} の窓 {spw + 1} が設定されていない")
    k0, k1 = obs.istart_chan[sub][spw], obs.iend_chan[sub][spw]
    avg = obs.ichan_avg[sub][spw]
    if avg <= 0 or k1 - k0 + 1 != NCH * avg:
        raise MappingError(f"{sl.array_label(index)}: istart {k0}・iend {k1}・avg {avg} が 4096 ch にならない")
    bw = obs.dbebw[index] / 1e6
    if abs(bw - NCH * avg * SAM45_DK_MHZ) > 1e-6 * bw:
        raise MappingError(f"{sl.array_label(index)}: 帯域 {bw} MHz と istart〜iend の幅が合わない")
    c = np.arange(1, NCH + 1)
    k = k0 + (c - 1) * avg + (avg - 1) / 2
    if_c = ifm.if_mhz(k)
    # sky = a + b·IF。dfqdat_fq は 10 kHz に丸められている（サンプル）ので、傾きは向きだけを
    # dfqdat の 2 点から取って ±1 にし、窓の中心（ch 2048.5）の sky を dfqdat_f0 とする
    (c_a, c_b), (f_a, f_b) = obs.dfqdat_ch[index], obs.dfqdat_fq[index]
    i_a, i_b = if_c[int(round(c_a)) - 1], if_c[int(round(c_b)) - 1]
    if i_a == i_b:
        raise MappingError(f"{sl.array_label(index)}: dfqdat の 2 点が同じ ch")
    b_raw = (f_b - f_a) / ((i_b - i_a) * 1e6)
    if abs(abs(b_raw) - 1) > 1e-3:
        raise MappingError(f"{sl.array_label(index)}: sky と IF の傾きが {b_raw:.6f}（±1 のはず）")
    b = 1.0 if b_raw > 0 else -1.0
    if_mid = (if_c[0] + if_c[-1]) / 2
    a = obs.dfqdat_f0[index] - b * if_mid * 1e6
    return Sam45Window(index, bw, k0, k1, avg, if_c, a, b)


# ---- YATSUGATAKE の窓を決める ----

def _fine_window(if_req: float, w: float) -> Tuple[float, float]:
    """SAM45-Fine: 中心を ch の格子（w/4096）に丸める（YATSUGATAKE の window.window_params と同じ）。
    → (実際の IF の中心 = 記録の if_mhz, df)"""
    df = w / NCH
    c = round((YK_FS_MHZ - if_req) / df) * df
    if not (w / 2 <= c <= YK_FS_MHZ / 2 - w / 2):
        raise MappingError(f"窓が YATSUGATAKE の帯域（IF 2048〜4096 MHz）からはみ出す: IF {YK_FS_MHZ - c:.4f} ± {w / 2}")
    return YK_FS_MHZ - c, df


def _wide_window(if_req: float, w: float) -> Tuple[int, int, float, float]:
    """SAM45-Wide: 束ね B と流れの最初の ch S0 を、中心が if_req に最も近くなるように選ぶ（範囲に収める）。
    YATSUGATAKE の wide.py の if_mhz と同じ式。→ (B, S0, 記録の if_mhz, df)"""
    b = WIDE_B[w]
    n = WIDE_N_PHYS // b
    df = YK_FS_MHZ / n
    off = (b - 1) / (2 * b)
    s0_max = n // 2 - NCH
    s0 = round((YK_FS_MHZ - if_req) / df - (NCH / 2 - 0.5) - off)
    s0 = min(max(s0, 0), s0_max)
    # 流れの ch j の IF = 4096 − (S0 + j + off)·df。記録は IF の昇順（i = 4095 − j）
    if_2047 = YK_FS_MHZ - (s0 + (NCH - 1 - 2047) + off) * df
    return b, s0, if_2047, df


def plan(obs, assign: Dict[str, Slot] = None, ifm: Sam45IfModel = Sam45IfModel(),
         shift: Optional[int] = None) -> Plan:
    """L1 と割り当てから、YATSUGATAKE の設定の予定を作る。

    - 使われていない Array は予定に入れない（skipped にも入れない）
    - Wide と Fine が混ざるときは Fine（250 MHz 以下）を取り、Wide の Array は skipped
    - Wide は B が ADC ごとに 1 つ。同じ ADC の 2 窓で B が違えば流れ 1 を skipped。B 4 は流れ 1 が無い
    """
    assign = DEFAULT_ASSIGN if assign is None else assign
    targets: List[Tuple[int, Slot]] = sorted(
        (sl.array_index(name), slot) for name, slot in assign.items())
    sam: Dict[int, Sam45Window] = {}
    p = Plan(kind=None)
    for idx, slot in targets:
        if not obs.iary_usefg[idx]:
            continue
        bw = obs.dbebw[idx] / 1e6
        if bw not in BW_MAP:
            p.skipped[idx] = f"SAM45 の帯域 {bw} MHz に対応する YATSUGATAKE の窓が無い"
            continue
        try:
            sam[idx] = sam45_window(obs, idx, ifm)
        except MappingError as e:
            p.skipped[idx] = str(e)
    kinds = {BW_MAP[s.bw_mhz][0] for s in sam.values()}
    if not kinds:
        return p
    p.kind = "fine" if "fine" in kinds else "wide"
    wide_b: Dict[str, int] = {}
    for idx, s in sorted(sam.items()):
        slot = dict(targets)[idx]
        kind, w = BW_MAP[s.bw_mhz]
        if kind != p.kind:
            p.skipped[idx] = f"Wide と Fine が混ざる観測（{p.kind} を取った）"
            continue
        try:
            if kind == "fine":
                if_mhz, df = _fine_window(s.if_center_mhz, w)
                keys = {f"{slot.key}.if": f"{if_mhz:.10g}", f"{slot.key}.bw": f"{w:g}"}
                if shift is not None:
                    keys[f"{slot.key}.shift"] = str(shift)
            else:
                b, s0, if_mhz, df = _wide_window(s.if_center_mhz, w)
                if b == 4 and slot.stream == 1:
                    raise MappingError("Wide の 2048 MHz は窓が 1 つ")
                if wide_b.setdefault(slot.adc, b) != b:
                    raise MappingError(f"ADC {slot.adc} の束ねが窓ごとに違う（B {wide_b[slot.adc]} と {b}）")
                keys = {f"{slot.adc}.b": str(b), f"{slot.key}.s0": str(s0)}
                if shift is not None:
                    keys[f"{slot.adc}.shift"] = str(shift)
        except MappingError as e:
            p.skipped[idx] = str(e)
            continue
        if_rec = if_mhz + (np.arange(NCH) - 2047) * df
        reverse = not s.if_ascending          # SAM45 の ch の向きに合わせる
        if_l2 = if_rec[::-1] if reverse else if_rec
        sky = s.sky_a + s.sky_b * if_l2 * 1e6
        p.windows[idx] = YkWindow(idx, slot, kind, w, df, if_mhz, keys, reverse, sky)
    return p


# ---- f2: L1 の書き換え ----

IFATT_FOLLOW = "follow"   # 段階 1・1.1: 指示書の値のまま
IFATT_IGNORE = "ignore"   # 段階 1.5・2: 0 に書き換える


def rewrite_l1(obs_rec: sl.Record, p: Plan, ifatt_mode: str) -> sl.Record:
    """差し替える Array の L1 の周波数の欄（と iary_ifatt）を書き換えた写しを返す。

    書き換える欄: dbebw・dbechwid・dberes・dfqdat_fq（dfqdat_ch の 2 点の ch の sky）。
    dcent_freq・dtrk_freq・dfqdat_f0 は観測の指定なので変えない（マージ・QL がどの欄を使うかは確認中）。
    """
    if ifatt_mode not in (IFATT_FOLLOW, IFATT_IGNORE):
        raise ValueError(f"IFATT のモード {ifatt_mode!r}")
    out = sl.copy_record(obs_rec)
    o = out.s
    for idx, w in p.windows.items():
        o.dbebw[idx] = w.bw_mhz * 1e6
        o.dbechwid[idx] = w.df_mhz * 1e6
        o.dberes[idx] = w.df_mhz * 1e6
        for j in range(2):
            ch = int(round(o.dfqdat_ch[idx][j]))
            o.dfqdat_fq[idx][j] = float(w.sky_hz[ch - 1])
        if ifatt_mode == IFATT_IGNORE:
            o.iary_ifatt[idx] = 0
    if ifatt_mode == IFATT_IGNORE:
        for idx in p.skipped:
            o.iary_ifatt[idx] = 0
    return out


def restrict_arrays(obs_rec: sl.Record, keep) -> sl.Record:
    """YKSP のロギング（段階 1）用: keep の Array だけを使う設定にした L1 の写し。"""
    out = sl.copy_record(obs_rec)
    keep = set(keep)
    for i in range(sl.ARYMAX):
        if i not in keep:
            out.s.iary_usefg[i] = 0
    out.s.iary_num = sum(1 for i in range(sl.ARYMAX) if out.s.iary_usefg[i])
    return out
