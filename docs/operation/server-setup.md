# 【本番機】ストレージ・制御サーバーのセットアップ

仮機の記録は [`server-setup-provisional.md`](server-setup-provisional.md)。ここでは本番機の手順と、仮機との違いを書く。
仮機と同じ手順は、仮機の記録の節を指す。

- 記録を始めた日: 2026-11-19
- 作業者: 西村
- 状態: 作業中（OS のインストールから）

## 本番機の構成

| 項目 | 値 |
|---|---|
| 機種 | （記入） |
| CPU・メモリ | （記入） |
| ディスク | NVMe SSD 500 GB × 1（OS・一時置き場）、SATA HDD 12 TB × 6（ストレージ、ZFS RAIDZ2）。RAID カードなし（SATA に直に接続） |
| NIC | オンボード 2.5 Gbps、PCIe 10 Gbps。後で USB の 1 Gbps を足す |
| BMC | なし（作業はモニターとキーボードをつないで行う） |
| ホスト名・IP | 仮機と入れ替えるので、仮機と同じものにする（値は文書に書かない） |
| OS | Ubuntu Server 26.04.x LTS（`/etc/os-release` で確認: （記入）） |

## 0. ディスクの分け方（2026-11-19 に決めた）

[`../design/data-delivery.md`](../design/data-delivery.md) の決まり（一時置き場とストレージを別のディスクにし、ストレージは RAIDZ）に合わせる。

| ディスク | 使い道 | 作る時 |
|---|---|---|
| NVMe 500 GB | ESP 1 GB・`/boot` 2 GB・LVM（`ubuntu-vg`）。`/`（`ubuntu-lv`）は 100 GB | インストーラー |
| 〃（`ubuntu-vg` の残り） | 一時置き場 `/home/yk/bklog`（LV `bklog`、340 GB、ext4）。VG に ≒ 20 GB を余らせる | インストールの後（2.1） |
| HDD 12 TB × 6 | ZFS の RAIDZ2（`tank`）。`/srv/download` | インストールの後（2.2） |

- 一時置き場: 10.24 ms の一時ファイルは観測中に ≒ 46 GB/時 になる。340 GB なら途切れなく ≒ 7 時間。それより長い観測が続くと足りなくなる
- 仮機では `ubuntu-lv` を VG いっぱいにしたが、本番機では**広げない**。残りを一時置き場の LV に使う
- 一時置き場をインストーラーで作らないのは、`yk` を作る前に `/home/yk` を root の持ち物として作ってしまうため
- RAIDZ2 にした理由: 12 TB の HDD は作り直し（resilver）に長くかかり、その間にもう 1 台が壊れると RAIDZ1 ではデータを失う。使える容量は ≒ 4 台分（≒ 43 TiB からメタデータの分を引いたもの）

## 1. インストール

USB の作り方は、仮機の記録の 1 章と同じ。

### 1.1 BIOS / UEFI

- 起動は UEFI。Secure Boot は有効のままでよい（Ubuntu の ZFS のモジュールは署名されている）
- SATA のモードが AHCI であることを確かめる（RAID になっていたら AHCI にする）
- 6 台の HDD と NVMe が見えていることを確かめる
- 時計を合わせる（UTC か JST か: （記入））
- 停電から戻ったときの動き（AC Power Recovery）: （記入。BMC が無いので、自動で電源が入る設定にするかを決める）

### 1.2 インストーラー

仮機の記録の 3 章と同じ。違うところ:

| 画面 | 選んだもの | メモ |
|---|---|---|
| Network | DHCP。つながっている NIC 1 本だけ | **仮機が動いている間は、仮機の IP を使わない**。固定 IP は入れ替えのときに設定する（4 章） |
| Storage | Use an entire disk で **NVMe を選ぶ** ＋ Set up this disk as an LVM group。暗号化はしない | 確認画面で次を確かめる: HDD 6 台が使われていないこと（`Available devices` に残る）、`ubuntu-lv` が 100 GB 前後で、`ubuntu-vg` に空きが残っていること。**`ubuntu-lv` は広げない** |
| Profile | ホスト名は、入れ替えまでは仮機と区別できる名前にする（例: `yk-new`）。ユーザーは管理者のアカウント | 入れ替えのときに `hostnamectl` で仮機の名前に変える |
| SSH | Install OpenSSH server にチェック | |

