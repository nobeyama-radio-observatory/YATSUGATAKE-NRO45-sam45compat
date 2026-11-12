# SPDX-License-Identifier: BSD-3-Clause
"""状態のページ: 暫定 LC と RFSoC（specd）の状態を JSON に書き、Apache が配る（2026-11-11）。

- 書く: LC の中の 1 本のスレッドが status_s ごとに status_path を書き直す（一時ファイルに書いて rename）。
  ページ（tools/status/index.html）が 1 秒ごとに取りに来る。書いた時刻（"t"）が古ければ、ページは「LC が止まった」と出す
- ボード: 別の 1 本のスレッドが status_board_s ごとに specd の STATUS を取る。制御の口は同時に 1 接続だけなので、
  board.BoardSpectrometer.status_nowait（LC の命令と同じロック）を使い、LOAD などで使っているときは問い合わせを飛ばす。
  つながらないときの待ち（数秒）で JSON の更新を止めないよう、LC の状態とは別のスレッドにする
- ボードの温度・電源・CPU・メモリ: 設定の board.hk_port があれば、同じスレッドでボードの常駐（tools/boardhk.py）に
  HTTP で問い合わせる。specd とは別のプロセスなので、ロックは要らない
- 中身は表示用。運用の判断（無効にする・知らせる）は今までどおり lc.py が行う。ここが失敗しても LC は止めない

設計は docs/design/status-page.md。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import threading
import time
import urllib.request
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from .lc import LC

log = logging.getLogger("sam45compat")

VERSION = 1


class BoardPoller:
    """specd の STATUS を定期的に取り、最後の結果を持つ。"""

    def __init__(self, spec, interval_s: float = 1.0, timeout_s: float = 2.0, hk_url: Optional[str] = None):
        self.spec = spec
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.supported = hasattr(spec, "status_nowait")
        self.status: Optional[Dict[str, str]] = None   # 最後に取れた STATUS
        self.t_ok: Optional[float] = None              # それを取った時刻（unix 秒）
        self.t_try: Optional[float] = None
        self.error: Optional[str] = None               # 最後の問い合わせの失敗（取れたら None）
        self.busy = False                              # LC が制御の口を使っていて飛ばした
        cfg = getattr(spec, "cfg", None)
        if hk_url is None and getattr(cfg, "hk_port", None):
            hk_url = f"http://{cfg.host}:{cfg.hk_port}/"
        self.hk_url = hk_url
        self.hk: Optional[Dict[str, object]] = None   # 最後に取れたボードの状態（boardhk.py）
        self.hk_t_ok: Optional[float] = None
        self.hk_error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.supported or self.hk_url:
            self._thread = threading.Thread(target=self._run, daemon=True, name="status-board")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poll(self) -> None:
        if self.supported:
            self.poll_status()
        if self.hk_url:
            self.poll_hk()

    def poll_hk(self) -> None:
        try:
            with urllib.request.urlopen(self.hk_url, timeout=self.timeout_s) as r:
                self.hk = json.loads(r.read().decode())
            self.hk_t_ok = time.time()
            self.hk_error = None
        except Exception as e:
            self.hk_error = str(e) or type(e).__name__

    def poll_status(self) -> None:
        self.t_try = time.time()
        try:
            st = self.spec.status_nowait(self.timeout_s)
        except Exception as e:
            self.error = str(e) or type(e).__name__
            self.busy = False
            return
        if st is None:
            self.busy = True
            return
        self.busy = False
        self.error = None
        self.status = st
        self.t_ok = time.time()

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            self.poll()
            self._stop.wait(max(0.0, self.interval_s - (time.monotonic() - t0)))

    def snapshot(self) -> Dict[str, object]:
        hk = None
        if self.hk_url:
            hk = {"t_ok": self.hk_t_ok, "error": self.hk_error, "data": self.hk}
        if not self.supported:
            return {"mode": "fake", "hk": hk} if hk else {"mode": "fake"}
        d: Dict[str, object] = {"mode": "board", "hk": hk, "t_ok": self.t_ok, "t_try": self.t_try,
                                "error": self.error, "busy": self.busy, "status": self.status}
        info = getattr(self.spec, "recorder_info", None)
        if info is not None:
            try:
                d["recorder"] = info()
            except Exception as e:
                d["recorder"] = {"error": repr(e)}
        return d


def _disk(path: Optional[str]) -> Optional[Dict[str, object]]:
    if not path:
        return None
    try:
        u = shutil.disk_usage(path)
    except OSError as e:
        return {"path": path, "error": str(e)}
    return {"path": path, "total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1),
            "used_pct": round(100.0 * u.used / u.total, 1) if u.total else None}


def lc_snapshot(lc: "LC") -> Dict[str, object]:
    """LC の状態（表示用）。本体のスレッドが書き換えている途中のものを読むので、値はおおよそ。"""
    now_m = time.monotonic()
    cfg = lc.cfg
    sessions: List[Dict[str, object]] = []
    for path, s in list(lc.sessions.items()):
        sessions.append(s.info())
    dirs = []
    for in_dir, out_dir, kind in lc._dirs():
        dirs.append({"in": in_dir, "out": out_dir, "kind": kind,
                     "readable": in_dir in lc._primed and in_dir not in lc._unreadable})
    dl = None
    if lc.deliverer is not None:
        d = lc.deliverer
        dl = {"queued": d.queued(), "current": d.current,
              "done": [os.path.basename(p) for p in d.done[-5:]],
              "merge": bool(d.merge_cmds)}
    disks = []
    seen = set()
    tmp = cfg.board.get("tmp_dir") if isinstance(cfg.board, dict) else None
    for p in [o for _, o, _ in lc._dirs()] + [tmp, cfg.deliver_root]:
        if p and p not in seen:
            seen.add(p)
            disks.append(_disk(p))
    # 窓の設定: 今の観測のもの。無ければ直前の観測のもの（ボードは次の SET まで同じ設定のまま）
    windows = None
    for src, cur in [(x, True) for x in sessions] + [(x, False) for x in list(lc.recent)]:
        if src.get("windows"):
            windows = {"log_id": src["log_id"], "current": cur, "bit": src.get("bit"),
                       "t": src.get("t_start"), "windows": src["windows"], "skipped": src.get("skipped", {})}
            break
    return {
        "loop_age_s": round(now_m - lc._last_loop, 1),
        "windows": windows,
        "hang_s": cfg.hang_s,
        "output": cfg.output,
        "dirs": dirs,
        "sessions": sessions,
        "recent": list(lc.recent),
        "deliver": dl,
        "disks": disks,
        "notices": [{"t": n[0], "subject": n[1], "body": n[2] if len(n) > 2 else ""} for n in list(lc.notify.recent)],
    }


class StatusWriter:
    """LC の状態とボードの状態を、1 つの JSON に定期的に書く。"""

    def __init__(self, lc: "LC", path: str, interval_s: float = 1.0, board_interval_s: float = 1.0):
        self.lc = lc
        self.path = path
        self.interval_s = interval_s
        # ボードの常駐（boardhk.py）の宛先は設定の board から決める（--fake で動かしているときも読む）
        b = lc.cfg.board if isinstance(lc.cfg.board, dict) else {}
        hk_url = f"http://{b['host']}:{b['hk_port']}/" if b.get("hk_port") and b.get("host") else None
        self.board = BoardPoller(lc.spec, board_interval_s, hk_url=hk_url)
        self.t_start = time.time()
        self.host = socket.gethostname()
        self._stop = threading.Event()
        self._err_logged = False

    def start(self) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        if not os.path.isdir(d):
            log.error("状態のページの置き場所が無い: %s（状態のページは書かない）", d)
            return
        self.board.start()
        threading.Thread(target=self._run, daemon=True, name="status").start()
        log.info("状態のページを書く: %s（%.1f s ごと）", self.path, self.interval_s)

    def stop(self) -> None:
        self._stop.set()
        self.board.stop()

    def snapshot(self) -> Dict[str, object]:
        d: Dict[str, object] = {"version": VERSION, "t": time.time(), "host": self.host, "pid": os.getpid(),
                                "started": self.t_start, "interval_s": self.interval_s,
                                "software": getattr(self.lc, "software", None)}
        try:
            d["lc"] = lc_snapshot(self.lc)
        except Exception as e:          # 表示用なので、失敗しても書き続ける
            d["lc"] = {"error": repr(e)}
        d["board"] = self.board.snapshot()
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
                if not self._err_logged:        # 書けない間、毎秒ログに出さない
                    log.error("状態のページを書けない: %s", e)
                    self._err_logged = True
            self._stop.wait(max(0.0, self.interval_s - (time.monotonic() - t0)))
