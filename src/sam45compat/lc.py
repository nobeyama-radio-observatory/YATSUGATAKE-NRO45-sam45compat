# SPDX-License-Identifier: BSD-3-Clause
"""YATSUGATAKE 暫定 LC の本体。

SAM45 のロギングの置き場所を見回り、観測ごとに:
1. L1 を読んだら、mapping で予定を作り、分光計を準備する（LOAD・SET・START）
2. YKSP のロギング（ファイル名の装置を YKSP にしたもの）に L0・L1 を書く
3. SAM45 の L2 を積分ごとにまとめ、ダンプが届いたら束ねて YKSP の L2 を書く
4. ED を読んだら、残りを書いて ED を書き、分光計を終える
5. deliver_root があれば、別スレッドで YKSP のロギングを配信の置き場所に移し、マージして観測生データを振り分ける（deliver.py）

出力は設定の output で選ぶ:
- "separate"（使うのはこちら）: YKSP のロギングを別のファイルに書く。肩代わりする Array だけの L1。
  45m では bklog を読み、/home/yk/bklog の下に書く（config/bklog.json）。観測の後に配信の置き場所に移し、
  別の計算機で ssh 越しにマージして観測生データ（YKSP）にする（deliver.py）
- "combined"（2026-11-10 にやめた。コードは残す）: SAM45 LC が `_` 付きの名前で書くロギングを追いかけ、
  YKSP の L2 を合体して、`_` の無い正規の名前で書く（combine.merge_integration）。
  別スレッドで心拍のファイルを更新し、暫定 LC が止まると見張り（watchdog.py）が受け持ちの印
  （<正規の名前>.passthrough）を置いて素通しで写す。暫定 LC は書く前に印を見て、あればその観測から手を引く。
方針は docs/design/standin-study.md の「方針の見直し」「設定とタイミングの受け取り」。
"""

from __future__ import annotations

import argparse
import ctypes as C
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Union

from . import combine as cb
from . import deliver as dv
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
    log_dir: str = ""                             # SAM45 のロギングの置き場所
    out_dir: Optional[str] = None                 # YKSP のロギングの置き場所（既定は log_dir）
    # 置き場所が複数のとき: [[SAM45 の置き場所, YKSP の置き場所, 種類], ...]（45m の bklog は sam45psw・sam45otf に分かれる）。
    # 種類（"psw" / "otf"）は配信の置き場所の分け方。省くと SAM45 の置き場所の名前の末尾から決める。
    # 指定すると log_dir・out_dir は使わない
    watch: Optional[List[List[str]]] = None
    output: str = "separate"                      # "separate" / "combined"（合体。2026-11-10 にやめた）
    in_backend: str = "SAM45"
    in_prefix: str = ""                           # combined では "_"
    out_backend: str = "YKSP"                     # separate のときのファイル名の装置
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
    heartbeat_path: Optional[str] = None          # combined: 心拍のファイル（見張りが見る）
    heartbeat_s: float = 2.0
    hang_s: float = 180.0                         # 本体の 1 周がこれより長く止まったら心拍を止める（LOAD の待ちより長く）
    # SAM45 のロギングに無い肩代わりの Array を、ほかの Array の設定で埋める（{"A05": "A01", ...}）。
    # SAM45 のロギングには A05〜A08・A21〜A24 が含まれないので、A01〜A04・A17〜A20 の設定から作る（2026-11-10）。
    # L1 で既に使われている Array はそのまま（SAM45 のロギングに含まれていれば、その設定を使う）
    mirror: Dict[str, str] = field(default_factory=dict)
    board: Dict[str, object] = field(default_factory=dict)   # board.BoardConfig の欄（host・ctl_port・data_port・bits・tmp_dir など）
    # 観測の後の処理（deliver.py）。None なら移さない（YKSP のロギングは out_dir に残る）。45m では /srv/download
    deliver_root: Optional[str] = None
    # マージの命令（引数の並び。psw と otf で違うなら {"psw": [...], "otf": [...]}）。None ならマージを飛ばす。
    # {logging} {root} {kind} {log_id} {obs} {group} {project} を差し込む（deliver.py）
    merge_cmd: Optional[Union[List[str], Dict[str, List[str]]]] = None
    merge_root: Optional[str] = None              # マージの計算機から見た deliver_root（NFS のマウント先）
    merge_timeout_s: float = 3600.0

    @classmethod
    def from_json(cls, path: str) -> "LcConfig":
        with open(path) as f:
            d = {k: v for k, v in json.load(f).items() if not k.startswith("_")}   # "_comment" などは注釈
        if "watch" in d and d["watch"] is not None:
            d["watch"] = [list(x) for x in d["watch"]]
        if "assign" in d:
            d["assign"] = {k: mp.Slot(v[0], int(v[1])) for k, v in d["assign"].items()}
        return cls(**d)


