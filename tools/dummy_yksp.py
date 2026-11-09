# SPDX-License-Identifier: BSD-3-Clause
"""ダミーの YKSP のロギングを作る（望遠鏡・ボードなしの通しの試験）。

YATSUGATAKE の specd を PL なし（--fake）で起こし、暫定 LC（実機と同じ BoardSpectrometer）をつなぐ。
模擬の SAM45 のロギング（A01〜A08、Fine 31.25 MHz の窓 1 つ、PSW 風）を今の時刻で実時間に書き足し、
暫定 LC がそれを追いかけて YKSP のロギングを書く。

  python3 tools/dummy_yksp.py --out /home/yk/data [--tint 0.04096] [--duration 30]

書く先:
  <out>/<ログ ID>.YKSP.dummy.grp.prj         暫定 LC が書いた YKSP のロギング（A05〜A08）
  <out>/sam45_in/<ログ ID>.SAM45.dummy.grp.prj 入力にした模擬の SAM45 のロギング（A01〜A08。合体・比較の試験に使える）
  <out>/tmp/                                   10.24 ms などの一時ファイル（終わると消す）
  <out>/specd.log                              specd --fake のログ

specd --fake のスペクトルは模擬（決まった形）なので、値に意味はない。見られるのは、形式・時刻・束ねた数・
idmy_flag・準備から記録までの流れ。
"""

import argparse
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from sam45compat import lc as lcm  # noqa: E402
from sam45compat import mapping as mp  # noqa: E402
from sam45compat import samlog as sl  # noqa: E402
from sam45compat.board import BoardConfig, BoardSpectrometer  # noqa: E402
from sam45compat.notify import Notifier  # noqa: E402

PYNQ = os.path.join(HERE, "..", "external", "YATSUGATAKE", "sw", "pynq")
log = logging.getLogger("dummy_yksp")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_specd(logfile: str):
    cport, dport = free_port(), free_port()
    f = open(logfile, "w")
    proc = subprocess.Popen(
        [sys.executable, "specd.py", "--fake", "--cpu", "-1", "--rt", "0",
         "--ctrl-port", str(cport), "--data-port", str(dport)],
        cwd=PYNQ, stdout=f, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", cport), timeout=0.2).close()
            return proc, f, cport, dport
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"specd --fake が起きない（{logfile} を見る）")


def obs_record(arrays=range(8), bw_mhz=31.25, k0=258048) -> sl.Record:
    """模擬の L1（窓 1 つ。IF ≒ 3 GHz、sky は IF の昇順で下がる）。"""
    rec = sl.new_record("L1")
    o = rec.s
    sl.set_cstr(o, "csub_array", "OFF")
    o.in_spec_window[0] = 1
    avg = int(round(bw_mhz / mp.SAM45_DK_MHZ / 4096))
    o.istart_chan[0][0] = k0
    o.iend_chan[0][0] = k0 + 4096 * avg - 1
    o.ichan_avg[0][0] = avg
    if_1 = mp.SAM45_IF_LO_MHZ + (k0 + (avg - 1) / 2) * mp.SAM45_DK_MHZ
    if_n = mp.SAM45_IF_LO_MHZ + (k0 + 4095 * avg + (avg - 1) / 2) * mp.SAM45_DK_MHZ
    for i in arrays:
        o.iary_usefg[i] = 1
        o.ichannel[i] = 4096
        o.dbebw[i] = bw_mhz * 1e6
        o.iary_ifatt[i] = 5
        o.dfqdat_ch[i][0], o.dfqdat_ch[i][1] = 4096, 1
        o.dfqdat_fq[i][0] = (46122.0 - if_n) * 1e6
        o.dfqdat_fq[i][1] = (46122.0 - if_1) * 1e6
        o.dfqdat_f0[i] = (46122.0 - (if_1 + if_n) / 2) * 1e6
    o.iary_num = len(list(arrays))
    return rec


