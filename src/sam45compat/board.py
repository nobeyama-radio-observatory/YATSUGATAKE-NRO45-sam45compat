# SPDX-License-Identifier: BSD-3-Clause
"""実機（specd）の分光計。lc.Spectrometer の実装。

制御の口（テキスト、1 接続まで）は命令のたびにつないで切る。データの口は Recorder がつなぎっぱなしにする。
観測ごとに: STOP（動いていれば）→ LOAD（bit が違うときだけ）→ SET → CLEAR → START → SEND ON。
終わったら STOP し、一時ファイルを消す。
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import mapping as mp
from .binning import TINT_NS
from .recorder import DiskDumps, Recorder

log = logging.getLogger("sam45compat")


class SpecdError(RuntimeError):
    pass


class Control:
    """specd の制御の口（YATSUGATAKE の specctl.Ctl と同じやりとり）。"""

    def __init__(self, host: str, port: int, timeout: float = 60.0):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.f = self.s.makefile("rwb", buffering=0)

    def cmd(self, line: str) -> str:
        self.s.sendall((line.strip() + "\n").encode())
        r = self.f.readline()
        if not r:
            raise SpecdError("specd が切った")
        return r.decode().rstrip("\n")

    def ok(self, line: str) -> Dict[str, str]:
        r = self.cmd(line)
        log.debug("specd> %s\nspecd< %s", line, r)
        if not r.startswith("OK"):
            raise SpecdError(f"{line} → {r}")
        return dict(x.split("=", 1) for x in r.split()[1:] if "=" in x)

    def close(self) -> None:
        try:
            self.cmd("BYE")
        except Exception:
            pass
        self.s.close()


@dataclass
class BoardConfig:
    host: str = "127.0.0.1"
    ctl_port: int = 51000
    data_port: int = 51001
    bits: Dict[str, str] = field(default_factory=lambda: {"fine": "sam45-fine", "wide": "sam45-wide"})
    tmp_dir: str = "/tmp/sam45compat"
    keep_tmp: bool = False
    load_timeout_s: float = 120.0


class BoardSpectrometer:
    def __init__(self, cfg: BoardConfig, tint_s: float = 0.01024):
        self.cfg = cfg
        self.tint_s = tint_s
        self.recorder = Recorder(cfg.host, cfg.data_port)
        self.recorder.start()
        self.store: Optional[DiskDumps] = None
        self.commands: List[str] = []      # 送った命令（試験と調べ用）

    def _ctl(self, timeout: float = 60.0) -> Control:
        return Control(self.cfg.host, self.cfg.ctl_port, timeout)

    def _ok(self, c: Control, line: str) -> Dict[str, str]:
        self.commands.append(line)
        return c.ok(line)

    def prepare(self, plan: mp.Plan, log_id: str) -> None:
        if plan.kind not in self.cfg.bits:
            raise SpecdError(f"bit の種類 {plan.kind!r} の名前が設定に無い")
        want = self.cfg.bits[plan.kind]
        self._close_store()
        c = self._ctl(self.cfg.load_timeout_s)
        try:
            st = self._ok(c, "STATUS")
            if st.get("state") != "IDLE":
                self._ok(c, "STOP")
            cur = self._ok(c, "BITS").get("cur")
            if cur != want:
                log.info("bit を載せ替える: %s → %s", cur, want)
                self._ok(c, f"LOAD bit={want}")
            self._ok(c, "SET " + " ".join(plan.set_args(self.tint_s)))
            expect = {w.slot.key: (w.if_mhz, w.df_mhz) for w in plan.windows.values()}
            self.store = DiskDumps(os.path.join(self.cfg.tmp_dir, log_id), expect,
                                   int(round(self.tint_s * 1e9)))
            self.recorder.set_store(self.store)
            self._ok(c, "CLEAR")
            self._ok(c, "START")
            self._ok(c, "SEND ON")
        finally:
            c.close()

    def finish(self, log_id: str) -> None:
        try:
            c = self._ctl()
            try:
                self._ok(c, "STOP")
            finally:
                c.close()
        finally:
            self._close_store()

    def _close_store(self) -> None:
        self.recorder.set_store(None)
        if self.store is not None:
            self.store.close(remove=not self.cfg.keep_tmp)
            self.store = None

    def dumps(self):
        return self.store if self.store is not None else _Empty()

    def status(self) -> Dict[str, str]:
        c = self._ctl()
        try:
            return c.ok("STATUS")
        finally:
            c.close()

    def close(self) -> None:
        self._close_store()
        self.recorder.stop()


class _Empty:
    def interval(self, key, st_ns, ed_ns):
        from .binning import Binned
        return Binned(None, 0, 0)

    def latest_end_ns(self):
        return None
