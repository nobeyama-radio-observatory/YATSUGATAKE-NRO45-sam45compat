# SPDX-License-Identifier: BSD-3-Clause
"""段階 2 の見張り: 暫定 LC が止まったら、SAM45 のロギング（`_` 付き）を正規の名前に素通しで写す。

docs/design/standin-study.md の「止まったときの備え」。YATSUGATAKE に依らず、写すだけの単純なもの。
- 暫定 LC の心拍のファイルが stale_s より古い（か無い）とき、ED まで書かれていない正規のファイル
  （または、まだ無い正規のファイル）を受け持つ
- 受け持ちの印（<正規の名前>.passthrough）を置く。暫定 LC は書く前に印を見て、あれば手を引く
- 正規のファイルを最後の完全な積分の頭まで切り詰め、`_` のファイルの続きから ED までそのまま写す
  （肩代わりの Array は SAM45 が書いたまま）
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, Optional

from . import combine as cb
from . import samlog as sl
from .lc import MARKER_SUFFIX
from .notify import Notifier

log = logging.getLogger("sam45compat")


@dataclass
class WatchdogConfig:
    log_dir: str
    heartbeat_path: str
    out_dir: Optional[str] = None
    in_backend: str = "SAM45"
    in_prefix: str = "_"
    stale_s: float = 30.0
    poll_s: float = 1.0


class _Copy:
    def __init__(self, src: str, dst: str, pos: int):
        self.src, self.dst, self.pos = src, dst, pos
        self.done = False

    def step(self) -> None:
        with open(self.src, "rb") as f:
            f.seek(self.pos)
            buf = f.read()
        n = 0
        with open(self.dst, "ab") as out:
            for r in sl.iter_records(buf):
                out.write(r.to_bytes())
                n = r.offset + sl.SIZES[r.kind]
                if r.kind == "ED":
                    self.done = True
        self.pos += n


class Watchdog:
    def __init__(self, cfg: WatchdogConfig, notify: Optional[Notifier] = None):
        self.cfg = cfg
        self.notify = notify or Notifier()
        self.copies: Dict[str, _Copy] = {}
        self.finished = set()

    def heartbeat_age(self, now: float) -> float:
        try:
            with open(self.cfg.heartbeat_path) as f:
                t = float(f.read().split()[0])
        except (OSError, ValueError, IndexError):
            return float("inf")
        return now - t

    def _dst(self, name: sl.LogName) -> str:
        return os.path.join(self.cfg.out_dir or self.cfg.log_dir, str(name.with_prefix("")))

    @staticmethod
    def _finished(path: str) -> bool:
        if not os.path.exists(path):
            return False
        with open(path, "rb") as f:
            return cb.last_complete_integration_start(f.read())[1]

    def step(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        stale = self.heartbeat_age(now) > self.cfg.stale_s
        for n in sorted(os.listdir(self.cfg.log_dir)):
            try:
                name = sl.LogName.parse(n)
            except ValueError:
                continue
            if name.backend != self.cfg.in_backend or name.prefix != self.cfg.in_prefix:
                continue
            if n in self.copies or n in self.finished:
                continue
            dst = self._dst(name)
            if self._finished(dst):
                self.finished.add(n)
                continue
            if not (stale or os.path.exists(dst + MARKER_SUFFIX)):
                continue
            self._take_over(n, dst)
        for n, c in list(self.copies.items()):
            c.step()
            if c.done:
                log.info("%s: 素通しで ED まで写した → %s", n, c.dst)
                del self.copies[n]
                self.finished.add(n)

    def _take_over(self, n: str, dst: str) -> None:
        src = os.path.join(self.cfg.log_dir, n)
        open(dst + MARKER_SUFFIX, "a").close()      # 先に印（暫定 LC に手を引かせる）
        pos, last = cb.passthrough_resume(src, dst)
        if pos < 0:
            self.finished.add(n)
            return
        log.warning("%s: 暫定 LC の心拍が無い。素通しで受け持つ（%s の続きから）", n, last)
        self.notify(f"{sl.LogName.parse(n).log_id}: 見張りが素通しで受け持った",
                    f"{dst}（肩代わりの Array は SAM45 が書いたまま）")
        self.copies[n] = _Copy(src, dst, pos)

    def run(self) -> None:
        while True:
            try:
                self.step()
            except Exception:
                log.exception("見張りの 1 周に失敗")
            time.sleep(self.cfg.poll_s)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="段階 2 の見張り（暫定 LC が止まったら素通し）")
    ap.add_argument("--log-dir", required=True)
    ap.add_argument("--heartbeat", required=True)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--stale-s", type=float, default=30.0)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Watchdog(WatchdogConfig(a.log_dir, a.heartbeat, a.out_dir, stale_s=a.stale_s)).run()


if __name__ == "__main__":
    main()
