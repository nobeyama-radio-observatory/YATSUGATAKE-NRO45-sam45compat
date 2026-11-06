# SPDX-License-Identifier: BSD-3-Clause
"""SAM45 のロギングの読み書き。

形式は docs/cosmos/sam45-logging.md。COSMOS の sam_log.h（V004.1）の構造体を写したもの。
ファイルは little-endian のレコードの並び（L0・L1・L2 × n・ED）で、各レコードの頭は
種類（char[4]）と長さ（int、頭の 8 B を除く）。
"""

from __future__ import annotations

import ctypes as C
import datetime as _dt
import os
import re
from dataclasses import dataclass
from typing import BinaryIO, Iterator, List, Optional, Tuple

# sam_common.h
ARYMAX = 32          # Array の数（A01〜A32）
BINMAX = 4
SPWMAX = 2
SUBARY = 2
CHMAX_LOGGING = 4096  # ロギングの ch 数

_D = C.c_double
_I = C.c_int32
_F = C.c_float
_U = C.c_uint8
_B = C.c_int8
_S = C.c_char


class _Struct(C.LittleEndianStructure):
    """8 B 境界の自然な並べ方（pack の指定なし）。"""


class TypeHead(_Struct):
    _fields_ = [("crec_type", _S * 4), ("irec_len", _I)]


class LogCtl(_Struct):
    """L0: ロギングの管理情報。"""
    _fields_ = [
        ("head", TypeHead),
        ("cversion", _S * 8),
        ("clog_name", _S * 64),
        ("cbe_type", _S * 8),
    ]


class LogObs(_Struct):
    """L1: ロギングの観測情報（分光の設定を含む）。Array ごとの欄は添字 0 = A01。"""
    _fields_ = [
        ("head", TypeHead),
        # 8 B の欄
        ("dant_off", _D * 2),
        ("dsub_off", _D * 3),
        ("dsrc_pos", (_D * 3) * 2),
        ("dstrv", _D),
        ("dmap_pos", _D),
        ("dmult_off", _D),
        ("dbsw_freq", _D),
        ("dsw_freq", _D * ARYMAX),
        ("dflif", _D * ARYMAX),
        ("dcent_freq", _D * ARYMAX),
        ("dtrk_freq", _D * ARYMAX),
        ("dbebw", _D * ARYMAX),
        ("dfqdat_f0", _D * ARYMAX),
        ("dfqdat_fq", (_D * 2) * ARYMAX),
        ("dfqdat_ch", (_D * 2) * ARYMAX),
        ("dbechwid", _D * ARYMAX),
        ("dberes", _D * ARYMAX),
        ("deffa", _D * ARYMAX),
        ("deffb", _D * ARYMAX),
        ("deffl", _D * ARYMAX),
        ("defss", _D * ARYMAX),
        ("dgain", _D * ARYMAX),
        ("dhpbw", _D * ARYMAX),
        ("dpoldr", _D * ARYMAX),
        ("dbin_dwell", _D * BINMAX),
        ("diptim", _D),
        ("reserve8", _D * 15),
        # 4 B の欄
        ("ich_bind", _I * ARYMAX),
        ("ich_range", (_I * 2) * ARYMAX),
        ("icalb_int", _I),
        ("ichannel", _I * ARYMAX),
        ("iipint", _I * ARYMAX),
        ("ipordr", _I * ARYMAX),
        ("ichan_avg", (_I * SPWMAX) * SUBARY),
        ("istart_chan", (_I * SPWMAX) * SUBARY),
        ("iend_chan", (_I * SPWMAX) * SUBARY),
        ("drms_value", _F),
        ("on_axis", (_F * 2) * 10),
        ("irms_judge", _I),
        ("irms_mode", _I),
        ("irms_onnum", _I),
        ("autofeedback_weight", _F),
        ("reserve4", _I * 64),
        # 1 B の欄
        ("imd_obs", _U),
        ("icoord_type", _U),
        ("iscan_coord", _U),
        ("isw_mod", _U),
        ("ivdef", _U),
        ("ivref", _U),
        ("imlt_no", _U * ARYMAX),
        ("iary_ifatt", _B * ARYMAX),
        ("iary_refno", _U * ARYMAX),
        ("iary_usefg", _U * ARYMAX),
        ("ibin_num", _U),
        ("in_spec_window", _U * SUBARY),
        ("clog_id", _S * 20),
        ("cst_time", _S * 16),
        ("cobs_file", _S * 20),
        ("cobj_name", _S * 16),
        ("csrc_comment", _S * 24),
        ("cobs_user", _S * 40),
        ("cgroup", _S * 10),
        ("cproject", _S * 10),
        ("ctrk_type", _S * 8),
        ("cepoch", _S * 8),
        ("cscan_coord", _S * 8),
        ("cseq_ptn", _S * 120),
        ("csid_type", (_S * 4) * ARYMAX),
        ("cfe_type", (_S * 10) * ARYMAX),
        ("chorn_typ", (_S * 2) * ARYMAX),
        ("cpol_typ", (_S * 5) * ARYMAX),
        ("csub_array", _S * 4),
        ("cinput_mode", (_S * 6) * SUBARY),
        ("cbut_scale", (_S * 64) * SUBARY),
        ("crequant_rescale", (_S * 64) * SUBARY),
        ("crequant_scale", (_S * 64) * SUBARY),
        ("cmake_hfs_table", (_S * 4) * SUBARY),
        ("cmisc_flags_hfs", (_S * 4) * SUBARY),
        ("cmisc_flags_lfs", (_S * 4) * SUBARY),
        ("cmisc_flags_mult", (_S * 7) * SUBARY),
        ("cfreq_prof_synth", ((_S * 4) * SPWMAX) * SUBARY),
        ("cwin_func_id", ((_S * 16) * SPWMAX) * SUBARY),
        ("crms_array", _S * 4),
        ("i2beam_mode", _U),
        ("imlt_mode", _U),
        ("iary_num", _U),
        ("iotf_mode", _U),
        ("iotf_dumpall", _U),
        ("reserve1", _S * 508),
    ]