@dataclass
class _Pending:
    integ: yk.Integration
    closed_at: float


MARKER_SUFFIX = ".passthrough"


class TakenOver(Exception):
    """見張りが受け持った観測。"""


class Session:
    """1 つの観測（SAM45 のロギング 1 ファイル）。"""

    def __init__(self, path: str, cfg: LcConfig, spec: Spectrometer, notify: Notifier,
                 out_dir: Optional[str] = None, kind: Optional[str] = None,
                 deliverer: Optional[dv.Deliverer] = None):
        self.path = path
        self.kind = kind
        self.deliverer = deliverer
        self.name = sl.LogName.parse(path)
        self.cfg = cfg
        self.spec = spec
        self.notify = notify
        self.follower = sl.Follower(path)
        self.grouper = yk.IntegrationGrouper()
        self.builder: Optional[yk.YkspBuilder] = None
        self.pending: List[_Pending] = []
        self.n_used = 0                 # SAM45 が使う Array の数（積分を閉じる目安）
        if cfg.output not in ("separate", "combined"):
            raise ValueError(f"output {cfg.output!r}")
        self.combined = cfg.output == "combined"
        out_name = self.name.with_prefix("") if self.combined else \
            self.name.with_backend(cfg.out_backend).with_prefix("")
        self.out_path = os.path.join(out_dir or cfg.out_dir or cfg.log_dir, str(out_name))
        if os.path.abspath(self.out_path) == os.path.abspath(path):
            raise ValueError(f"出力が入力と同じ名前: {path}（combined なら in_prefix を '_' に）")
        self.marker = self.out_path + MARKER_SUFFIX
        self.taken_over = False
        self._out = None
        self.done = False
        self.n_written = 0
        self.n_invalid = 0
        self.notime_noted = False

    # ---- 1 周 ----

    def step(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        try:
            self._step(now)
        except TakenOver:
            log.warning("%s: 見張りが受け持った。この観測から手を引く", self.name.log_id)
            self.notify(f"{self.name.log_id}: 見張りが素通しで受け持った", self.out_path)
            self.taken_over = True
            self.done = True
            self.close()
            try:
                self.spec.finish(self.name.log_id)
            except Exception:
                pass

    def _step(self, now: float) -> None:
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
        filled: Dict[int, int] = {}
        if cfg.mirror:
            filled = mirror_sources(obs, cfg.mirror)
            obs = mirror_arrays(obs, cfg.mirror)
        ifm = mp.Sam45IfModel(cfg.sam45_if_ascending, cfg.sam45_k_offset)
        plan = mp.plan(obs.s, cfg.assign, ifm, cfg.shift)
        targets = sorted(set(plan.windows) | set(plan.skipped))
        log.info("%s: 観測の始まり。肩代わり %s、取らない %s", self.name.log_id,
                 [sl.array_label(i) for i in plan.windows],
                 {sl.array_label(i): r for i, r in plan.skipped.items()})
        for i, r in plan.skipped.items():
            self.notify(f"{self.name.log_id}: {sl.array_label(i)} を取らない", r)
        from .recorder import DEFAULT_FLAG_MASK
        mask = DEFAULT_FLAG_MASK if cfg.flag_mask is None else cfg.flag_mask
        self.builder = yk.YkspBuilder(plan, cfg.ifatt_mode, self.spec.dumps(), cfg.scale,
                                      cfg.min_coverage, mask, cfg.t_load_k, template_src=filled)
        # 頭は準備（LOAD は 10〜20 s）を待たずに書く（combined で正規のファイルを遅らせないため）
        self._check_marker()
        self._out = open(self.out_path, "wb")
        self._write(self.builder.header(ctl, obs, only_targets=not self.combined))
        if plan.windows:
            try:
                self.spec.prepare(plan, self.name.log_id)
            except Exception as e:
                reason = f"分光計の準備に失敗: {e}"
                self.notify(f"{self.name.log_id}: {reason}", "肩代わりする Array は idmy_flag = 1 で書く")
                self.builder.plan = mp.Plan(kind=None, windows={}, skipped={i: reason for i in targets})
        self.builder.dumps = self.spec.dumps()

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
            self._check_notime()
            if self.combined:
                self._write(cb.merge_integration(p.integ.records, {r.array: r.record for r in res}))
            else:
                self._write([r.record for r in res])
            bad = [r for r in res if not r.valid and r.array in self.builder.plan.windows]
            self.n_invalid += len(bad)
            for r in bad:
                log.warning("%s: %s %s を無効にした: %s", self.name.log_id, p.integ.key[0],
                            sl.array_label(r.array), r.reason)

    def _check_notime(self) -> None:
        """観測の途中で分光計が時刻を失ったら、すぐ運用者に知らせる（観測に 1 回）。

        specd は RUN の間は錨を打ち直せないので、この観測の残りの肩代わりは無効になる（次の観測の準備で打ち直す）。
        観測の終わりの「無効にした L2」の通知だけでは遅いので、気づいた時点で知らせる。
        """
        n = getattr(self.builder.dumps, "n_notime", 0)
        if n and not self.notime_noted:
            self.notime_noted = True
            self.notify(f"{self.name.log_id}: 分光計が時刻を失った（時刻の無いダンプ {n} 個）",
                        "この観測の残りの肩代わりは idmy_flag = 1 になる。次の観測の準備で錨を打ち直す")

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
        if self.deliverer is not None and not self.combined:
            self.deliverer.submit(dv.Job(self.out_path, self.kind))

    def _check_marker(self) -> None:
        if os.path.exists(self.marker):
            raise TakenOver(self.marker)

    def _write(self, recs) -> None:
        self._check_marker()
        for r in recs:
            self._out.write(r.to_bytes())
            self.n_written += r.kind == "L2"
        self._out.flush()

    def close(self) -> None:
        if self._out is not None:
            self._out.close()
            self._out = None


def mirror_sources(obs: sl.Record, mirror: Dict[str, str]) -> Dict[int, int]:
    """埋める Array → 元の Array（添字）。L1 で使われていない Array（鍵）のうち、元（値）が使われているものだけ。"""
    o = obs.s
    out = {}
    for dst_name, src_name in sorted(mirror.items()):
        dst, src = sl.array_index(dst_name), sl.array_index(src_name)
        if not o.iary_usefg[dst] and o.iary_usefg[src]:
            out[dst] = src
    return out


def mirror_arrays(obs: sl.Record, mirror: Dict[str, str]) -> sl.Record:
    """L1 で使われていない Array（鍵）を、使われている Array（値）の設定で埋めた L1 の写し。

    SAM45 のロギングに無い肩代わりの Array（A05〜A08・A21〜A24）を、A01〜A04・A17〜A20 の設定から作る（2026-11-10）。
    Array ごとの欄（32 要素の配列）を全部写す。周波数の欄は、この後 mapping.rewrite_l1（f2）で YATSUGATAKE の値になる。
    """
    obs = sl.copy_record(obs)
    o = obs.s
    done = []
    for dst, src in mirror_sources(obs, mirror).items():
        dst_name, src_name = sl.array_label(dst), sl.array_label(src)
        for fname, ftype in type(o)._fields_:
            arr = getattr(o, fname)
            if isinstance(arr, C.Array) and len(arr) == sl.ARYMAX:
                n = C.sizeof(arr) // sl.ARYMAX
                base = C.addressof(arr)
                C.memmove(base + dst * n, base + src * n, n)
        o.iary_num += 1
        done.append(f"{dst_name}←{src_name}")
    if done:
        log.info("SAM45 のロギングに無い Array を、写した設定で埋めた: %s", " ".join(done))
    return obs


class LC:
    """置き場所を見回り、観測ごとに Session を回す。"""

    def __init__(self, cfg: LcConfig, spec: Spectrometer, notify: Optional[Notifier] = None):
        self.cfg = cfg
        self.spec = spec
        self.notify = notify or Notifier()
        self.sessions: Dict[str, Session] = {}
        self._unreadable = set()
        self._primed = set()            # 一度でも読めた置き場所（skip_existing の「既にある」を決めた）
        self.seen = set()
        self.deliverer: Optional[dv.Deliverer] = None
        if cfg.deliver_root:
            if cfg.output != "separate":
                raise ValueError("deliver_root は output が separate のときだけ")
            for in_dir, _, kind in self._dirs():
                if kind not in dv.KINDS:
                    raise ValueError(f"{in_dir}: 種類（psw / otf）を決められない。watch の 3 つ目に書く")
            self.deliverer = dv.Deliverer(cfg.deliver_root, cfg.merge_cmd, self.notify, cfg.merge_timeout_s,
                                          merge_root=cfg.merge_root)
        self._candidates()
        self._last_loop = time.monotonic()
        self._hb_stop = threading.Event()
        if cfg.heartbeat_path:
            threading.Thread(target=self._heartbeat, daemon=True, name="heartbeat").start()

    def _heartbeat(self) -> None:
        """心拍のファイルを更新する。本体の 1 周が hang_s より長く止まっていたら更新しない（見張りに任せる）。"""
        while not self._hb_stop.wait(self.cfg.heartbeat_s):
            if time.monotonic() - self._last_loop > self.cfg.hang_s:
                continue
            tmp = self.cfg.heartbeat_path + ".tmp"
            try:
                with open(tmp, "w") as f:
                    f.write(f"{time.time():.3f} {os.getpid()}\n")
                os.replace(tmp, self.cfg.heartbeat_path)
            except OSError as e:
                log.error("心拍を書けない: %s", e)

    def _dirs(self) -> List[tuple]:
        """[(SAM45 の置き場所, YKSP の置き場所, 種類 psw / otf / None)]。"""
        if self.cfg.watch:
            return [(w[0], w[1], w[2] if len(w) > 2 else dv.kind_of(w[0])) for w in self.cfg.watch]
        return [(self.cfg.log_dir, self.cfg.out_dir or self.cfg.log_dir, dv.kind_of(self.cfg.log_dir))]

    def _candidates(self) -> Dict[str, tuple]:
        """{SAM45 のロギングのパス: (YKSP の置き場所, 種類)}。置き場所を読めなければ（NFS の一時の不調）、その回は飛ばす。"""
        out = {}
        for in_dir, out_dir, kind in self._dirs():
            try:
                names = os.listdir(in_dir)
            except OSError as e:
                if in_dir not in self._unreadable:
                    log.error("置き場所を読めない: %s（%s）", in_dir, e)
                    self._unreadable.add(in_dir)
                continue
            self._unreadable.discard(in_dir)
            found = []
            for n in names:
                try:
                    name = sl.LogName.parse(n)
                except ValueError:
                    continue
                if name.backend == self.cfg.in_backend and name.prefix == self.cfg.in_prefix:
                    found.append(os.path.join(in_dir, n))
            if in_dir not in self._primed:
                # 初めて読めたとき（起動時、または起動後にマウントされたとき）にあるものは扱わない
                self._primed.add(in_dir)
                if self.cfg.skip_existing:
                    self.seen.update(found)
                    log.info("置き場所を読めた: %s（既にある %d 個は扱わない）", in_dir, len(found))
            for path in found:
                out[path] = (out_dir, kind)
        return dict(sorted(out.items()))

    def step(self, now: Optional[float] = None) -> None:
        self._last_loop = time.monotonic()
        for path, (out_dir, kind) in self._candidates().items():
            if path not in self.seen:
                self.seen.add(path)
                log.info("新しいロギング: %s → %s", path, out_dir)
                try:
                    os.makedirs(out_dir, exist_ok=True)
                    self.sessions[path] = Session(path, self.cfg, self.spec, self.notify, out_dir,
                                                  kind, self.deliverer)
                except Exception as e:
                    log.exception("%s を扱えない", path)
                    self.notify(f"{os.path.basename(path)}: 暫定 LC が扱えない", repr(e))
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
        self._hb_stop.set()
        for s in self.sessions.values():
            s.close()
        if self.deliverer is not None:
            self.deliverer.close()

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
