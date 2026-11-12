# 版の付け方と記録（試験運用。2026-11-12）

試験運用（2026-11-12 から）では、観測ごとに「どの版のソフトで取ったか」を後から引けるようにする。
観測生データ（YKSP）は観測者に渡るので、不具合が見つかったときに、どの観測が影響を受けたかを版で絞り込めるようにするため。

## 方針

1. **運用に入れる版には git のタグを付ける**（`v0.1.0` の形）。仮機・本番機は**タグの版だけ**を動かす
2. **動いている版は LC が自分で記録する**（`version.py`）。人の記録に頼らない
3. YKSP のロギングの中（L1 の `cversion` など）には版を書かない。観測者に渡るデータの形を変えるため（入れるなら別に決める）

## タグの付け方

- 名前は `vX.Y.Z`。直しだけなら Z、機能を足したら Y を上げる。`pyproject.toml` の `version` も同じ値にする
- 注釈付きのタグにし、メッセージに何を変えたかを一言書く:

```sh
# pyproject.toml の version を上げて commit した後
git tag -a v0.1.1 -m "QL の …を直した"
git push origin v0.1.1
```

- 1 つの版を動かしている間に手で直したもの（下の「手直しあり」）は、commit してタグを打ち直す。動いている版を手直しのまま残さない

## 版の区切り

| 版 | 日付 | 区切り |
|---|---|---|
| v0.1.0 | 2026-11-12 | 試験運用の始まり |
| v0.2.0 | 2026-11-13 | 段階 1 の確かめ: 観測生データ（YKSP）がマージでき、スペクトルの周波数と強度が SAM45 と合った（中身は v0.1.9 と同じ。[`plan.md`](../plan.md) の 2-5・2-6） |

## 仮機・本番機に入れる

観測の合間に行う（**観測の途中で起こし直すと、その観測は扱わない**）:

```sh
cd /home/yk/YATSUGATAKE-NRO45-sam45compat
git fetch --tags                     # 失敗したら次に進まない（submodule の取得の失敗でも止まる。&& でつないだときは checkout が走らない）
git checkout v0.1.1                  # 入れる版（タグ）
git submodule update                 # YATSUGATAKE を、この版が固定した commit にそろえる
git status --short                   # 何も出ないこと（出れば「手直しあり」になる）
sudo systemctl restart sam45compat-lc
journalctl -u sam45compat-lc -b -n 5 --no-pager    # 「版: sam45compat v0.1.1 ・ YATSUGATAKE v1.4.0」
```

- タグを `checkout` すると「detached HEAD」と出るが、それでよい（ブランチでなくタグの版を動かす）
- サービスのファイル（`tools/systemd/*.service`）が変わった版なら、[`server-setup-provisional.md`](server-setup-provisional.md) の 5.1 の手順で写し直す
- 試験の版（タグの無い commit）を一時的に動かすこともできる。そのときは版が `v0.1.1-3-gabc1234` のように出て、記録にもそのまま残る

## LC が記録するもの

LC を起こしたときに 1 回、版を読む（動いているコードは起こしたときのもので、その後の `git pull` では変わらないため）。

| 項目 | 読み方 | 例 |
|---|---|---|
| sam45compat | このリポジトリの `git describe --tags --always --dirty`（`v` で始まるタグを優先） | `v0.1.0`、`v0.1.0-3-gabc1234`、`v0.1.0-dirty` |
| YATSUGATAKE | submodule（`external/YATSUGATAKE`）の describe。LC は `s45proto` をここから読む | `v1.4.0`（同じ commit に `2g-2/v1.0.0` もあるが、`v` のタグを出す） |
| bit・specd | 観測ごとに specd の `ID`（公開の命令）。`usr` は載せた bit に焼いた USR_ACCESS（コアの commit と汚れ）、`version` は取得の版 | |

出す先:

- **journal**: 起こしたときに `版: sam45compat … ・ YATSUGATAKE …（commit …）`、観測ごとに `<ログ ID>: 版 … ・ bit …（USR_ACCESS …、specd …）`
- **状態のページ**: 「暫定 LC」の「版」と「bit の版」、「終わった観測」の「版」の列
- **版の記録**（設定の `version_log`。仮機では `/home/yk/bklog/sam45compat-versions.tsv`）: 1 観測 1 行のタブ区切り。
  列は `t_start log_id kind sam45compat yatsugatake bit bit_usr specd dirty`。LC を起こし直しても消えない（追記）

注意の色になるとき:

- **手直しあり**（`-dirty`）: commit していない変更が作業ツリーにある。版が一意に決まらない。`git status` で見て、commit してタグを打つか、`git checkout -- <ファイル>` で戻す。
  `config/*.local.json` は git に入らない（`.gitignore`）ので数えない
- **固定の版と違う**: submodule の commit が、この版の固定したものと違う。`git submodule update` を忘れている

## 引き方

```sh
# ある観測の版
grep 20261112103000 /home/yk/bklog/sam45compat-versions.tsv
# ある版で取った観測
awk -F'\t' '$4 == "v0.1.0"' /home/yk/bklog/sam45compat-versions.tsv
# 版の中身
git show v0.1.0 --stat
```

## 決めたこと・やめた案

- **版を観測データに書かない**: L1 の `cversion`（8 文字）は SAM45 の欄で、観測者に渡る。形を変えないことを優先した（CLAUDE.md の「守ること」）
- **日付のタグ（`ops-20261112`）はやめた**: 同じ日に 2 回入れると名前が足りない。何を変えたかの大きさも伝わらない
- **起こしたときに 1 回だけ読む**: 観測ごとに git を呼ぶと、`git pull` した後・起こし直す前の観測に、動いていない版を書いてしまう