class LogDat(_Struct):
    """L2: 積分ごと・Array ごとの付加情報とスペクトル。"""
    _fields_ = [
        ("head", TypeHead),
        # 8 B の欄
        ("dant_prog", _D * 2),
        ("dant_real", _D * 2),
        ("dant_vel", _D),
        ("dmap_center", (_D * 3) * 2),
        ("dobs_pos", (_D * 3) * 2),
        ("dch_center", _D * 2),
        ("dscan_pos_xy", _D * 2),
        ("dscan_pos_xy_origin", _D * 2),
        ("dsun_distance", _D),
        ("dsun_ps_angle", _D),
        ("dint_edmjd", _D),   # 名前は MJD だが、中身は MJD の起点からの秒
        ("dint_stmjd", _D),
        ("dtsys", _D),
        ("dweather", _D * 5),
        ("dcenter_frequency", _D),
        ("reserve8", _D * 15),
        # 4 B の欄
        ("iinteg_time", _I),
        ("iline_no", _I),
        ("iscan_line", _I),
        ("iscan_no", _I),
        ("istart_channel", _I),
        ("inumber_channels", _I),
        ("fwindow_overlap", _F),
        ("reserve4", _I * 11),
        # 1 B の欄
        ("iary_no", _U),       # 40 + Array の番号（A05 → 45）
        ("idmy_flag", _U),     # 0 有効・1 無効
        ("cary_name", _S * 4),  # "A5"（ゼロ詰めしない）
        ("cint_edtm", _S * 16),
        ("cint_sttm", _S * 32),
        ("cscan_type", _S * 6),
        ("cscanid", _S * 20),
        ("clog_time", _S * 16),
        ("icdpc_endcode", _B),
        ("iquantization_correction", _U),
        ("cpoln_products", _S * 6),
        ("cwindow_function", _S * 17),
        ("reserve1", _S * 87),
        ("fary_data", _F * CHMAX_LOGGING),
    ]


class LogEnd(_Struct):
    """ED: ロギングの終わり。"""
    _fields_ = [("head", TypeHead)]


