# SPDX-License-Identifier: BSD-3-Clause
"""SAM45 のロギングと YKSP の L2 を 1 つのロギングにする（合体）。

- L0: SAM45 のもの
- L1: SAM45 のものに、肩代わりする Array の周波数の欄（f2）と iary_ifatt を書き換えたもの（全 Array を残す）
- L2: SAM45 の 1 積分の組の中で、肩代わりする Array の L2 を YKSP のもので差し替える。
  SAM45 にその Array の L2 が無ければ、Array の順の位置に足す
- ED

段階 1.1: 記録済みの SAM45 と YKSP のファイルから作る（combine_files・このモジュールの main）。
段階 2: 暫定 LC が観測中に作る（lc.py の output = "combined"）。
見張り（watchdog.py）が使う素通し（passthrough）もここに置く。
"""

from __future__ import annotations

import argparse
import os
from typing import Dict, List, Optional, Sequence, Tuple

from . import samlog as sl
from . import yksp as yk

# L1 のうち、YKSP の L1 から写す Array ごとの欄（mapping.rewrite_l1 が書き換えるもの）
L1_ARRAY_FIELDS = ("dbebw", "dbechwid", "dberes", "dfqdat_fq", "iary_ifatt")


def array_of(rec: sl.Record) -> int:
    return sl.array_index(sl.cstr(rec.s.cary_name))


def merge_integration(sam_recs: Sequence[sl.Record], ours: Dict[int, sl.Record]) -> List[sl.Record]:
    """1 積分ぶん: SAM45 の L2 の組に、ours（Array → YKSP の L2）を差し替え・足して、Array の順に並べる。"""
    by_array: Dict[int, sl.Record] = {}
    order: List[int] = []
    for r in sam_recs:
        a = array_of(r)
        if a not in by_array:
            order.append(a)
        by_array[a] = r
    for a, r in ours.items():
        if a not in by_array:
            order.append(a)
        by_array[a] = r
    # SAM45 の並びが Array の順なら、足した分もその順に入れる
    if [a for a in order if a not in ours] == sorted(a for a in order if a not in ours):
        order = sorted(order)
    return [by_array[a] for a in order]


def combined_l1(sam_l1: sl.Record, yksp_l1: sl.Record, targets) -> sl.Record:
    """SAM45 の L1 に、YKSP の L1 の肩代わりの Array の欄を写したもの。"""
    out = sl.copy_record(sam_l1)
    for i in targets:
        for name in L1_ARRAY_FIELDS:
            src = getattr(yksp_l1.s, name)
            dst = getattr(out.s, name)
            if name == "dfqdat_fq":
                dst[i][0], dst[i][1] = src[i][0], src[i][1]
            else:
                dst[i] = src[i]
    return out


def combine_records(sam: Sequence[sl.Record], yksp: Sequence[sl.Record]) -> Tuple[List[sl.Record], Dict[str, int]]:
    """段階 1.1: 記録済みの SAM45 と YKSP のレコードの並びから、合体したレコードの並び。"""
    if [r.kind for r in sam[:2]] != ["L0", "L1"] or [r.kind for r in yksp[:2]] != ["L0", "L1"]:
        raise sl.SamLogError("頭が L0・L1 でない")
    yl1 = yksp[1]
    targets = [i for i in range(sl.ARYMAX) if yl1.s.iary_usefg[i]]
    ours: Dict[Tuple, Dict[int, sl.Record]] = {}
    for r in yksp:
        if r.kind == "L2":
            ours.setdefault(yk.integration_key(r.s), {})[array_of(r)] = r
    stats = dict(integrations=0, replaced=0, added=0, missing=0)
    out = [sam[0], combined_l1(sam[1], yl1, targets)]
    g = yk.IntegrationGrouper()

    def emit(integs):
        for integ in integs:
            mine = ours.pop(integ.key, {})
            have = {array_of(r) for r in integ.records}
            for a in targets:
                if a not in mine:
                    stats["missing"] += 1        # YKSP に無い → SAM45 の値を無効にして残す
                    if a in have:
                        r = next(x for x in integ.records if array_of(x) == a)
                        r = sl.copy_record(r)
                        r.s.idmy_flag = 1
                        mine[a] = r
            stats["replaced"] += len(have & set(mine))
            stats["added"] += len(set(mine) - have)
            stats["integrations"] += 1
            out.extend(merge_integration(integ.records, mine))

    for r in sam[2:]:
        if r.kind == "L2":
            emit(g.feed(r))
        elif r.kind == "ED":
            emit(g.flush())
            out.append(r)
    stats["yksp_unused"] = sum(len(v) for v in ours.values())
    return out, stats


