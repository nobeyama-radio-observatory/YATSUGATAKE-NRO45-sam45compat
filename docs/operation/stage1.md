# 段階 1 の運用手順（同じ IF・IFATT に従う）

段階 1 では、SAM45 の A01〜A04 と**同じ IF** を YATSUGATAKE（A05〜A08・A21〜A24 の受け持ち）に入れて観測する。
暫定 LC は SAM45 のロギング（bklog）を読み、A05〜A08・A21〜A24 の YKSP のロギングを `/home/yk/bklog` の下に書く。
観測が終わったら、暫定 LC が YKSP のロギングを `/srv/download` に移し、マージの計算機で ssh 越しにマージを起動して観測生データ（YKSP）を作る（[`../design/data-delivery.md`](../design/data-delivery.md)）。
YKSP のロギング（と観測生データ）を SAM45 と比べる。
SAM45 のロギングには手を入れない（読むだけ）。SAM45 のロギングには A05〜A08・A21〜A24 が含まれない。YKSP は A01〜A04・A17〜A20 の設定（L1）と L2 の付随の欄から A05〜A08・A21〜A24 を作る（2026-11-10）。方式は [`../design/standin-study.md`](../design/standin-study.md)、作業の一覧は [`../plan.md`](../plan.md) の 2.。

> **未定の部分**には【未定】と書いてある。決まったら埋める。
> ボード宛てのコマンドでは、ホストを `$B` と書く。

## 0. 準備（観測の前に 1 回）

### ボード（RFSoC 4x2）

- [ ] YATSUGATAKE の版は **v1.4.0**（このリポジトリの `external/YATSUGATAKE` と同じ）
- [ ] ボードの `bits.json` に SAM45-Fine と SAM45-Wide がある。**その名前を設定の `board.bits` に書く**（下の「設定」）
- [ ] 10 MHz・1PPS を SAM45 と同じ基準からつなぐ
- [ ] specd を起こす（YATSUGATAKE の [`docs/user/specd.md`](../../external/YATSUGATAKE/docs/user/specd.md)。仮機ではボードの systemd のサービス [`tools/systemd/specd.service`](../../tools/systemd/specd.service)、[`server-setup-provisional.md`](server-setup-provisional.md) の 5.6）。時刻を答えられることを確かめる:
  ```bash
  python3 external/YATSUGATAKE/sw/pynq/specctl.py --host $B STATUS     # time_ok=1・state=IDLE
  python3 external/YATSUGATAKE/sw/pynq/specctl.py --host $B BITS       # bits= に設定の名前がある
  ```
- [ ] tint 10.24 ms で取りこぼしなく運転できることは、YATSUGATAKE 側の試験で確かめておく（[`../plan.md`](../plan.md) の 7.）

### IF

- [ ] IF 系で、A01〜A04 と同じ IF を、IF 入力 5〜8 にも出す（SAM45 の IF 系の機能。運用の経験あり）
- [ ] IF 入力 5〜8 の線を、RFSoC の ADC_A〜D（A05・A21 → ADC_A … A08・A24 → ADC_D）に入れる【未定: 分岐か、つなぎ替えか】
- [ ] **IFATT の制御プログラムは改修しない**（段階 1 は IFATT に「従う」。IF 入力 5〜8 も R で ATT が入る）

### 暫定 LC を動かす計算機

- [ ] SAM45 のロギングの置き場所（bklog の `sam45psw/`・`sam45otf/`）を**読める**（NFS で読み取り専用。[`server-setup-provisional.md`](server-setup-provisional.md) の 5.1）、YKSP のロギングの置き場所 **`/home/yk/bklog`** に**書ける**
- [ ] ボードの制御の口（51000）・データの口（51001）に届く
- [ ] Python 3.9 以上と numpy
- [ ] このリポジトリを clone する:
  ```bash
  # 公開リポジトリなので HTTPS で鍵なしで取れる（submodule の URL は SSH なので、HTTPS に読み替える）
  git -c url."https://github.com/".insteadOf=git@github.com: \
      clone --recurse-submodules https://github.com/nobeyama-radio-observatory/YATSUGATAKE-NRO45-sam45compat.git
  cd YATSUGATAKE-NRO45-sam45compat              # external/YATSUGATAKE は v1.4.0
  PYTHONPATH=src python3 -m unittest discover -s tests    # 試験が通ること（ボードは要らない）
  ```