## 2. インストール後

仮機の記録の 4 章と同じ（`apt full-upgrade`・タイムゾーン・パッケージ・SSH の鍵）。固定 IP（4.1）と `/etc/hosts`（4.3）は入れ替えのときに行う。

確かめる: `lsblk -o NAME,SIZE,TYPE,MOUNTPOINTS,MODEL`・`sudo vgs`・`sudo lvs`

本番機で見えたもの（2026-11-20）:

| デバイス | 中身 |
|---|---|
| `nvme0n1` | WD Blue SN570 500GB（465.8 GiB）。p1 `/boot/efi` 1 GiB・p2 `/boot` 2 GiB・p3 LVM（`ubuntu-vg` 462.7 GiB、うち `ubuntu-lv` 100 GiB、空き 362.7 GiB） |
| `sda`〜`sdf` | Seagate ST12000VN0008（IronWolf 12 TB、10.9 TiB）× 6 |
| NIC | `enp2s0`（PCIe の 10 G）・`enp7s0`（オンボードの 2.5 G）（`ethtool` の Supported link modes で確かめた）、`wlp6s0`（無線。使わない） |

- NVMe（SN570）の書き込みの寿命は 300 TBW。一時置き場に観測 1 時間あたり ≒ 46 GB 書くので、年に 2,000 時間観測すると ≒ 92 TB/年（≒ 3 年）。
  `sudo smartctl -a /dev/nvme0n1` の `Percentage Used`・`Data Units Written` をときどき見る（`smartmontools`）

### 2.0 更新したら再起動する（2026-11-20 に決めた）

`apt upgrade` の後は、**再起動を基本とする**。

- upgrade の最後に needrestart がサービスを自動で起こし直す（systemd-resolved・systemd-networkd も含む）が、systemd 本体とカーネルは再起動まで古いまま残り、版の混ざった状態になる
- 本番機で、最初の `apt upgrade` の後に再起動しないでいたら apt の取得が極端に遅くなり（Waiting for headers）、再起動で直った（2026-11-19〜20。設定は変えていない。原因は確かめていない）。
  仮機でも、needrestart が systemd-resolved を起こし直した前後で名前を引けなくなった（仮機の記録の 4.3）
- 再起動が要るかは `ls /var/run/reboot-required`（あれば要る）、何のためかは `cat /var/run/reboot-required.pkgs`
- **運用に入った後は、観測のない時間に upgrade と再起動をまとめて行う**（観測中に暫定 LC・ボードとの通信が止まるため）。
  unattended-upgrades の自動の再起動は止めたまま（既定）にし、needrestart から暫定 LC を外す（仮機の記録の 5.1）

```sh
sudo apt update && sudo apt full-upgrade -y
ls /var/run/reboot-required 2>/dev/null && cat /var/run/reboot-required.pkgs
sudo reboot
```

### 2.1 一時置き場（`/home/yk/bklog`）

**アカウントを、管理（`ykadm`）と運用（`yk`）に分ける**（2026-11-20）。仮機は `yk` 1 つ（uid 1000、sudo あり）で管理も運用もしていた。

- `ykadm`（uid 1000、sudo あり）: インストーラーで作った。ssh で入り、`apt`・`systemctl`・設定を扱う
- `yk`（uid 1001、sudo なし、パスワードなし）: 暫定 LC（`User=yk`）が動く。ssh の鍵（ボード・マージ）・`config/bklog.local.json`・clone を持つ。入るときは `sudo -iu yk`
- 分けた理由: 暫定 LC は外（bklog・ボード・マージの計算機）とつながるので、そこから root を取られないようにする
- uid は仮機（1000）とずれるが、困らない: サービスは名前（`User=yk`）で指し、NFS の `anonuid` は本番機の `id -u yk` で入れる（仮機の記録の 5.3）。
  仮機から写すときは `rsync -a` を **`--numeric-ids` なしで**使う（持ち主を名前で合わせる）


