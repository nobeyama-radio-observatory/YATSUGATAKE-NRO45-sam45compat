# YATSUGATAKE-NRO45-sam45compat

野辺山 45m 鏡で、分光計 [YATSUGATAKE](https://github.com/nobeyama-radio-observatory/YATSUGATAKE) を
既存の分光計 SAM45 と**共存**させて試験運用するための、観測システム COSMOS との連携ソフトウェアです。

> **これは暫定のリポジトリです。**
> SAM45 が廃止され、45m の分光計が YATSUGATAKE に一本化された時点で役目を終え、archive します。
> 最終的な 45m 用の連携ソフトウェアは、構成から作り直して別のリポジトリで開発します。
> ここのコードを最終版の土台にすることは想定していません。
>
> English: Temporary COSMOS interface for running YATSUGATAKE alongside the existing SAM45
> spectrometer at the Nobeyama 45 m telescope. To be archived once SAM45 is retired.

## 対象とする構成

| 項目 | この試験運用での条件 |
|---|---|
| ボード | RFSoC 4x2 を **1 台** |
| bit | **SAM45-Wide**・**SAM45-Fine** のみ |
| 既存の分光計 | SAM45 と**並行して稼働**（置き換えない） |
| 観測システム | 既存の COSMOS（改修は最小限） |

上の条件から外れる使い方（ボードを複数台にする、ほかの bit を使う、SAM45 を外すなど）は、
このリポジトリの範囲外です。

## 設計の方針

1. **COSMOS の改修を最小限にする。** 速さを優先し、COSMOS から見て既存の仕組みに近い形で
   YATSUGATAKE をつなぐ。YATSUGATAKE に最適化した作りは最終版で行う。
2. **YATSUGATAKE には公開 API だけで触れる。** コアの内部（レジスタ・内部モジュール）に直接触らない。
   必要な機能が足りないときは、**45m 固有でない汎用の機能として**コア側に追加する。
   コアの不足を回避するためにこちらで処理を書いた場合は、`WORKAROUND(core):` の印を付ける。
3. **コアの版を固定する。** 実運用で動くため、使う YATSUGATAKE の版はタグで固定し、
   上げるときは試験してから上げる。
4. **45m 固有のものはここに置く。** IF の割り当て、SAM45 と共存するための設定、運用手順など。

## 最終版に引き継ぐもの

コードは捨てる前提ですが、次のものは最終版でも使える見込みがあるため、
ほかの部分と分けて置きます。

- COSMOS 側のインターフェースの調査メモ・通信の記録 → `docs/cosmos/`
- 実データでの比較（SAM45 と YATSUGATAKE）の試験データと手順 → `docs/comparison/`
- 45m での運用手順 → `docs/operation/`

## リポジトリの構成（予定）

| 場所 | 中身 |
|---|---|
| `docs/` | 調査メモ・決めたこと・運用手順 |
| `src/` | COSMOS 連携のソフトウェア |
| `config/` | 45m 固有の設定（IF の割り当てなど） |
| `test/` | 試験 |

## 使う YATSUGATAKE の版

| YATSUGATAKE | 備考 |
|---|---|
| （未定） | 固定する方法（submodule かパッケージの版指定か）も未定 |

## 廃止の条件

次がすべて満たされたら、このリポジトリを archive します。

- 45m の分光計が YATSUGATAKE に一本化され、SAM45 が運用から外れた
- 最終版の連携ソフトウェアが運用に入った
- 上の「最終版に引き継ぐもの」を最終版のリポジトリに移した

## 関係するリポジトリ

- [YATSUGATAKE](https://github.com/nobeyama-radio-observatory/YATSUGATAKE) — 分光計本体（汎用。bit と制御ソフト）
- 最終版の 45m 連携ソフトウェア — 未作成

## ライセンス

[BSD 3-Clause License](LICENSE)（YATSUGATAKE と同じ）。
