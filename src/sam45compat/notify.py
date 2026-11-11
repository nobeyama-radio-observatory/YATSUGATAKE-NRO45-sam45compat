# SPDX-License-Identifier: BSD-3-Clause
"""運用者への通知。手段（メール・Slack など）は未定なので、今はログに出すだけ。
命令を設定すれば、通知ごとにその命令を実行する（本文は標準入力に渡す）。"""

from __future__ import annotations

import logging
import subprocess
import time
from collections import deque
from typing import List, Optional, Sequence

log = logging.getLogger("sam45compat")


class Notifier:
    def __init__(self, command: Optional[Sequence[str]] = None):
        self.command = list(command) if command else None
        self.sent: List[str] = []       # 試験で見る
        self.recent = deque(maxlen=30)  # 状態のページ（status.py）に出す: (unix 秒, 件名)

    def __call__(self, subject: str, body: str = "") -> None:
        text = f"{subject}\n{body}".rstrip()
        self.sent.append(text)
        self.recent.append((time.time(), subject))
        log.warning("通知: %s %s", subject, body)
        if self.command:
            try:
                subprocess.run(self.command + [subject], input=text.encode(), timeout=10, check=False)
            except Exception as e:     # 通知の失敗で LC を止めない
                log.error("通知の命令に失敗: %s", e)