def scan_plan(duration_s: int, integ_s: int, gap_s: int):
    """PSW 風の並び: ZERO・R・SKY の後に OFF・ON を繰り返す。duration に収まるだけ。"""
    head = ["ZERO", "R", "SKY"]
    n = max(1, duration_s // (integ_s + gap_s))
    return [head[k] if k < len(head) else ("OFF", "ON")[(k - len(head)) % 2] for k in range(n)]


def l2_records(scan: str, n: int, st_unix: int, integ_s: int, arrays) -> list:
    out = []
    st = sl.datetime_to_mjdsec(datetime.fromtimestamp(st_unix, timezone.utc))
    for a in arrays:
        d = sl.new_record("L2")
        sl.set_cstr(d.s, "cary_name", sl.array_logname(a))
        d.s.iary_no = 41 + a
        sl.set_cstr(d.s, "cscan_type", scan)
        sl.set_cstr(d.s, "cscanid", f"dummy.{n:05d}")
        d.s.dint_stmjd, d.s.dint_edmjd = st, st + integ_s
        d.s.iinteg_time = integ_s
        d.s.dtsys = 300.0
        out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="書く先（YKSP のロギング）")
    ap.add_argument("--tint", type=float, default=0.04096, help="specd の積分時間 [s]（10.24 ms の倍数）")
    ap.add_argument("--duration", type=int, default=30, help="観測の長さ [s] の目安")
    ap.add_argument("--integ", type=int, default=4, help="SAM45 の 1 積分 [s]")
    ap.add_argument("--gap", type=int, default=1, help="積分の間の空き [s]")
    ap.add_argument("--lead", type=float, default=3.0, help="L1 を書いてから最初の積分までの秒（準備の時間）")
    ap.add_argument("--keep-tmp", action="store_true", help="一時ファイルを残す")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    out = os.path.abspath(a.out)
    sam_dir = os.path.join(out, "sam45_in")
    os.makedirs(sam_dir, exist_ok=True)
    arrays = list(range(8))                       # A01〜A08（段階 1 と同じ: A05〜A08 を肩代わり）
    log_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    sam_path = os.path.join(sam_dir, str(sl.LogName(log_id, "SAM45", "dummy", "grp", "prj")))

    proc, flog, cport, dport = start_specd(os.path.join(out, "specd.log"))
    spec = None
    try:
        cfg = lcm.LcConfig(log_dir=sam_dir, out_dir=out, tint_s=a.tint, skip_existing=True)
        bcfg = BoardConfig(host="127.0.0.1", ctl_port=cport, data_port=dport,
                           bits={"fine": "default", "wide": "default"},   # --fake の bit は default だけ
                           tmp_dir=os.path.join(out, "tmp"), keep_tmp=a.keep_tmp)
        spec = BoardSpectrometer(bcfg, a.tint)
        note = Notifier()
        lc = lcm.LC(cfg, spec, note)
        lc.step()                                 # 既にあるファイルを「見た」にする

        ctl = sl.new_record("L0")
        sl.set_cstr(ctl.s, "cbe_type", "SAM45")
        with open(sam_path, "wb") as f:
            for r in (ctl, obs_record(arrays)):
                f.write(r.to_bytes())
        log.info("模擬の SAM45: %s", sam_path)

        scans = scan_plan(a.duration, a.integ, a.gap)
        t0 = int(time.time() + a.lead) + 1        # 秒の境界（PSW と同じ）
        due = [(t0 + k * (a.integ + a.gap), sc) for k, sc in enumerate(scans)]
        log.info("積分 %d 個（%s）、%d s × tint %.2f ms、最初は %s",
                 len(scans), " ".join(scans), a.integ, a.tint * 1e3,
                 datetime.fromtimestamp(t0, timezone.utc).strftime("%H:%M:%S UTC"))
        k = 0
        ed_written = False
        deadline = due[-1][0] + a.integ + 30
        while time.time() < deadline:
            lc.step()
            now = time.time()
            # 積分が終わって 0.2 s 後に L2 を書き足す（SAM45 LC と同じ頃合い）
            while k < len(due) and now >= due[k][0] + a.integ + 0.2:
                with open(sam_path, "ab") as f:
                    for r in l2_records(due[k][1], k, due[k][0], a.integ, arrays):
                        f.write(r.to_bytes())
                log.info("SAM45 の L2: %s", due[k][1])
                k += 1
            if k == len(due) and not ed_written:
                with open(sam_path, "ab") as f:
                    f.write(sl.new_record("ED").to_bytes())
                ed_written = True
            if ed_written and not lc.sessions:
                break
            time.sleep(0.05)
        lc.close()
    finally:
        if spec is not None:
            spec.close()
        proc.terminate()
        proc.wait(10)
        flog.close()

    yk_path = os.path.join(out, str(sl.LogName(log_id, "YKSP", "dummy", "grp", "prj")))
    if not os.path.exists(yk_path):
        print(f"YKSP のロギングができなかった: {yk_path}", file=sys.stderr)
        return 1
    recs = sl.read_file(yk_path)
    l2 = [r for r in recs if r.kind == "L2"]
    bad = sum(r.s.idmy_flag != 0 for r in l2)
    print(yk_path)
    print(f"  レコード: {' '.join(sorted(set(r.kind for r in recs)))}、L2 {len(l2)} 個（idmy_flag ≠ 0: {bad}）"
          f"、{os.path.getsize(yk_path)} B")
    for m in note.sent:
        print("  通知:", m)
    return 0 if recs[-1].kind == "ED" and bad == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
