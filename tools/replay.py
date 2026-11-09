#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""記録済みの SAM45 のロギングを、SAM45 LC が書いている途中のように再生する（望遠鏡なしの試験）。

L0・L1 をまず書き、L2 は積分ごとに、積分の時刻の差を --speed で割った間隔で書き足し、最後に ED を書く。

  python3 tools/replay.py <SAM45 のロギング> <書く先のディレクトリ> [--speed 60] [--prefix _] [--log-id 新しいID]

--log-id で別のログ ID の名前にできる（同じサンプルを何度も流すため）。

--now 秒: 実機の試験用。L2 の積分の時刻（dint_stmjd・dint_edmjd）を、最初の積分が「今から 秒 後の秒の境界」に
始まるようにずらし、各積分の L2 をその積分の終わり（壁の時計）に書く（SAM45 LC と同じ頃合い）。--speed は使わない。
ログ ID を指定しなければ今の時刻にする。
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sam45compat import samlog as sl          # noqa: E402
from sam45compat import yksp as yk            # noqa: E402


def chunks(records):
    """[L0, L1]、積分ごとの L2 の組…、[ED] の順に返す。(レコードの並び, 積分の開始の秒) の組。"""
    head, cur, key = [], [], None
    for r in records:
        if r.kind in ("L0", "L1"):
            head.append(r)
            if r.kind == "L1":
                yield head, None
        elif r.kind == "L2":
            k = yk.integration_key(r.s)
            if key is not None and k != key and cur:
                yield cur, key[1]
                cur = []
            key = k
            cur.append(r)
        elif r.kind == "ED":
            if cur:
                yield cur, key[1]
            yield [r], None


def shift_times(recs, shift_s: float) -> None:
    """L2 の積分の時刻を shift_s 秒ずらす（その場で）"""
    for r in recs:
        if r.kind == "L2":
            r.s.dint_stmjd += shift_s
            r.s.dint_edmjd += shift_s


def replay(src: str, dst_dir: str, speed: float = 60.0, prefix: str = "", log_id: str = None,
           on_chunk=None, now_lead_s: float = None, write_delay_s: float = 0.2) -> str:
    recs = sl.read_file(src)
    if now_lead_s is not None:
        first = next(r.s.dint_stmjd for r in recs if r.kind == "L2")
        t0 = int(time.time() + now_lead_s) + 1                     # 秒の境界（PSW と同じ）
        shift_times(recs, sl.datetime_to_mjdsec(datetime.fromtimestamp(t0, timezone.utc)) - first)
        if not log_id:
            log_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    name = sl.LogName.parse(src).with_prefix(prefix)
    if log_id:
        name = sl.LogName(log_id, name.backend, name.obs_file, name.group, name.project, prefix)
    out = os.path.join(dst_dir, str(name))
    t_prev = None
    with open(out, "wb") as f:
        for part, t in chunks(recs):
            if now_lead_s is not None:
                if part[0].kind == "L2":
                    ed = sl.mjdsec_to_unix_ns(part[0].s.dint_edmjd) / 1e9 + write_delay_s
                    time.sleep(max(0.0, ed - time.time()))
            elif t is not None and t_prev is not None and speed > 0:
                time.sleep(max(0.0, (t - t_prev) / speed))
            if t is not None:
                t_prev = t
            for r in part:
                f.write(r.to_bytes())
            f.flush()
            if on_chunk:
                on_chunk(out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst_dir")
    ap.add_argument("--speed", type=float, default=60.0, help="時間の早回しの倍率（0 で待たない）")
    ap.add_argument("--prefix", default="", help="ファイル名の頭（段階 2 の SAM45 LC は '_'）")
    ap.add_argument("--log-id", default=None)
    ap.add_argument("--now", type=float, default=None, metavar="秒",
                    help="実時間で再生（最初の積分を今から この秒 後に始める）")
    a = ap.parse_args()
    print(replay(a.src, a.dst_dir, a.speed, a.prefix, a.log_id, now_lead_s=a.now))


if __name__ == "__main__":
    main()