HEAD_SIZE = C.sizeof(TypeHead)
STRUCTS = {"L0": LogCtl, "L1": LogObs, "L2": LogDat, "ED": LogEnd}
SIZES = {k: C.sizeof(v) for k, v in STRUCTS.items()}
# 実ファイル（V004.1）で確かめた大きさ。構造体の写し間違いを import のときに捕まえる
_EXPECTED_SIZES = {"L0": 88, "L1": 8472, "L2": 17064, "ED": 8}
assert SIZES == _EXPECTED_SIZES, SIZES


class SamLogError(ValueError):
    """ロギングの形式が想定と違う。"""


@dataclass
class Record:
    kind: str          # "L0" / "L1" / "L2" / "ED"
    s: C.Structure     # 中身（ctypes の構造体）
    offset: int = -1   # ファイルの中の位置（読んだときだけ）

    def to_bytes(self) -> bytes:
        return bytes(self.s)


def new_record(kind: str) -> Record:
    """頭（種類・長さ）だけを埋めた空のレコード。"""
    cls = STRUCTS[kind]
    s = cls()
    s.head.crec_type = kind.encode()
    s.head.irec_len = C.sizeof(cls) - HEAD_SIZE
    return Record(kind, s)


def copy_record(rec: Record) -> Record:
    cls = STRUCTS[rec.kind]
    return Record(rec.kind, cls.from_buffer_copy(bytes(rec.s)))


def parse_one(buf: bytes, offset: int = 0) -> Optional[Record]:
    """buf[offset:] の頭のレコードを 1 つ読む。まだ書き終わっていなければ None。"""
    if len(buf) - offset < HEAD_SIZE:
        return None
    head = TypeHead.from_buffer_copy(buf, offset)
    kind = head.crec_type.decode("ascii", errors="replace").rstrip("\0")
    if kind not in STRUCTS:
        raise SamLogError(f"知らないレコードの種類 {head.crec_type!r}（位置 {offset}）")
    size = SIZES[kind]
    if head.irec_len != size - HEAD_SIZE:
        raise SamLogError(
            f"{kind} の長さが {head.irec_len}（想定 {size - HEAD_SIZE}、位置 {offset}）")
    if len(buf) - offset < size:
        return None
    return Record(kind, STRUCTS[kind].from_buffer_copy(buf, offset), offset)


def iter_records(buf: bytes, offset: int = 0) -> Iterator[Record]:
    """buf の中の書き終わったレコードを順に返す。ED の後は読まない。"""
    while True:
        rec = parse_one(buf, offset)
        if rec is None:
            return
        yield rec
        offset += SIZES[rec.kind]
        if rec.kind == "ED":
            return


def read_file(path: str) -> List[Record]:
    with open(path, "rb") as f:
        return list(iter_records(f.read()))


def write_records(f: BinaryIO, records) -> None:
    for rec in records:
        f.write(rec.to_bytes())


class Follower:
    """書き足されていくロギングを追いかけて読む。

    poll() を呼ぶたびに、前回から後に書き終わったレコードを返す。
    ED を読んだら done が True になる。ネットワークマウントの上でも動くよう、
    毎回ファイルを開き直して前回の位置から読む。
    """

    def __init__(self, path: str):
        self.path = path
        self.pos = 0          # 読み終えたレコードの終わり
        self.done = False
        self.ctl: Optional[Record] = None
        self.obs: Optional[Record] = None
        self.n_dat = 0

    def poll(self) -> List[Record]:
        if self.done:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            buf = f.read()
        out = []
        off = 0
        for rec in iter_records(buf):
            rec.offset += self.pos
            off = rec.offset - self.pos + SIZES[rec.kind]
            self._check_order(rec)
            out.append(rec)
            if rec.kind == "ED":
                self.done = True
        self.pos += off
        return out

    def _check_order(self, rec: Record) -> None:
        if rec.kind == "L0":
            if self.ctl is not None:
                raise SamLogError("L0 が 2 つある")
            self.ctl = rec
        elif rec.kind == "L1":
            if self.ctl is None or self.obs is not None:
                raise SamLogError("L1 の位置が L0 の直後でない")
            self.obs = rec
        elif rec.kind == "L2":
            if self.obs is None:
                raise SamLogError("L1 より前に L2 がある")
            self.n_dat += 1
        elif rec.kind == "ED":
            if self.obs is None:
                raise SamLogError("L1 より前に ED がある")


