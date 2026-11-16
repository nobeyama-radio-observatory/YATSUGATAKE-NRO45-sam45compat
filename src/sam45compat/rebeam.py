# SPDX-License-Identifier: BSD-3-Clause
"""v0.3.0 より前に書いた YKSP のロギングの、埋めた Array のビームの番号（imlt_no）を決め直し、マージし直す（2026-11-16）。

v0.2.x までの mirror は写し元の Array の欄を丸ごと写したので、マルチビームの受信機（FOREST・7BEE）では
A05〜A08 のビームが A01〜A04 と同じ（1,1,2,2）になり、マージ後のビームの位置が誤った。
YKSP の L1 は写し元の欄を残している（iary_usefg が 0 になるだけ）ので、lc.mirror_beams をそのまま当てて直せる。

- 埋めた Array のビームが写し元と同じもの（直す前の印）だけを直す。v0.3.0 からのロギングは触らない
- 直すのは L1 の 1 レコードだけ（その場で書き換える）。元の L1 は退避の置き場所に残す
- --remerge: 前の観測生データ（psw/・otf/ の、ログ ID を名前に含むもの）を退避に移してから、deliver でマージし直す

  python3 -m sam45compat.rebeam --config config/bklog.local.json                 # 直すものを並べるだけ
  python3 -m sam45compat.rebeam --config config/bklog.local.json --apply         # L1 を直す
  python3 -m sam45compat.rebeam --config config/bklog.local.json --remerge       # L1 を直してマージし直す
"""

import argparse
import datetime as _dt
import glob
import json
import logging
import os
import shutil
from typing import Dict, List, Optional, Tuple

from . import deliver as dv
from . import lc as lcm
from . import samlog as sl

log = logging.getLogger("sam45compat")


def read_l1(path: str) -> sl.Record:
    """ロギングの頭（L0・L1）だけを読み、L1 を返す（offset 付き）。"""
    with open(path, "rb") as f:
        buf = f.read(sl.SIZES["L0"] + sl.SIZES["L1"])
    for rec in sl.iter_records(buf):
        if rec.kind == "L1":
            return rec
    raise sl.SamLogError(f"{path}: 頭に L1 が無い")


def stale_fills(obs: sl.Record, mirror: Dict[str, str]) -> Dict[int, int]:
    """埋めた Array → 写し元。使われていて、ビームが写し元と同じ（直す前の印）ものだけ。"""
    o = obs.s
    out = {}
    for dst_name, src_name in sorted(mirror.items()):
        dst, src = sl.array_index(dst_name), sl.array_index(src_name)
        if o.iary_usefg[dst] and o.imlt_no[dst] == o.imlt_no[src] \
                and sl.cstr(o.cfe_type[dst]) == sl.cstr(o.cfe_type[src]):
            out[dst] = src
    return out


def fix_l1(obs: sl.Record, mirror: Dict[str, str]) -> Tuple[Optional[sl.Record], List[str], List[Tuple[int, str]]]:
    """(直した L1（直すものが無ければ None）, 変えた Array の説明, 決められなかった Array と理由)。"""
    filled = stale_fills(obs, mirror)
    if not filled:
        return None, [], []
    new, problems = lcm.mirror_beams(obs, filled)
    changes = [f"{sl.array_label(i)} {obs.s.imlt_no[i]}→{new.s.imlt_no[i]}"
               for i in sorted(filled) if new.s.imlt_no[i] != obs.s.imlt_no[i]]
    if not changes:
        return None, [], problems
    return new, changes, problems


def patch_l1(path: str, old: sl.Record, new: sl.Record, backup_dir: str) -> None:
    """元の L1 を退避に書いてから、ロギングの L1 をその場で書き換える。"""
    os.makedirs(backup_dir, exist_ok=True)
    keep = os.path.join(backup_dir, os.path.basename(path) + ".L1")
    if not os.path.exists(keep):                  # 2 回目以降は最初の（直す前の）L1 を残す
        with open(keep, "wb") as f:
            f.write(old.to_bytes())
    with open(path, "r+b") as f:
        f.seek(old.offset)
        f.write(new.to_bytes())
        f.flush()
        os.fsync(f.fileno())


