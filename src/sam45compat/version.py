# SPDX-License-Identifier: BSD-3-Clause
"""動いているソフトの版（2026-11-12。試験運用で、観測ごとにどの版で取ったかを後から引けるように）。

- sam45compat: このリポジトリの `git describe --tags --always --dirty`（v で始まるタグを優先。例: v0.1.0、v0.1.0-3-gabc1234、…-dirty）。
  git で読めなければ（clone でない置き方）、pyproject.toml の版（パッケージの版）
- YATSUGATAKE: submodule（external/YATSUGATAKE）の describe（例: v1.4.0）。recorder が s45proto をここから読むので、
  LC の中で動いているのはこの版。superproject が固定した commit と違えば mismatch を立てる
- 読むのは LC を起こしたときの 1 回だけ（software()）。動いているコードは起こしたときのもので、
  その後の git pull では変わらないため

ボードの bit と specd の版は specd の ID 命令で読む（board.BoardSpectrometer.ident。観測ごと）。
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CORE = os.path.join("external", "YATSUGATAKE")

_cache: Optional[Dict[str, object]] = None


def _git(args, cwd: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _describe(cwd: str) -> Optional[str]:
    """v で始まるタグを優先する（YATSUGATAKE の固定 commit には v1.4.0 と 2g-2/v1.0.0 が付いていて、
    README の書き方は v1.4.0）。v のタグが無ければほかのタグ、タグが無ければ commit の短いハッシュ。"""
    return (_git(["describe", "--tags", "--always", "--dirty", "--match", "v[0-9]*"], cwd)
            if _git(["describe", "--tags", "--match", "v[0-9]*"], cwd) else
            _git(["describe", "--tags", "--always", "--dirty"], cwd))


def _package_version() -> Optional[str]:
    try:
        from importlib.metadata import version
        return version("sam45compat")
    except Exception:
        return None


def read(root: str = ROOT) -> Dict[str, object]:
    """今の作業ツリーの版を読む（git を呼ぶ）。"""
    own = _describe(root) if os.path.exists(os.path.join(root, ".git")) else None
    d: Dict[str, object] = {
        "sam45compat": own or _package_version() or "unknown",
        "commit": _git(["rev-parse", "HEAD"], root) if own else None,
        "dirty": bool(own and own.endswith("-dirty")),
    }
    core_dir = os.path.join(root, CORE)
    core = _describe(core_dir) if os.path.exists(os.path.join(core_dir, ".git")) else None
    pinned = None
    if own:
        ls = _git(["ls-tree", "HEAD", CORE], root)               # "160000 commit <sha>\texternal/YATSUGATAKE"
        if ls:
            pinned = ls.split()[2]
    core_sha = _git(["rev-parse", "HEAD"], core_dir) if core else None
    d.update({
        "yatsugatake": core or "unknown",
        "yatsugatake_commit": core_sha,
        "yatsugatake_pinned": pinned,
        "yatsugatake_mismatch": bool(pinned and core_sha and pinned != core_sha),
    })
    d["dirty"] = bool(d["dirty"] or (core and core.endswith("-dirty")))
    return d


def software() -> Dict[str, object]:
    """起こしたときの版（1 回だけ読み、後は同じものを返す）。"""
    global _cache
    if _cache is None:
        _cache = read()
    return _cache


def short(sw: Dict[str, object]) -> str:
    """1 行の表し方（journal と版の記録用）。"""
    s = f"sam45compat {sw.get('sam45compat')} ・ YATSUGATAKE {sw.get('yatsugatake')}"
    if sw.get("yatsugatake_mismatch"):
        s += f"（固定は {str(sw.get('yatsugatake_pinned'))[:7]}。違う）"
    return s


LOG_COLUMNS = ("t_start", "log_id", "kind", "sam45compat", "yatsugatake", "bit", "bit_usr", "specd", "dirty")


def observation(sw: Dict[str, object], ident: Optional[Dict[str, str]], bit: Optional[str]) -> Dict[str, object]:
    """1 つの観測の版（状態のページと版の記録に出す）。ident は specd の ID の答え（模擬・読めなければ None か error）。"""
    ident = ident or {}
    return {"sam45compat": sw.get("sam45compat"), "yatsugatake": sw.get("yatsugatake"),
            "bit": ident.get("bit") or bit, "bit_usr": ident.get("usr"), "specd": ident.get("version"),
            "dirty": bool(sw.get("dirty")), "mismatch": bool(sw.get("yatsugatake_mismatch")),
            "ident_error": ident.get("error")}


def append_log(path: str, t_start: str, log_id: str, kind: Optional[str], v: Dict[str, object]) -> None:
    """版の記録（タブ区切り。1 観測 1 行。無ければ見出しの行から書く）に 1 行足す。"""
    row = {"t_start": t_start, "log_id": log_id, "kind": kind, **v}
    row["dirty"] = "dirty" if v.get("dirty") else ""
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a") as f:
        if new:
            f.write("\t".join(LOG_COLUMNS) + "\n")
        f.write("\t".join("-" if row.get(k) in (None, "") and k != "dirty" else str(row.get(k) or "")
                          for k in LOG_COLUMNS) + "\n")