# ---- 欄の扱い ----

def cstr(value) -> str:
    """char[] の欄を文字列に（NUL から後は捨てる）。"""
    raw = bytes(value).split(b"\0", 1)[0]
    return raw.decode("ascii", errors="replace")


def set_cstr(struct: C.Structure, name: str, text: str) -> None:
    """char[] の欄に文字列を入れる（長さを越えたらエラー。残りは NUL）。"""
    size = getattr(type(struct), name).size
    raw = text.encode("ascii")
    if len(raw) > size:
        raise ValueError(f"{name} に入らない（{len(raw)} > {size}）: {text!r}")
    C.memmove(C.addressof(struct) + getattr(type(struct), name).offset,
              raw.ljust(size, b"\0"), size)


def array_index(name: str) -> int:
    """'A05'・'A5' → 4（添字）。"""
    m = re.fullmatch(r"A0*(\d+)", name.strip())
    if not m or not 1 <= int(m.group(1)) <= ARYMAX:
        raise ValueError(f"Array の名前でない: {name!r}")
    return int(m.group(1)) - 1


def array_label(index: int) -> str:
    """4 → 'A05'（文書・設定での書き方）。"""
    return f"A{index + 1:02d}"


def array_logname(index: int) -> str:
    """4 → 'A5'（L2 の cary_name の書き方）。"""
    return f"A{index + 1}"


_MJD_EPOCH = _dt.datetime(1858, 11, 17, tzinfo=_dt.timezone.utc)


def mjdsec_to_datetime(sec: float) -> _dt.datetime:
    """dint_stmjd・dint_edmjd（MJD の起点からの秒）→ UTC の datetime。"""
    return _MJD_EPOCH + _dt.timedelta(seconds=sec)


def datetime_to_mjdsec(t: _dt.datetime) -> float:
    if t.tzinfo is None:
        raise ValueError("タイムゾーンの無い datetime")
    return (t - _MJD_EPOCH).total_seconds()


def mjdsec_to_unix_ns(sec: float) -> int:
    """MJD の起点からの秒 → UNIX 時刻の ns（YATSUGATAKE の utc_ns と比べるため）。"""
    unix_epoch_mjdsec = 40587 * 86400  # MJD 40587 = 1970-01-01
    return round((sec - unix_epoch_mjdsec) * 1e9)


# ---- ファイル名 ----

@dataclass(frozen=True)
class LogName:
    """'<ログ ID>.<装置>.<指示書名>.<group>.<project>'（装置は SAM45 / YKSP、段階 2 は頭に '_'）。"""
    log_id: str
    backend: str
    obs_file: str
    group: str
    project: str
    prefix: str = ""

    _RE = re.compile(r"(_?)(\d{14})\.([A-Za-z0-9]+)\.([^.]+)\.([^.]+)\.([^.]+)")

    @classmethod
    def parse(cls, name: str) -> "LogName":
        m = cls._RE.fullmatch(os.path.basename(name))
        if not m:
            raise ValueError(f"ロギングのファイル名でない: {name!r}")
        p, log_id, be, obs, grp, prj = m.groups()
        return cls(log_id, be, obs, grp, prj, p)

    def __str__(self) -> str:
        return f"{self.prefix}{self.log_id}.{self.backend}.{self.obs_file}.{self.group}.{self.project}"

    def with_backend(self, backend: str) -> "LogName":
        return LogName(self.log_id, backend, self.obs_file, self.group, self.project, self.prefix)

    def with_prefix(self, prefix: str) -> "LogName":
        return LogName(self.log_id, self.backend, self.obs_file, self.group, self.project, prefix)


def summary(records: List[Record]) -> Tuple[int, int, bool]:
    """(L2 の数, 使う Array の数, ED があるか)。"""
    n_dat = sum(r.kind == "L2" for r in records)
    obs = next((r for r in records if r.kind == "L1"), None)
    n_ary = sum(obs.s.iary_usefg) if obs else 0
    return n_dat, n_ary, any(r.kind == "ED" for r in records)
