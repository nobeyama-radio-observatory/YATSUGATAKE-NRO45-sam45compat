# SPDX-License-Identifier: BSD-3-Clause
"""観測の後の処理: マージして、観測生データと YKSP のロギングを配信の置き場所に移す。

docs/design/data-delivery.md の「データの流れ」。置き場所（deliver_root、45m では /srv/download）の下:

- 観測生データ（マージの出力）: <root>/<group>/<project>/otf/ または psw/
- YKSP のロギング:             <root>/<group>/<project>/logging/

<group> は LDAP のアカウント名（Apache はログインした名前と <group> が同じときだけ通す）。
otf / psw は、どの bklog（sam45otf / sam45psw）から来たか（設定の watch の 3 つ目）で決める。

マージのソフトは別に提供される（2026-11-10 時点で未着）。merge_cmd が None なら、マージは飛ばしてロギングだけ移す。
merge_cmd は引数の並びで、{logging}（YKSP のロギング）・{outdir}（出力を置く空のフォルダ）・{kind}・{log_id}・
{group}・{project} を差し込む。出力のフォルダにできたファイルを、全部 otf/ または psw/ に移す。

暫定 LC の見回りを止めないよう、別スレッドで 1 つずつ処理する。
暫定 LC を止めると、待っていた分は一時置き場に残る。そのときは手で:
  python3 -m sam45compat.deliver <YKSP のロギング> --kind psw --root /srv/download [--merge-cmd ...]
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import List, Optional, Sequence

from . import samlog as sl
from .notify import Notifier

log = logging.getLogger("sam45compat")

KINDS = ("psw", "otf")
LOGGING_DIR = "logging"
_SAFE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_+-]*")   # フォルダの名前にしてよい <group>・<project>


def kind_of(in_dir: str) -> Optional[str]:
    """bklog の置き場所の名前から otf / psw（sam45otf → otf）。決められなければ None。"""
    base = os.path.basename(os.path.normpath(in_dir)).lower()
    found = [k for k in KINDS if base.endswith(k)]
    return found[0] if len(found) == 1 else None


@dataclass
class Job:
    logging_path: str           # 一時置き場に書き終えた YKSP のロギング
    kind: str                   # "psw" / "otf"


class Deliverer:
    def __init__(self, root: str, merge_cmd: Optional[Sequence[str]] = None,
                 notify: Optional[Notifier] = None, merge_timeout_s: float = 3600.0,
                 background: bool = True):
        self.root = root
        self.merge_cmd = list(merge_cmd) if merge_cmd else None
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
        if self.merge_cmd:
            self._merge(job, name, raw_dir)
        dst = move_file(job.logging_path, log_dir)
        self.done.append(dst)
        log.info("%s: YKSP のロギングを移した → %s", name.log_id, dst)

    def _merge(self, job: Job, name: sl.LogName, raw_dir: str) -> None:
        # 出力はまず一時置き場（ロギングと同じディスク）の空のフォルダに書かせ、できてから移す
        work = tempfile.mkdtemp(prefix=f".merge-{name.log_id}-", dir=os.path.dirname(job.logging_path))
        try:
            subs = {"logging": job.logging_path, "outdir": work, "kind": job.kind,
                    "log_id": name.log_id, "group": name.group, "project": name.project}
            cmd = [a.format(**subs) for a in self.merge_cmd]
            log.info("%s: マージ: %s", name.log_id, " ".join(cmd))
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=self.merge_timeout_s)
            except Exception as e:
                self.notify(f"{name.log_id}: マージを起動できない", repr(e))
                return
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or b"").decode(errors="replace")[-2000:]
                self.notify(f"{name.log_id}: マージに失敗（終了コード {r.returncode}）",
                            f"YKSP のロギングは移すので、後から手でやり直せる\n{tail}")
                return
            outs = sorted(os.listdir(work))
            if not outs:
                self.notify(f"{name.log_id}: マージの出力が無い", " ".join(cmd))
                return
            for f in outs:
                dst = move_file(os.path.join(work, f), raw_dir)
                self.done.append(dst)
                log.info("%s: 観測生データを移した → %s", name.log_id, dst)
        finally:
            shutil.rmtree(work, ignore_errors=True)


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
    ap.add_argument("--root", required=True, help="配信の置き場所（45m では /srv/download）")
    ap.add_argument("--merge-cmd", nargs="+", default=None,
                    help="マージの命令（{logging} {outdir} {kind} {log_id} {group} {project} を差し込む）")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    d = Deliverer(a.root, a.merge_cmd, background=False)
    for p in a.logging:
        d.submit(Job(p, a.kind))


if __name__ == "__main__":
    main()
