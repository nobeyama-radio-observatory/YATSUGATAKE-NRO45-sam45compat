# SPDX-License-Identifier: BSD-3-Clause
"""YATSUGATAKE 暫定 LC の本体。

SAM45 のロギングの置き場所を見回り、観測ごとに:
1. L1 を読んだら、mapping で予定を作り、分光計を準備する（LOAD・SET・START）
2. YKSP のロギング（.YKSP.）に L0・L1 を書く
3. SAM45 の L2 を積分ごとにまとめ、ダンプが届いたら束ねて YKSP の L2 を書く
4. ED を読んだら、残りを書いて ED を書き、分光計を終える

段階 1・1.5（YKSP を別のファイルに書く）の形。段階 2（合体）は combine.py を足すときに出力を差し替える。
方針は docs/design/standin-study.md の「設定とタイミングの受け取り」「止まったときの備え」。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

from . import mapping as mp
from . import samlog as sl
from . import yksp as yk
from .binning import DumpSource
from .notify import Notifier

log = logging.getLogger("sam45compat")


class Spectrometer(Protocol):
    def prepare(self, plan: mp.Plan, log_id: str) -> None:
        """bit を載せ（要るときだけ）、SET し、記録を始める。失敗したら例外。"""

    def finish(self, log_id: str) -> None:
        """記録を止め、一時ファイルを片付ける。"""

    def dumps(self) -> DumpSource: ...


@dataclass
class LcConfig:
    log_dir: str                                  # SAM45 のロギングの置き場所
    out_dir: Optional[str] = None                 # YKSP のロギングの置き場所（既定は log_dir）
    in_backend: str = "SAM45"
    in_prefix: str = ""                           # 段階 2 では "_"
    out_backend: str = "YKSP"
    ifatt_mode: str = mp.IFATT_FOLLOW
    assign: Dict[str, mp.Slot] = field(default_factory=lambda: dict(mp.DEFAULT_ASSIGN))
    sam45_if_ascending: bool = True
    sam45_k_offset: float = 0.0
    shift: Optional[int] = None
    tint_s: float = 0.01024
    scale: float = 1.0
    min_coverage: float = 0.9
    flag_mask: Optional[int] = None               # None なら recorder.DEFAULT_FLAG_MASK
    t_load_k: Optional[float] = yk.T_LOAD_K
    poll_s: float = 1.0
    ready_margin_s: float = 0.05                  # 積分の終わりからこれだけ後のダンプまで届いたら束ねる
    ready_timeout_s: float = 5.0                  # これだけ待っても届かなければ、ある分で束ねる（足りなければ無効）
    skip_existing: bool = True                    # 起動したときに既にある観測は扱わない
    board: Dict[str, object] = field(default_factory=dict)   # board.BoardConfig の欄（host・ctl_port・data_port・bits・tmp_dir など）

    @classmethod
    def from_json(cls, path: str) -> "LcConfig":
        with open(path) as f:
            d = json.load(f)
        if "assign" in d:
            d["assign"] = {k: mp.Slot(v[0], int(v[1])) for k, v in d["assign"].items()}
        return cls(**d)


@dataclass
class _Pending:
    integ: yk.Integration
    closed_at: float


class Session:
    """1 つの観測（SAM45 のロギング 1 ファイル）。"""

    def __init__(self, path: str, cfg: LcConfig, spec: Spectrometer, notify: Notifier):
        self.path = path
        self.name = sl.LogName.parse(path)
        self.cfg = cfg
        self.spec = spec
        self.notify = notify
        self.follower = sl.Follower(path)
        self.grouper = yk.IntegrationGrouper()
        self.builder: Optional[yk.YkspBuilder] = None
        self.pending: List[_Pending] = []
        self.n_used = 0                 # SAM45 が使う Array の数（積分を閉じる目安）
        self.out_path = os.path.join(
            cfg.out_dir or cfg.log_dir,
            str(self.name.with_backend(cfg.out_backend).with_prefix("")))
        self._out = None
        self.done = False
        self.n_written = 0
        self.n_invalid = 0

    # ---- 1 周 ----

    def step(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        for rec in self.follower.poll():
            self._on_record(rec, now)
        self._flush_pending(now, final=False)
        if self.follower.done and not self.pending and not self.done:
            self._finish()

    def _on_record(self, rec: sl.Record, now: float) -> None:
        if rec.kind == "L1":
            self._start(self.follower.ctl, rec)
        elif rec.kind == "L2":
            for integ in self.grouper.feed(rec):
                self.pending.append(_Pending(integ, now))
            cur = self.grouper.cur
            if cur is not None and self.n_used and len(cur.records) >= self.n_used:
                self.pending.extend(_Pending(i, now) for i in self.grouper.flush())
        elif rec.kind == "ED":
            self.pending.extend(_Pending(i, now) for i in self.grouper.flush())
            self._flush_pending(now, final=True)

    def _start(self, ctl: sl.Record, obs: sl.Record) -> None:
        cfg = self.cfg
        self.n_used = sum(obs.s.iary_usefg)
        ifm = mp.Sam45IfModel(cfg.sam45_if_ascending, cfg.sam45_k_offset)
        plan = mp.plan(obs.s, cfg.assign, ifm, cfg.shift)
        targets = sorted(set(plan.windows) | set(plan.skipped))
        log.info("%s: 観測の始まり。肩代わり %s、取らない %s", self.name.log_id,
                 [sl.array_label(i) for i in plan.windows],
                 {sl.array_label(i): r for i, r in plan.skipped.items()})
        for i, r in plan.skipped.items():
            self.notify(f"{self.name.log_id}: {sl.array_label(i)} を取らない", r)
        if plan.windows:
            try:
                self.spec.prepare(plan, self.name.log_id)
            except Exception as e:
                reason = f"分光計の準備に失敗: {e}"
                self.notify(f"{self.name.log_id}: {reason}", "肩代わりする Array は idmy_flag = 1 で書く")
                plan = mp.Plan(kind=None, windows={}, skipped={i: reason for i in targets})
        from .recorder import DEFAULT_FLAG_MASK
        mask = DEFAULT_FLAG_MASK if cfg.flag_mask is None else cfg.flag_mask
        self.builder = yk.YkspBuilder(plan, cfg.ifatt_mode, self.spec.dumps(), cfg.scale,
                                      cfg.min_coverage, mask, cfg.t_load_k)
        self._out = open(self.out_path, "wb")
        self._write(self.builder.header(ctl, obs, only_targets=True))

    def _flush_pending(self, now: float, final: bool) -> None:
        if self.builder is None:
            return
        margin = int(self.cfg.ready_margin_s * 1e9)
        while self.pending:
            p = self.pending[0]
            late = now - p.closed_at >= self.cfg.ready_timeout_s
            if not (final or late or self.builder.ready(p.integ, margin)):
                break
            self.pending.pop(0)
            res = self.builder.integration(p.integ)
            self._write([r.record for r in res])
            bad = [r for r in res if not r.valid and r.array in self.builder.plan.windows]
            self.n_invalid += len(bad)
            for r in bad:
                log.warning("%s: %s %s を無効にした: %s", self.name.log_id, p.integ.key[0],
                            sl.array_label(r.array), r.reason)

    def _finish(self) -> None:
        if self._out is None:          # L1 の前に ED（ありえないが）
            self.done = True
            return
        self._write([self.builder.end()])
        self._out.close()
        self._out = None
        self.done = True
        try:
            self.spec.finish(self.name.log_id)
        except Exception as e:
            self.notify(f"{self.name.log_id}: 分光計の終わりに失敗", str(e))
        if self.n_invalid:
            self.notify(f"{self.name.log_id}: 無効にした L2 が {self.n_invalid} 個", self.out_path)
        log.info("%s: 観測の終わり。L2 %d 個（無効 %d）→ %s", self.name.log_id,
                 self.n_written, self.n_invalid, self.out_path)

    def _write(self, recs) -> None:
        for r in recs:
            self._out.write(r.to_bytes())
            self.n_written += r.kind == "L2"
        self._out.flush()

    def close(self) -> None:
        if self._out is not None:
            self._out.close()
            self._out = None


class LC:
    """置き場所を見回り、観測ごとに Session を回す。"""

    def __init__(self, cfg: LcConfig, spec: Spectrometer, notify: Optional[Notifier] = None):
        self.cfg = cfg
        self.spec = spec
        self.notify = notify or Notifier()
        self.sessions: Dict[str, Session] = {}
        self.seen = set(self._candidates()) if cfg.skip_existing else set()

    def _candidates(self) -> List[str]:
        out = []
        for n in os.listdir(self.cfg.log_dir):
            try:
                name = sl.LogName.parse(n)
            except ValueError:
                continue
            if name.backend == self.cfg.in_backend and name.prefix == self.cfg.in_prefix:
                out.append(n)
        return sorted(out)

    def step(self, now: Optional[float] = None) -> None:
        for n in self._candidates():
            if n not in self.seen:
                self.seen.add(n)
                self.sessions[n] = Session(os.path.join(self.cfg.log_dir, n), self.cfg,
                                           self.spec, self.notify)
        for n, s in list(self.sessions.items()):
            try:
                s.step(now)
            except Exception as e:
                # 1 つの観測の失敗で LC を止めない
                log.exception("%s で失敗", n)
                self.notify(f"{n}: 暫定 LC の処理に失敗", repr(e))
                s.close()
                del self.sessions[n]
                continue
            if s.done:
                del self.sessions[n]

    def close(self) -> None:
        for s in self.sessions.values():
            s.close()

    def run(self) -> None:
        while True:
            self.step()
            time.sleep(self.cfg.poll_s)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="YATSUGATAKE 暫定 LC")
    ap.add_argument("--config", required=True, help="設定（JSON。config/example.json）")
    ap.add_argument("--fake", action="store_true", help="模擬の分光計で動かす（ボードなしの試験）")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = LcConfig.from_json(a.config)
    if a.fake:
        from .fake import FakeSpectrometer
        spec = FakeSpectrometer()
    else:
        from .board import BoardConfig, BoardSpectrometer
        spec = BoardSpectrometer(BoardConfig(**cfg.board), cfg.tint_s)
    LC(cfg, spec).run()


if __name__ == "__main__":
    main()
