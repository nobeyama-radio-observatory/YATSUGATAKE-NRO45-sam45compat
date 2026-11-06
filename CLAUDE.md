# CLAUDE.md

このリポジトリで作業するときの前提です。詳しくは [README.md](README.md) を見てください。

## このリポジトリは何か

- YATSUGATAKE 分光計を、野辺山 45m で SAM45 と**共存**させて試験運用するための COSMOS 連携ソフト。
- **暫定**。SAM45 廃止後に archive する。最終版は別リポジトリで作り直す。
- 対象は RFSoC 4x2 を 1 台、bit は SAM45-Wide・SAM45-Fine のみ。

## 守ること

- **COSMOS の改修を最小限にする**ことを、作りのきれいさより優先する。
  ただし、その判断で入れた妥協は `docs/` に理由とともに残す（最終版で同じ判断をしないため）。
- **YATSUGATAKE には公開 API だけで触れる。** コアの内部に依存するコードを書かない。
  コアに機能が足りないときは、ここで回避する前に「汎用の機能としてコアに入れるべきか」をユーザーに確認する。
  回避した箇所には `WORKAROUND(core):` を付ける。
- **45m 固有のものをコア（YATSUGATAKE リポジトリ）に持ち込まない。**
- 使う YATSUGATAKE の版は固定する。勝手に上げない。
- 実運用で動くため、SAM45 の運用に影響しうる変更（COSMOS 側の設定、共有の資源、ネットワーク）は、実施前にユーザーに確認する。

## 書き方

- 文書・コメント・コミットメッセージは日本語。YATSUGATAKE リポジトリの書き方にそろえる。
- ソースファイルには `SPDX-License-Identifier: BSD-3-Clause` を記載する。
- ボード宛てのコマンド例では、ホストを `$B` と書く。

## 関係する場所

- YATSUGATAKE 本体: `../YATSUGATAKE`（このサーバー上の clone）
- 最終版でも使う見込みの資料: `docs/cosmos/`・`docs/comparison/`・`docs/operation/`
