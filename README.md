# YATSUGATAKE-NRO45-sam45compat

野辺山 45m 鏡の分光計 SAM45 のうち、**故障した Array を
[YATSUGATAKE](https://github.com/nobeyama-radio-observatory/YATSUGATAKE) で肩代わり**するためのソフトウェアです。
YATSUGATAKE に SAM45 を模倣させ、観測者からは SAM45 が故障していないように見せます。

> **これは暫定のリポジトリです。**
> SAM45 が廃止され、45m の分光計が YATSUGATAKE に一本化された時点で役目を終え、archive します。
> 最終的な 45m 用の連携ソフトウェアは、構成から作り直して別のリポジトリで開発します。
> ここのコードを最終版の土台にすることは想定していません。
>
> English: Temporary software that lets YATSUGATAKE stand in for the failed arrays of the SAM45
> spectrometer at the Nobeyama 45 m telescope, so that observers see an intact SAM45.
> To be archived once SAM45 is retired.

## 目的

SAM45 は 16 の IF 入力を持ち、各入力を **Array**（A01〜A16）と呼びます。
1 つの IF 入力には窓（SpW）を最大 2 つ設定でき、2 つ目の窓は A17〜A32 として扱われます（A_n の 2 つ目の窓が A_(n+16)）。

SAM45 のうち **A05〜A08 と A21〜A24** が故障しました。これは IF 入力 4 本ぶん（A05〜A08）の、1 つ目と 2 つ目の窓にあたります。
この 4 本の IF を YATSUGATAKE の RFSoC 1 台で分光し、SAM45 の出力として見せます。

| SAM45 の Array | IF 入力 | 窓 | YATSUGATAKE（案） |
|---|---|---|---|
| A05 / A21 | 5 | 1 つ目 / 2 つ目 | ADC_A の窓 1 / 窓 2 |
| A06 / A22 | 6 | 1 つ目 / 2 つ目 | ADC_B の窓 1 / 窓 2 |
| A07 / A23 | 7 | 1 つ目 / 2 つ目 | ADC_C の窓 1 / 窓 2 |
| A08 / A24 | 8 | 1 つ目 / 2 つ目 | ADC_D の窓 1 / 窓 2 |

ほかの Array（A01〜A04、A09〜A20、A25〜A32）は、これまでどおり SAM45 が分光します。

## 対象とする構成

| 項目 | 条件 |
|---|---|
| ボード | RFSoC 4x2 を **1 台**（ADC 4 本 = IF 入力 4 本） |
| bit | **SAM45-Wide**・**SAM45-Fine** のみ（どちらも ADC 4 本 × 窓 2 つまで、4096 ch） |
| 肩代わりする Array | A05〜A08、A21〜A24 |
| 観測者から見えるもの | 故障していない SAM45（観測指示書・観測生データは今までどおり） |
| 観測システム | 既存の COSMOS（改修は最小限） |

上の条件から外れる使い方（ボードを複数台にする、ほかの bit を使う、ほかの Array を肩代わりするなど）は、
このリポジトリの範囲外です。

## 設計の方針

1. **スピードを最優先する。** SAM45 はすでに故障しているので、できるだけ早く実装を終える。
   YATSUGATAKE に最適化した作りは最終版で行う。COSMOS のマネージャーは改修しない（再コンパイルが要りリスクがあるため）。
2. **観測者の手順の変更を最小にする。** 観測生データの形は変えない。
   観測指示書に変換スクリプトをかける程度の変更は許容する。
3. **YATSUGATAKE には公開 API だけで触れる。** コアの内部（レジスタ・内部モジュール）に直接触らない。
   必要な機能が足りないときは、**45m 固有でない汎用の機能として**コア側に追加する。
   コアの不足を回避するためにこちらで処理を書いた場合は、`WORKAROUND(core):` の印を付ける。
4. **コアの版を固定する。** 実運用で動くため、使う YATSUGATAKE の版はタグで固定し、
   上げるときは試験してから上げる。
5. **45m 固有のものはここに置く。** Array の割り当て、SAM45 の模倣に要る変換、運用手順など。

## 最終版に引き継ぐもの

コードは捨てる前提ですが、次のものは最終版でも使える見込みがあるため、
ほかの部分と分けて置きます。

- 既存の COSMOS・SAM45 の構成とインターフェースの調査メモ・通信の記録 → `docs/cosmos/`
- 実データでの比較（SAM45 と YATSUGATAKE）の試験データと手順 → `docs/comparison/`
- 45m での運用手順 → `docs/operation/`

## リポジトリの構成（予定）

| 場所 | 中身 |
|---|---|
| `docs/plan.md` | 残りの作業の一覧 |
| `docs/cosmos/` | 既存の COSMOS・SAM45 の構成（最終版に引き継ぐ） |
| `docs/design/` | 肩代わりの検討・決めたこと（この暫定の構成に固有） |
| `docs/` のほか | 比較の試験（`comparison/`）・運用手順（`operation/`） |
| `src/sam45compat/` | 暫定 LC（Python 3.9 以上・numpy） |
| `external/YATSUGATAKE/` | YATSUGATAKE（git submodule、版を固定） |
| `config/` | 45m 固有の設定（Array の割り当てなど） |
| `tests/` | 試験（下の「試験」） |

## 使う YATSUGATAKE の版

| YATSUGATAKE | 備考 |
|---|---|
| **v1.4.0** | `external/YATSUGATAKE` の git submodule で固定（2026-11-06）。clone の後に `git submodule update --init` |

## 試験

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
# 実際の SAM45 のロギングを使う試験（サンプルはリポジトリに入れていない）
SAM45_SAMPLE=/path/to/<ログ ID>.SAM45.<指示書名>.<group>.<project> PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 動かし方

```bash
cp config/example.json my.json          # log_dir などを直す
PYTHONPATH=src python3 -m sam45compat.lc --config my.json --fake
# 別の端末で、記録済みの SAM45 のロギングを書いている途中のように流す
python3 tools/replay.py <SAM45 のロギング> <log_dir> --speed 60 --log-id 20261106130000
```

暫定 LC は `log_dir` に新しくできた SAM45 のロギングを追いかけ、同じ場所に `.YKSP.` のロギングを書く。
`--fake` を外すと、設定の `board`（specd の宛先・ポート・bit の名前・一時ファイルの置き場所）の実機につなぐ。
ボードが無くても、YATSUGATAKE の specd を PL なしで起こせば相手にできる（試験 `tests/test_board.py` もこれを使う）:

```bash
(cd external/YATSUGATAKE/sw/pynq && python3 specd.py --fake --cpu -1 --rt 0)   # bit は "default" の 1 つ
```

### 段階 1 の比較

```bash
# SAM45（A01〜A04）と YKSP（A05〜A08）を比べる。合体したロギングなら同じファイルを 2 回
PYTHONPATH=src python3 -m sam45compat.compare <SAM45 のロギング> <YKSP のロギング> --npz cmp.npz
```

### 段階 1.1・2

```bash
# 段階 1.1: 記録済みの SAM45 と YKSP のロギングを合体する（手でマージする前に）
PYTHONPATH=src python3 -m sam45compat.combine <SAM45 のロギング> <YKSP のロギング> -o <合体したロギング>
# 段階 2: 設定を output: "combined"・in_prefix: "_"・heartbeat_path にして暫定 LC を動かし、見張りを別に動かす
PYTHONPATH=src python3 -m sam45compat.watchdog --log-dir <log_dir> --heartbeat <heartbeat_path>
```

## 廃止の条件

次のどちらかになったら、このリポジトリを archive します。

- 45m の分光計が YATSUGATAKE に一本化され（SAM45 が運用から外れ、最終版の連携ソフトウェアが運用に入った）、
  上の「最終版に引き継ぐもの」を最終版のリポジトリに移した
- SAM45 の故障した Array が修理され、肩代わりが不要になった

## 関係するリポジトリ

- [YATSUGATAKE](https://github.com/nobeyama-radio-observatory/YATSUGATAKE) — 分光計本体（汎用。bit と制御ソフト）
- 最終版の 45m 連携ソフトウェア — 未作成

## ライセンス

[BSD 3-Clause License](LICENSE)（YATSUGATAKE と同じ）。