def old_outputs(proj_dir: str, log_id: str) -> Dict[str, List[str]]:
    """psw/・otf/ の、ログ ID を名前に含む観測生データ（PSW は yymmddHHMMSS、OTF は yyyymmddHHMMSS）。"""
    out = {}
    for kind in dv.KINDS:
        d = os.path.join(proj_dir, kind)
        try:
            names = sorted(os.listdir(d))
        except FileNotFoundError:
            continue
        hit = [os.path.join(d, n) for n in names if f".{log_id}." in n or f".{log_id[2:]}." in n]
        if hit:
            out[kind] = hit
    return out


def kind_of_obs(log_id: str, outputs: Dict[str, List[str]], bklog: str) -> Optional[str]:
    """PSW か OTF か。前の観測生データの置き場所、無ければ bklog の SAM45 のロギングの置き場所から。"""
    if len(outputs) == 1:
        return next(iter(outputs))
    found = [k for k in dv.KINDS if glob.glob(os.path.join(bklog, f"sam45{k}", f"SAM45.*.{log_id}"))]
    return found[0] if len(found) == 1 else None


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="v0.3.0 より前の YKSP のロギングのビームの番号を直し、マージし直す")
    ap.add_argument("logging", nargs="*", help="YKSP のロギング（省くと配信の置き場所の */*/logging/YKSP.* を全部見る）")
    ap.add_argument("--config", required=True, help="暫定 LC の設定（mirror・deliver_root・merge_cmd・merge_root を使う）")
    ap.add_argument("--bklog", default="/cosmos3/45m/bklog", help="SAM45 のロギングの置き場所（PSW か OTF かを決めるため）")
    ap.add_argument("--backup", default=None, help="元の L1 と前の観測生データの退避（既定 /home/yk/bklog/rebeam-<今日>）")
    ap.add_argument("--apply", action="store_true", help="L1 を直す")
    ap.add_argument("--remerge", action="store_true", help="L1 を直し、前の観測生データを退避してマージし直す")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(a.config) as f:
        c = json.load(f)
    mirror, root = c.get("mirror") or {}, c.get("deliver_root")
    if not mirror or not root:
        ap.error("設定に mirror と deliver_root が要る")
    backup = a.backup or f"/home/yk/bklog/rebeam-{_dt.date.today():%Y%m%d}"
    paths = a.logging or sorted(glob.glob(os.path.join(root, "*", "*", dv.LOGGING_DIR, "YKSP.*")))
    d = None
    if a.remerge:
        d = dv.Deliverer(root, c.get("merge_cmd"), background=False, merge_root=c.get("merge_root"),
                         merge_timeout_s=c.get("merge_timeout_s", 3600.0))
    n_fix = 0
    for path in paths:
        try:
            name = sl.LogName.parse(path)
            old = read_l1(path)
        except (ValueError, OSError) as e:
            print(f"?? {path}: 読めない（{e}）")
            continue
        new, changes, problems = fix_l1(old, mirror)
        for i, why in problems:
            print(f"?? {name.log_id}: {sl.array_label(i)} のビームを決められない（{why}）")
        if new is None:
            continue
        n_fix += 1
        recv = sl.cstr(old.s.cfe_type[sl.array_index(next(iter(mirror)))])
        proj = os.path.join(root, name.group, name.project)
        outputs = old_outputs(proj, name.log_id)
        kind = kind_of_obs(name.log_id, outputs, a.bklog)
        print(f"{kind or '?':3} {recv:8} {' '.join(changes)}  {path}")
        if not (a.apply or a.remerge):
            continue
        bdir = os.path.join(backup, name.group, name.project)
        patch_l1(path, old, new, bdir)
        if not a.remerge:
            continue
        if kind is None:
            print(f"?? {name.log_id}: PSW か OTF か決められない。マージしない（L1 は直した）")
            continue
        for files in outputs.values():
            for p in files:
                shutil.move(p, os.path.join(bdir, os.path.basename(p)))
        try:
            d.submit(dv.Job(path, kind))
        except Exception as e:              # 1 つの失敗で止めない（前の観測生データは退避にある）
            print(f"?? {name.log_id}: マージし直せない（{e!r}）。前の観測生データは {bdir}")
    print(f"直す（直した）ロギング: {n_fix} 個" + ("" if (a.apply or a.remerge) else "（並べただけ。--apply か --remerge で直す）"))


if __name__ == "__main__":
    main()