```sh
sudo adduser --disabled-password --comment '' yk   # 運用の専用。sudo に入れない。uid は 1001 になる（2026-11-20）
sudo lvcreate -L 340G -n bklog ubuntu-vg
sudo mkfs.ext4 -L bklog /dev/ubuntu-vg/bklog
sudo -u yk mkdir /home/yk/bklog
echo '/dev/ubuntu-vg/bklog  /home/yk/bklog  ext4  defaults,noatime  0 2' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo mount /home/yk/bklog
sudo chown yk:yk /home/yk/bklog
sudo tune2fs -m 0 /dev/ubuntu-vg/bklog                # root 用の予約（既定 5% ≒ 17 GB）を外す。データだけのファイルシステムなので要らない
df -h /home/yk/bklog
```

- `/home/yk` は `yk` 以外は入れない（Ubuntu の既定の 750）。`ykadm` からは `sudo df -h /home/yk/bklog` で見る

- 2026-11-20: 334 GB、予約を外す前の空きは 317 GB

### 2.2 ストレージ（ZFS RAIDZ2、`/srv/download`）

ディスクは `/dev/sdX` ではなく `/dev/disk/by-id/` の名前で指す（起動のたびに `sdX` の順番が変わりうるため）。

```sh
sudo apt install -y zfsutils-linux
ls -l /dev/disk/by-id/ | grep -E 'ata-' | grep -v part   # HDD 6 台の名前を確かめる
sudo zpool create -o ashift=12 \
  -O compression=lz4 -O atime=off -O xattr=sa -O acltype=posixacl \
  -m none tank raidz2 \
  /dev/disk/by-id/ata-ST12000VN0008-3MH101_WZ00H92H \
  /dev/disk/by-id/ata-ST12000VN0008-3MH101_WZ00H9LA \
  /dev/disk/by-id/ata-ST12000VN0008-3MH101_WZ00B2DZ \
  /dev/disk/by-id/ata-ST12000VN0008-3MH101_ZZ00K4NF \
  /dev/disk/by-id/ata-ST12000VN0008-3MH101_WZ00FBXZ \
  /dev/disk/by-id/ata-ST12000VN0008-3MH101_ZZ00HV5C
sudo zfs create -o mountpoint=/srv/download tank/download
zpool status tank
zfs list
```

- 2026-11-20: 6 台とも ONLINE、`tank/download` の AVAIL は 43.5T。再起動の後も `/srv/download` がマウントされた

- 2026-11-20 に見えた HDD（シリアル → `sdX`）: WZ00H92H → sda、WZ00H9LA → sdb、WZ00B2DZ → sdc、ZZ00K4NF → sdd、WZ00FBXZ → sde、ZZ00HV5C → sdf
- 壊れた HDD を抜くときに取り違えないよう、筐体のどの段がどのシリアルかを記入し、段にシリアルのラベルを貼る: （記入）
- 確かめる: 起こし直して `/srv/download` が自動でマウントされること（`zfs-mount.service`）
- scrub は `zfsutils-linux` の既定（毎月第 2 日曜日、`/etc/cron.d/zfsutils-linux`）で動く
- ディスクが壊れたことを知らせる手段（ZED のメール）: 運用者への通知の手段（[`../plan.md`](../plan.md) の 6 章）と一緒に決める
- NFS で `/srv/download` を公開するときは、`systemctl edit nfs-server` で `RequiresMountsFor=/srv/download` を足す（仮機の記録の 5.3 の「まだ」）

## 3. sam45compat を動かす

（仮機の記録の 5 章を、本番機で順に行う。入れる版はタグの版だけ（[`versioning.md`](versioning.md)））

`yk` で行う（`ykadm` で入って `sudo -iu yk`）。clone の場所は `/home/yk/YATSUGATAKE-NRO45-sam45compat`。

```sh
git clone --recurse-submodules https://github.com/nobeyama-radio-observatory/YATSUGATAKE-NRO45-sam45compat.git
cd YATSUGATAKE-NRO45-sam45compat
git checkout <版>
git submodule update --init --recursive
python3 -m venv .venv && . .venv/bin/activate
pip install -e . pytest
pytest
```

- 2026-11-20: v0.3.2、Python 3.14.4。1 failed・108 passed・14 skipped。
  落ちたのは `tests/test_version.py` の `test_tag_and_dirty`。試験が `git tag -a` に名前とメールを渡しておらず、`yk` に `git config user.name` が無いと落ちる（仮機の `yk` には設定があった）。
  試験を直した（v0.3.3）。ソフトの動きには関係しない
