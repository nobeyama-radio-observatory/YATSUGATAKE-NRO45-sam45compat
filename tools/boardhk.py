#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""RFSoC 4x2 の上で動かす、ボードの状態（温度・電源・CPU・メモリ）を返す常駐（2026-11-11）。

状態のページ（docs/design/status-page.md）の「ボード」の欄のため。specd には触れない（コアにこの命令が無いので、
別のプロセスにした。読むのは sysfs と /proc だけ）。WORKAROUND(core): specd に HK の命令ができたら、そちらに移る。

- 1 秒ごとに読み、最後の値を HTTP で返す: curl http://$B:51010/  → JSON 1 つ
- 読むもの:
  - AMS（xilinx-ams の iio）: 温度（℃）と電圧（mV）。名前は sysfs のもの（temp0_ps_temp など）
  - INA220（hwmon）: 電流（mA）。0x40 が VCCINT（YATSUGATAKE の sw/pynq/amslog.py と同じ見方）
  - /proc/stat: CPU ごとの使用率（%）。/proc/loadavg・/proc/meminfo・/proc/uptime
- 操作の口は無い（GET で値を返すだけ）
- specd の取得は CPU 3 に固定されている（specd の --cpu）。こちらは systemd で CPU 0・優先度最低にする
  （tools/systemd/sam45compat-boardhk.service）

  python3 boardhk.py [--bind 0.0.0.0] [--port 51010] [--period 1.0]
"""

import argparse
import glob
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

INA_NAMES = {"40": "VCCINT"}
# AMS の sysfs に名前のファイル（in_<名前>_label）が無いときの温度の名前（Xilinx の AMS の番号の慣例。2026-11-11 の
# ボードでは temp7・temp8・temp20 の 3 つだった）。電圧は番号から決めない（名前のファイルが無ければ番号のまま）
AMS_TEMP_LABELS = {"temp7": "PS_LPD", "temp8": "PS_FPD", "temp20": "PL"}


def _read(path):
    with open(path) as f:
        return f.read().strip()


def ams_channels():
    """[(名前, raw の道, scale, offset, 表示の名前)]。AMS が無ければ []。"""
    for d in glob.glob("/sys/bus/iio/devices/iio:device*"):
        try:
            if _read(d + "/name") != "xilinx-ams":
                continue
        except OSError:
            continue
        out = []
        for raw in sorted(glob.glob(os.path.join(d, "in_*_raw"))):
            base = raw[: -len("_raw")]
            name = os.path.basename(base)[3:]

            def num(p, default):
                try:
                    return float(_read(p))
                except (OSError, ValueError):
                    return default
            try:
                label = _read(base + "_label")
            except OSError:
                label = AMS_TEMP_LABELS.get(name, "")
            out.append((name, raw, num(base + "_scale", 1.0), num(base + "_offset", 0.0), label))
        return out
    return []


def ina_channels():
    """[(名前, curr1_input の道)]。名前は VCCINT か i2c の番地（i41 など）。"""
    out = []
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            if _read(h + "/name") != "ina220":
                continue
            addr = os.path.basename(os.path.realpath(h + "/device"))[-2:]
        except OSError:
            continue
        out.append((INA_NAMES.get(addr, "i" + addr), h + "/curr1_input"))
    return sorted(out)


def cpu_times():
    """{cpuN: (busy, total)}（jiffies の累計）。"""
    out = {}
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu") or line.startswith("cpu "):
                continue
            p = line.split()
            v = [int(x) for x in p[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)        # idle + iowait
            total = sum(v[:8])                                # guest は user に含まれる
            out[p[0]] = (total - idle, total)
    return out


def meminfo():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            d[k] = int(v.split()[0])                          # kB
    return d


class Sampler(threading.Thread):
    def __init__(self, period):
        super().__init__(daemon=True, name="sampler")
        self.period = period
        self.ams = ams_channels()
        self.ina = ina_channels()
        self.latest = {}
        self._prev = cpu_times()
        self.lock = threading.Lock()

    def sample(self):
        temp, volt, curr = {}, {}, {}
        for name, raw, sc, off, _ in self.ams:
            try:
                v = (float(_read(raw)) + off) * sc
            except (OSError, ValueError):
                continue
            if name.startswith("temp"):
                temp[name] = round(v / 1000.0, 2)             # m℃ → ℃
            else:
                volt[name] = round(v, 1)                      # mV
        for name, p in self.ina:
            try:
                curr[name] = int(_read(p))                    # mA
            except (OSError, ValueError):
                continue
        now = cpu_times()
        cpu = {}
        for k, (busy, total) in sorted(now.items(), key=lambda x: int(x[0][3:])):
            b0, t0 = self._prev.get(k, (busy, total))
            dt = total - t0
            cpu[k] = round(100.0 * (busy - b0) / dt, 1) if dt > 0 else None
        self._prev = now
        m = meminfo()
        mem_total = m.get("MemTotal", 0)
        mem_avail = m.get("MemAvailable", m.get("MemFree", 0))
        shm = os.statvfs("/dev/shm")
        root = os.statvfs("/")
        return {
            "t": time.time(),
            "labels": {name: label for name, _, _, _, label in self.ams if label},
            "temp_c": temp,
            "volt_mv": volt,
            "curr_ma": curr,
            "cpu_pct": cpu,
            "load": [float(x) for x in _read("/proc/loadavg").split()[:3]],
            "mem_total_mb": round(mem_total / 1024.0, 1),
            "mem_used_pct": round(100.0 * (mem_total - mem_avail) / mem_total, 1) if mem_total else None,
            "shm_used_pct": round(100.0 * (1 - shm.f_bavail / shm.f_blocks), 1) if shm.f_blocks else None,
            "root_used_pct": round(100.0 * (1 - root.f_bavail / root.f_blocks), 1) if root.f_blocks else None,
            "uptime_s": float(_read("/proc/uptime").split()[0]),
        }

    def run(self):
        while True:
            t0 = time.monotonic()
            try:
                s = self.sample()
            except Exception as e:                            # 読めない値があっても止まらない
                s = {"t": time.time(), "error": repr(e)}
            with self.lock:
                self.latest = s
            time.sleep(max(0.0, self.period - (time.monotonic() - t0)))


def make_handler(sampler):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] not in ("/", "/hk.json"):
                self.send_error(404)
                return
            with sampler.lock:
                body = json.dumps(sampler.latest, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):                   # 毎秒の問い合わせでログを埋めない
            pass
    return Handler


def main():
    ap = argparse.ArgumentParser(description="RFSoC のボードの状態（温度・電源・CPU・メモリ）を HTTP で返す")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=51010)
    ap.add_argument("--period", type=float, default=1.0, help="読む間隔 [s]")
    a = ap.parse_args()
    s = Sampler(a.period)
    print("AMS %d ch、INA220 %s" % (len(s.ams), [n for n, _ in s.ina]), flush=True)
    s.start()
    HTTPServer((a.bind, a.port), make_handler(s)).serve_forever()


if __name__ == "__main__":
    main()