def combine_files(sam_path: str, yksp_path: str, out_path: str) -> Dict[str, int]:
    recs, stats = combine_records(sl.read_file(sam_path), sl.read_file(yksp_path))
    with open(out_path, "wb") as f:
        sl.write_records(f, recs)
    return stats


# ---- 素通し（見張り用） ----

def last_complete_integration_start(buf: bytes) -> Tuple[int, bool]:
    """書きかけの合体ファイルの中で、素通しを続けてよい位置と、ED まで書き終わっているか。

    最後の完全なレコードまでを見て、最後の積分は書きかけかもしれないので、その積分の頭の位置を返す
    （L1 までしか無ければ L1 の終わり、L1 も無ければ 0）。"""
    pos = 0
    last_key = None
    last_key_pos = 0
    head_end = 0
    for r in sl.iter_records(buf):
        end = r.offset + sl.SIZES[r.kind]
        if r.kind in ("L0", "L1"):
            head_end = end if r.kind == "L1" else head_end
        elif r.kind == "L2":
            k = yk.integration_key(r.s)
            if k != last_key:
                last_key, last_key_pos = k, r.offset
        elif r.kind == "ED":
            return end, True
        pos = end
    if last_key is not None:
        return last_key_pos, False
    return head_end, False


def passthrough_resume(src_path: str, dst_path: str) -> Tuple[int, Optional[tuple]]:
    """dst（合体ファイル、書きかけ・無しでもよい）を素通しで続けられる形に切り詰め、
    src（`_` の SAM45 のロギング）のどこから写せばよいかを返す: (src の位置, 最後に dst に残した積分の鍵)。"""
    if not os.path.exists(dst_path):
        return 0, None
    with open(dst_path, "rb") as f:
        buf = f.read()
    cut, done = last_complete_integration_start(buf)
    if done:
        return -1, None
    with open(dst_path, "r+b") as f:
        f.truncate(cut)
    kept = list(sl.iter_records(buf[:cut]))
    if not kept:
        return 0, None
    if kept[-1].kind == "L1":
        return sl.SIZES["L0"] + sl.SIZES["L1"], None
    last = yk.integration_key(kept[-1].s)
    # src の中で、last の後の最初の積分の頭（last そのものは dst に揃っている）
    with open(src_path, "rb") as f:
        sbuf = f.read()
    pos = None
    seen_last = False
    for r in sl.iter_records(sbuf):
        if r.kind == "L2":
            k = yk.integration_key(r.s)
            if k == last:
                seen_last = True
            elif seen_last:
                return r.offset, last
        elif r.kind == "ED" and seen_last:
            return r.offset, last
    # last の後がまだ書かれていない: src の終わり（の完全なレコードまで）から
    if seen_last:
        end = 0
        for r in sl.iter_records(sbuf):
            end = r.offset + sl.SIZES[r.kind]
        return end, last
    raise sl.SamLogError(f"{dst_path} の最後の積分 {last} が {src_path} に無い")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="段階 1.1: SAM45 と YKSP のロギングを合体する")
    ap.add_argument("sam45")
    ap.add_argument("yksp")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args(argv)
    if os.path.abspath(a.out) in (os.path.abspath(a.sam45), os.path.abspath(a.yksp)):
        raise SystemExit("出力が入力と同じ")
    stats = combine_files(a.sam45, a.yksp, a.out)
    print(f"{a.out}: {stats}")


if __name__ == "__main__":
    main()