- 本番機の `yk` には `git config user.name` を設定していない（`yk` では commit しない）

## 4. 仮機から入れ替える

（記入: 手順を決める。観測のない時間に行う）

- 固定 IP・ホスト名を仮機のものにする（仮機の記録の 4.1。入れ替えの前に仮機を止めるか、仮機の IP を外す）
- SSH のホストの鍵（`/etc/ssh/ssh_host_*`）を仮機から写すと、マージの計算機などの `known_hosts` を直さずに済む
- 仮機の `/srv/download` と `/home/yk/bklog` に残っているものを写す
- `yk` の `~/.ssh`（ボードの鍵・マージの鍵）、`config/bklog.local.json`、Apache の設定と証明書

## NIC の割り当て

### 起動が systemd-networkd-wait-online で止まる（2026-11-20）

起動のたびに `Job systemd-networkd-wait-online.service/start running (… / no limit)` で長く待った。
wait-online は、netplan に書いた NIC が**すべて**つながるまで待つ。ケーブルを挿していない NIC（`enp2s0`）が netplan にあると、そこで待たされる。

- 待っていたのは `enp2s0`（インストーラーが netplan に名前だけ書いていた）と `wlp6s0`（netplan に無いが networkd が拾っていた）
- 対処（2026-11-20。これで起動が速くなった）:
  - 使っていない NIC、つながっていなくても起動を止めたくない NIC に、netplan で `optional: true` を付ける（`/etc/netplan/00-installer-config.yaml` の `enp2s0`）
  - 無線は networkd の管理から外す: `/etc/systemd/network/05-wlp6s0-unmanaged.network` に `[Match] Name=wlp6s0`・`[Link] Unmanaged=yes`
- `optional: true` を付けても、`networkctl list` の SETUP は `configuring` のまま（待たなくなるだけ）。`networkctl status enp2s0` の `Required For Online: no` で確かめる
- 入れ替えのときの固定 IP（4 章）でも同じ: **bklog 用の NIC だけ**を待つ（bklog の NFS のマウントがネットワークを要るため）。
  ボード用・ダウンロード用・USB の NIC には `optional: true` を付ける（ボードの電源が落ちていても起動が止まらないように）


（記入: 2.5 Gbps・10 Gbps・USB 1 Gbps を、ボード用・bklog 用・ダウンロード用のどれに当てるか）

- USB の NIC は、抜けたり止まったりしたときに観測が止まらない用途（ダウンロード用）に当てる案

## 仮機との違い

| 項目 | 仮機 | 本番機 |
|---|---|---|
| ディスク | SSD 500 GB × 1、RAID なし。`ubuntu-lv` を VG いっぱいに広げた | NVMe 500 GB（OS・一時置き場の LV）＋ HDD 12 TB × 6（ZFS RAIDZ2、`/srv/download`） |
| NIC | （仮機の記録を参照） | 2.5 Gbps ＋ 10 Gbps（＋ USB 1 Gbps） |
| ホスト名・IP | — | 入れ替えのときに仮機と同じにする |
| アカウント | `yk`（uid 1000、sudo あり）で管理も運用も | 管理 `ykadm`（uid 1000、sudo）と運用 `yk`（uid 1001、sudo なし）に分けた（2.1） |

## 作業の記録

| 日付 | 内容 |
|---|---|
| 2026-11-19 | 記録を作った。ディスクの分け方（0 章）を決めた |
| 2026-11-20 | OS を入れ（1 章）、`apt upgrade` まで済んだ。ssh でパスワードで入れる |
| 2026-11-20 | 更新の後は再起動する決まり（2.0）。アカウントを `ykadm`・`yk` に分け、一時置き場の LV を作った（2.1） |
| 2026-11-20 | 起動が wait-online で長く待たされたので、`enp2s0` を `optional`、`wlp6s0` を networkd の管理から外した（NIC の割り当ての節） |
| 2026-11-20 | ZFS の RAIDZ2（`tank`、`/srv/download`）を作った（2.2）。NIC は `enp2s0` が 10 G、`enp7s0` が 2.5 G |
| 2026-11-20 | `yk` に v0.3.2 を clone し、`pytest` で 1 件だけ落ちた（試験の不備。3 章） |
