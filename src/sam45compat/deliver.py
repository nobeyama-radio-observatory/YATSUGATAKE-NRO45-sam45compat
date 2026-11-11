# SPDX-License-Identifier: BSD-3-Clause
"""観測の後の処理: YKSP のロギングを配信の置き場所に移し、マージして、観測生データを振り分ける。

docs/design/data-delivery.md の「データの流れ」。置き場所（deliver_root、45m では /srv/download）の下:

- YKSP のロギング:             <root>/<group>/<project>/logging/
- 観測生データ（マージの出力）: <root>/<group>/<project>/otf/ または psw/

<group> は LDAP のアカウント名（Apache はログインした名前と <group> が同じときだけ通す）。
otf / psw は、どの bklog（sam45otf / sam45psw）から来たか（設定の watch の 3 つ目）で決める。

マージ（2026-11-11 に仕様が決まった）は、置き場所を NFS でマウントした別の計算機で ssh 越しに走らせる。
マージは <project>/ の直下に出力を書くので、走らせる前と後で <project>/ の直下のファイルを比べ、
増えたファイルを otf/ または psw/ に移す。

- merge_cmd: 引数の並び（psw と otf で違うなら {"psw": [...], "otf": [...]}）。None ならマージを飛ばす。
  {logging}（マージの計算機から見た、logging/ に移した後のロギング）・{root}（マージの計算機から見た置き場所）・
  {kind}・{log_id}・{obs}・{group}・{project} を差し込む
- merge_root: マージの計算機から見た置き場所（NFS のマウント先）。None なら deliver_root と同じ

ssh は引数を空白でつないで向こうのシェルに渡すので、差し込む値はシェルで特別な意味を持つ文字を含まないものだけにする。

暫定 LC の見回りを止めないよう、別スレッドで 1 つずつ処理する。
暫定 LC を止めると、待っていた分は一時置き場に残る。マージに失敗したときは logging/ に移した後なので、そのパスで。
どちらも手で:
  python3 -m sam45compat.deliver <YKSP のロギング> --kind psw --config <設定>   （設定の deliver_root・merge_cmd・merge_root）
  python3 -m sam45compat.deliver <YKSP のロギング> --kind psw --root /srv/download   （マージなし）
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Union

from . import samlog as sl
from .notify import Notifier

log = logging.getLogger("sam45compat")

KINDS = ("psw", "otf")
LOGGING_DIR = "logging"
_SAFE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_+-]*")   # フォルダの名前にしてよい <group>・<project>
_SHELL_SAFE = re.compile(r"[A-Za-z0-9_./+-]+")       # ssh 越しに渡してよい値

MergeCmd = Union[Sequence[str], Dict[str, Sequence[str]], None]


def kind_of(in_dir: str) -> Optional[str]:
    """bklog の置き場所の名前から otf / psw（sam45otf → otf）。決められなければ None。"""
    base = os.path.basename(os.path.normpath(in_dir)).lower()
    found = [k for k in KINDS if base.endswith(k)]
    return found[0] if len(found) == 1 else None


@dataclass
class Job:
    logging_path: str           # 書き終えた YKSP のロギング（一時置き場、または logging/ に移した後）
    kind: str                   # "psw" / "otf"


class Deliverer:
    def __init__(self, root: str, merge_cmd: MergeCmd = None,
                 notify: Optional[Notifier] = None, merge_timeout_s: float = 3600.0,
                 background: bool = True, merge_root: Optional[str] = None):
        self.root = root
        self.merge_root = merge_root or root
        if isinstance(merge_cmd, dict):
            bad = set(merge_cmd) - set(KINDS)
            if bad:
                raise ValueError(f"merge_cmd の種類 {sorted(bad)}（psw / otf）")
            self.merge_cmds = {k: list(v) for k, v in merge_cmd.items() if v}
        elif merge_cmd:
            self.merge_cmds = {k: list(merge_cmd) for k in KINDS}
        else:
            self.merge_cmds = {}
        self.notify = notify or Notifier()
        self.merge_timeout_s = merge_timeout_s
        self.done: List[str] = []           # 試験で見る（移した先のパス）
        self._q: "queue.Queue[Optional[Job]]" = queue.Queue()
        self._thread = None
        if background:
            self._thread = threading.Thread(target=self._worker, daemon=True, name="deliver")
            self._thread.start()

    # ---- 受け付け ----

    def submit(self, job: Job) -> None:
        if self._thread is None:
            self.process(job)
        else:
            self._q.put(job)

    def close(self, wait: bool = True) -> None:
        if self._thread is not None:
            self._q.put(None)
            if wait:
                self._thread.join()

    def _worker(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                return
            try:
                self.process(job)
            except Exception as e:          # 1 つの失敗で止めない
                log.exception("%s の後の処理に失敗", job.logging_path)
                self.notify(f"{os.path.basename(job.logging_path)}: 観測の後の処理に失敗", repr(e))

    # ---- 1 観測 ----

    def dest_dirs(self, logging_path: str, kind: str):
        """(観測生データの置き場所, ロギングの置き場所)。"""
        if kind not in KINDS:
            raise ValueError(f"kind {kind!r}（psw / otf）")
        name = sl.LogName.parse(logging_path)
        for v in (name.group, name.project):
            if not _SAFE.fullmatch(v):
                raise ValueError(f"フォルダの名前にできない: {v!r}")
        base = os.path.join(self.root, name.group, name.project)
        return os.path.join(base, kind), os.path.join(base, LOGGING_DIR)

    def process(self, job: Job) -> None:
        name = sl.LogName.parse(job.logging_path)
        raw_dir, log_dir = self.dest_dirs(job.logging_path, job.kind)
        here = os.path.join(log_dir, os.path.basename(job.logging_path))
        if os.path.abspath(job.logging_path) == os.path.abspath(here):
            dst = here                      # 手でやり直すとき（もう logging/ にある）
        else:
            dst = move_file(job.logging_path, log_dir)
            self.done.append(dst)
            log.info("%s: YKSP のロギングを移した → %s", name.log_id, dst)
        if job.kind in self.merge_cmds:
            self._merge(job, name, dst, raw_dir)

    def _merge(self, job: Job, name: sl.LogName, logging_path: str, raw_dir: str) -> None:
        proj = os.path.dirname(os.path.dirname(logging_path))          # <root>/<group>/<project>
        rel = os.path.relpath(logging_path, self.root)
        subs = {"logging": os.path.join(self.merge_root, rel), "root": self.merge_root,
                "kind": job.kind, "log_id": name.log_id, "obs": name.obs_file,
                "group": name.group, "project": name.project}
        for k, v in subs.items():
            if not _SHELL_SAFE.fullmatch(v):
                raise ValueError(f"マージに渡せない {k}: {v!r}")
        cmd = [a.format(**subs) for a in self.merge_cmds[job.kind]]
        before = _files(proj)
        log.info("%s: マージ: %s", name.log_id, " ".join(cmd))
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=self.merge_timeout_s)
        except Exception as e:
            self.notify(f"{name.log_id}: マージを起動できない、または時間切れ",
                        f"{e!r}\nやり直し: python3 -m sam45compat.deliver {logging_path} --kind {job.kind} ...")
            return
        new = sorted(_files(proj) - before)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or b"").decode(errors="replace")[-2000:]
            left = f"\n{proj} に残ったファイル: {' '.join(new)}" if new else ""
            self.notify(f"{name.log_id}: マージに失敗（終了コード {r.returncode}）",
                        f"YKSP のロギングは {logging_path} にある。後から手でやり直せる{left}\n{tail}")
            return
        if not new:
            self.notify(f"{name.log_id}: マージの出力が無い", " ".join(cmd))
            return
        for f in new:
            d = move_file(os.path.join(proj, f), raw_dir)
            self.done.append(d)
            log.info("%s: 観測生データを移した → %s", name.log_id, d)


def _files(d: str) -> set:
    """d の直下の普通のファイルの名前（フォルダ・隠しファイルは除く）。"""
    try:
        with os.scandir(d) as it:
            return {e.name for e in it if e.is_file(follow_symlinks=False) and not e.name.startswith(".")}
    except FileNotFoundError:
        return set()


def move_file(src: str, dst_dir: str) -> str:
    """src を dst_dir に移す。同じ名前があれば上書きせず例外。

    別のディスクへは、写してから名前を付け替える（途中のファイルを Apache から見せない）。
    配信のため、ファイルは 0644・フォルダは 0755（Apache の利用者が読む）。
    """
    os.makedirs(dst_dir, mode=0o755, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(src))
    if os.path.lexists(dst):
        raise FileExistsError(dst)
    try:
        os.rename(src, dst)                 # 同じディスクなら付け替えだけ
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        part = os.path.join(dst_dir, f".{os.path.basename(src)}.part")
        shutil.copyfile(src, part)
        with open(part, "rb") as f:
            os.fsync(f.fileno())
        if os.path.lexists(dst):
            os.unlink(part)
            raise FileExistsError(dst)
        os.replace(part, dst)
        os.unlink(src)
    os.chmod(dst, 0o644)
    return dst


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="観測の後の処理を手で行う（マージして、配信の置き場所に移す）")
    ap.add_argument("logging", nargs="+", help="書き終えた YKSP のロギング")
    ap.add_argument("--kind", required=True, choices=KINDS)
    ap.add_argument("--config", default=None,
                    help="暫定 LC の設定（JSON）。deliver_root・merge_cmd・merge_root・merge_timeout_s を使う")
    ap.add_argument("--root", default=None, help="配信の置き場所（45m では /srv/download）。--config より優先")
    ap.add_argument("--no-merge", action="store_true", help="設定に merge_cmd があってもマージしない")
    a = ap.parse_args(argv)
    c = {}
    if a.config:
        with open(a.config) as f:
            c = json.load(f)
    root = a.root or c.get("deliver_root")
    if not root:
        ap.error("--root か、deliver_root のある --config が要る")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    d = Deliverer(root, None if a.no_merge else c.get("merge_cmd"), background=False,
                  merge_root=c.get("merge_root"), merge_timeout_s=c.get("merge_timeout_s", 3600.0))
    for p in a.logging:
        d.submit(Job(p, a.kind))


if __name__ == "__main__":
    main()