- [ ] 一時ファイルの置き場所（`board.tmp_dir`）に空きがある。**10.24 ms で 8 窓は ≒ 46 GB/時**（観測が終わると消す）

### 設定

45m では `config/bklog.json`（bklog を読み `/home/yk/bklog` に書く。`mirror` で A05〜A08・A21〜A24 を A01〜A04・A17〜A20 から作る）を写して直す。段階 1 で見るところ:

| 欄 | 段階 1 の値 |
|---|---|
| `watch` | 読む場所と書く場所の組: `[["/cosmos3/45m/bklog/sam45psw", "/home/yk/bklog/yksppsw"], ["/cosmos3/45m/bklog/sam45otf", "/home/yk/bklog/ykspotf"]]`（`config/bklog-dummy.json` と同じ。`watch` があると `log_dir`・`out_dir` は使わない） |
| `assign` | A05〜A08・A21〜A24（`config/bklog.json` のまま） |
| `output` | `"separate"`（既定） |
| `in_prefix` | `""` |
| `ifatt_mode` | `"follow"` |
| `board.host` | ボード（`$B`） |
| `board.bits` | ボードの `bits.json` の名前（`{"fine": …, "wide": …}`） |
| `board.tmp_dir` | 一時ファイルの置き場所（`config/bklog.json` では一時置き場のディスクの `/home/yk/bklog/tmp`） |
| `t_load_k` | 293.0（Tsys の T_R。担当者に確認中） |
| `board.no_pps` | `false`。1PPS なしの試験だけ `true`（下） |

**1PPS なしの試験**（`board.no_pps: true`。2026-11-11）: 1PPS・10 MHz をつなぐ前に、実機のスペクトルで流れ（記録 → 束ね → YKSP のロギング → マージ → 配信）を確かめる。
specd は `--clkin 0 --ref 10` を外して（ボードの水晶で）起こす。暫定 LC は `START force=1` で時刻なしで取り、最初のダンプを受けた時刻（暫定 LC の計算機の時計、NTP で合わせる）を起点に、
ビート（`dump_t`）の差から時刻を付ける。ずれはネットワークと specd の溜まりの遅れぶん（数 ms〜数十 ms）で、積分の端のダンプが入れ替わりうる。**科学には使えない**（ログに「1PPS なしの試験」と出る）

## 1. 暫定 LC を起こす

```bash
PYTHONPATH=src python3 -m sam45compat.lc --config my.json 2>&1 | tee -a lc.log
```

- 起こしたときに既にある観測は扱わない。**観測を始める前に起こしておく**
- ボードなしで流れだけを確かめるなら `--fake`（模擬の分光計）

## 2. 観測する

### 観測指示書

- A01〜A04 と A05〜A08 を**同じ周波数・帯域**にする（A05〜A08 が YATSUGATAKE の受け持ち。2 つ目の窓なら A17〜A20 と A21〜A24）
- 2000 MHz の帯域は、2 つ目の窓（A21〜A24）を取れない
- **A05〜A08 は A01〜A04、A21〜A24 は A17〜A20 と同じ設定になる**（SAM45 のロギングに含まれないので、暫定 LC が写して作る。本番も同じ制約）
- 帯域は、肩代わりする Array の中で **500 MHz 以上と 250 MHz 以下を混ぜない**（混ぜると 250 MHz 以下だけを取り、残りは無効）
- 積分は PSW で始める（比較の道具の Ta* は PSW を前提にしている。OTF は値の比と時刻だけを比べる）

### 観測中に見るもの

- 暫定 LC のログ: 観測の始まりに「観測の始まり。肩代わり [...]、取らない {...}」。**取らない Array があれば理由を見る**
- 通知（今はログに `通知:` で出る）: 分光計の準備の失敗・取らない Array・分光計が時刻を失った（観測の途中、気づいた時点で 1 回）・無効にした L2
- `/home/yk/bklog` の下の YKSP のロギングが積分ごとに伸びている
- specd の `STATUS`（`miss`・`drop` が 0 のまま）。**暫定 LC が制御の口を使っている間（観測の始まり）は `ERR BUSY` になりうる**

## 3. 観測の後: マージ

- [ ] 暫定 LC のログに「観測の終わり。L2 n 個（無効 m）」が出て、YKSP のロギングの最後が ED になっている:
  ```bash
  PYTHONPATH=src python3 -c "import sys; from sam45compat import samlog as sl; print(sl.summary(sl.read_file(sys.argv[1])))" <YKSP のロギング>
  # (L2 の数, 使う Array の数, ED があるか)
  ```
- [ ] 暫定 LC のログに「YKSP のロギングを移した」「マージ: ssh …」「観測生データを移した」が出て、`/srv/download/<group>/<project>/` の `logging/` と `psw/`（または `otf/`）にファイルがある。失敗したら通知が来る（やり直しは [`../design/data-delivery.md`](../design/data-delivery.md) の「実装」）
- [ ] SAM45 のロギングも、いつもどおりマージされていることを確かめる
- [ ] **肩代わりする Array だけの L1（`iary_usefg`・`iary_num`）でマージが通るか**を記録する（通らなければ、そのとき考える）
- [ ] マージが `dbebw` の値（32 MHz など、SAM45 に無い値）・`idmy_flag` = 1 で困らないかを記録する

## 4. 比べる

```bash
PYTHONPATH=src python3 -m sam45compat.compare <SAM45 のロギング> <YKSP のロギング> --npz cmp_<ログ ID>.npz
```

見るもの（判定の目安は**案**。最初の比較の結果を見て決め直す）:

| 項目 | 目安（案） | 外れたら |
|---|---|---|
| 軸の中心の差 | 1 ch 以内、向きが同じ | SAM45 の ch と IF の対応の仮定（`sam45_if_ascending`・`sam45_k_offset`）を見直す |
| 積分の対応 | 全部共通、YKSP の無効 0 | 暫定 LC のログ（ダンプが足りない・FLAGS・準備の失敗） |
| 値の比のばらつき（ON・OFF） | 積分ごとに 1 % 以下 | 利得の変動、IF の分け方 |
| R/SKY・Tsys | SAM45 と 5 % 以内 | IFATT（段階 1 は従う）、レベル（飽和）、T_R |
| Ta* の差の rms | ch の雑音の √2 倍程度 | 帯域の形、量子化、軸のずれ |
| 線の中心のずれ | 0.5 ch 以内 | 軸（f2 の書き換え、SAM45 の ch と IF の対応） |
| ピーク | 5 % 以内 | 分解能の違い（ch 間隔 2.4 %）を考えて見る |

- 結果は `docs/comparison/` に、ログ ID・設定・比べた結果を残す（最終版に引き継ぐ）
- `--npz` の軸と Ta* は、手元で図にする（`<Array>_sky_hz`・`<Array>_ta`・`<YKSP の Array>_ta_on_<SAM45 の Array>`）
- **SAM45 の ch と IF の対応の向き**（今は仮定）は、窓を中央（3 GHz）から外した観測で確かめる。軸の中心の差が窓の位置に応じて大きくなれば、向きが逆

## 5. 片付け

- 暫定 LC を止める（Ctrl-C）。観測の途中で止めると、その観測の YKSP のロギングは ED の無いまま残る（SAM45 には影響しない）
- 一時ファイルの置き場所（`board.tmp_dir`）が空になっている（観測が終わると消す。`keep_tmp` が true なら残る）
- specd を `STOP` しておく（暫定 LC は観測の終わりに `STOP` を打つ）

## 困ったとき

| 起きたこと | 見るところ |
|---|---|
| 「分光計の準備に失敗」 | specd が起きているか・`STATUS`・`BITS` に設定の名前があるか。肩代わりの Array は無効（`idmy_flag` = 1）で書かれている |
| 「X を取らない」 | 理由（帯域の外・Wide と Fine の混在・2048 MHz の 2 つ目の窓・同じ ADC で束ねが違う） |
| 「無効にした L2 が n 個」 | 暫定 LC のログの「を無効にした」の理由（ダンプが足りない・FLAGS） |
| 「時刻を答えられない…錨を打ち直す」 | PPS の照合が一度外れた（`STATUS` の `time_err`）。暫定 LC は観測の始まりに毎回 `ANCHOR` を打つ（v0.1.8 から）ので、次の観測では戻る。観測の途中で外れると、その後のダンプは時刻が無く使えない（L2 は無効）。度々出るなら 10 MHz・1PPS の配線 |
| 「記録の軸が予定と違う」 | SET が効いていない。specd の `GET` で窓の設定を見る |
| YKSP のロギングができない | `watch` の読む場所に SAM45 のロギングが見えているか（bklog のマウント）、`/home/yk/bklog` に書けるか、暫定 LC を観測の前に起こしたか |
| 一時ファイルの置き場所が溢れる | `board.tmp_dir` の空き（≒ 46 GB/時） |
