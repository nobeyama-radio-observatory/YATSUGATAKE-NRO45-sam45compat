# 【本番機】ストレージ・制御サーバーのセットアップ

仮機の記録は [`server-setup-provisional.md`](server-setup-provisional.md)。ここでは本番機の手順と、仮機との違いを書く。
仮機と同じ手順は、仮機の記録の節を指す。

- 記録を始めた日: 2026-11-19
- 作業者: 西村
- 状態: 運用中（2026-11-20 の 14:15 から仮機と入れ替えた）。残り: 下の「残っていること」

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
- 2026-11-20: v0.3.3 を入れ、109 passed・14 skipped（全部通った）
- 本番機の `yk` には `git config user.name` を設定していない（`yk` では commit しない）

### 3.1 時刻（仮機の記録の 4.2）

観測所の NTP サーバーは**2 台**指定する（仮機の実際の設定と同じ。仮機の記録には 1 台しか書いていなかった。2026-11-20）。
`/etc/chrony/chrony.conf` の `pool` の行はコメントにする。

```
# /etc/chrony/sources.d/observatory.sources（IP は仮機のものを写す）
server <観測所 NTP サーバー 1> iburst prefer
server <観測所 NTP サーバー 2>
```

- **外の NTP を止める**: Ubuntu 26.04 は `chrony.conf` の `pool` とは別に、`/etc/chrony/sources.d/ubuntu-ntp-pools.sources` で Canonical の NTS のサーバー（`ntp-nts-*.canonical.com`）を使う。
  `pool` の行だけ止めても、こちらが残っていた（2026-11-20）。行をコメントにする（ファイルは消さない）:

  ```sh
  sudo cp -a /etc/chrony/sources.d/ubuntu-ntp-pools.sources /root/ubuntu-ntp-pools.sources.orig
  sudo sed -i -E 's/^(pool|server)/#\1/' /etc/chrony/sources.d/ubuntu-ntp-pools.sources
  grep -rnE '^(pool|server)' /etc/chrony/      # observatory.sources の 2 行だけ
  sudo systemctl restart chrony
  ```

- 2026-11-20: 外の NTP を止めた。観測所の NTP サーバー 2 台は `Reach 0`（返事なし）。本番の置き場所に据えるとつながる（それまでは時刻が合わなくてよい）。据えた後に `chronyc sources` で `^*` を確かめる
- RFSoC に配る設定（`conf.d/serve.conf` の `allow`）は、ボード用の NIC を決めてから入れる

### 3.2 needrestart・暫定 LC（ダミー）・状態のページの置き場所（仮機の記録の 5.1・5.4 の 1）

仮機の記録と同じ。`ykadm` は `/home/yk` の中に `cd` できないので、`sudo cp /home/yk/YATSUGATAKE-NRO45-sam45compat/tools/systemd/sam45compat-lc.service /etc/systemd/system/` のようにパスを付ける。

- 2026-11-20: サービスの既定（`--fake`、`config/bklog-dummy.json`）で起こした。版は sam45compat v0.3.3・YATSUGATAKE v1.4.0-2-g13b5ae5。
  bklog をマウントしていないので「置き場所を読めない」が出る（入れ替えでマウントするまでこのまま）。`status.json`・`ql.json` は書けている

### 3.3 Apache（仮機の記録の 5.2・5.4）

証明書: 仮機も `ssl-cert` の自己署名（snakeoil。`/etc/ssl/certs/ssl-cert-snakeoil.pem`・`/etc/ssl/private/ssl-cert-snakeoil.key`）だった。
本番機の snakeoil は入れたときのホスト名（`yk-new`）で作られるので、入れ替えのときに仮機の 2 つのファイルを写す（利用者がブラウザで許可した証明書をそのまま使うため。2026-11-20）。

```sh
sudo apt install -y apache2 ldap-utils ssl-cert
sudo a2dissite 000-default
sudo a2enmod ldap authnz_ldap ssl ratelimit headers rewrite deflate
sudo a2ensite default-ssl
sudo cp /home/yk/YATSUGATAKE-NRO45-sam45compat/tools/apache/yksp-status.conf /etc/apache2/conf-available/
sudo a2enconf yksp-status
sudo apache2ctl configtest && sudo systemctl restart apache2
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost/status/      # 200
```

- 2026-11-20: 状態のページが 200。`AH00558`（ServerName が無い）は入れ替えのときに `ServerName` を書いて消す
- 仮機では、配信（LDAP）の設定は `sites-` でなく `conf-enabled/download.conf` にある。中身は [`tools/apache/yksp-download.conf`](../../tools/apache/yksp-download.conf) の `<...>` を埋めたものと同じだった（2026-11-20 に比べた）。
  本番機も同じ名前（`conf-available/download.conf`）で入れる。`AuthLDAPURL` の値は仮機のものを写す（リポジトリには書かない）
- 仮機の `ports.conf`: 80 番は名前（2 つの NIC の名前）で、443 番は全部の NIC で待ち受けていた。本番機の 80 番は入れ替えのときに決める（名前で待つと、名前を引けないときに reload が落ちる。仮機の記録の 4.3）
- `/srv/download` は `yk` の持ち物・755 にする（仮機は root・777 だった。書くのは暫定 LC と、`all_squash` で `yk` になる NFS だけ）

## 4. 仮機から入れ替える

### 4.1 固定 IP とホスト名（2026-11-20）

仮機を止めてから行う（同じ IP を使うため）。ssh 越しだと、IP が変わったところで切れて `netplan try` を確かめられないので、**コンソールで**行う。

`/etc/netplan/00-installer-config.yaml` を `/root/` に退避し、`/etc/netplan/01-static.yaml`（`chmod 600`）に置き換える（値は記入しない。仮機と同じ）:

```yaml
network:
  version: 2
  ethernets:
    enp7s0:                       # オンボード 2.5 G、yk-c
      dhcp4: false
      dhcp6: false
      accept-ra: false
      addresses: [<yk-c の IP>/24]
      nameservers:                # yk-c のセグメントの DNS も書く（USB の口が抜けても名前を引ける。仮機と同じ）
        addresses: [<yk-c 側の DNS 1>, <yk-c 側の DNS 2>]
    enp2s0:                       # PCIe 10 G、ボード用
      dhcp4: false
      dhcp6: false
      accept-ra: false
      addresses: [<ボード用の IP>/24]
      optional: true
    enx3495db2cd6c6:              # USB 1 G、yk-x。デフォルトゲートウェイはこちら
      dhcp4: false
      dhcp6: false
      accept-ra: false
      addresses: [<yk-x の IP>/24]
      routes:
        - to: default
          via: <yk-x のセグメントのゲートウェイ>
      nameservers:
        addresses: [<DNS 1>, <DNS 2>]
        search: [<ドメイン>]
      # optional を付けない（下の「起動で wait-online が 2 分待つ」）
```

**起動で wait-online が 2 分待つ（2026-11-20）**: netplan 1.2 は `optional: true` を `.network` の `RequiredForOnline=` でなく、
wait-online の drop-in（`/run/systemd/generator.late/systemd-networkd-wait-online.service.d/10-netplan.conf`）で表す。`optional` でない口について:

```
ExecStart=/lib/systemd/systemd-networkd-wait-online -i enp7s0:degraded
ExecStart=/lib/systemd/systemd-networkd-wait-online --any --dns -o routable -i enp7s0
```

2 行目は「`optional` でない口のどれかで、デフォルトの経路と DNS が使える」まで待つ。
初めは USB の口（ゲートウェイと DNS がある）を `optional` にしたので、`enp7s0`（ゲートウェイも DNS も無い）だけで満たそうとして、毎回 2 分の時間切れになった。
USB の口から `optional` を外すと 2 行とも USB の口が入ったが、それでも 2 分待った。`--dns` が、起動の直後（まだ名前を引いていない）に満たされないため
（起動の後に手で動かすと、名前を引いた後なので満たされ、原因が見えない）。

**直し方**: wait-online を上書きし、DNS は待たない（netplan の `10-netplan.conf` より後に読まれ、空の `ExecStart=` で netplan の 2 行を消す）。これで wait-online は ≒ 2 s になった（2026-11-20）:

```sh
sudo mkdir -p /etc/systemd/system/systemd-networkd-wait-online.service.d
sudo tee /etc/systemd/system/systemd-networkd-wait-online.service.d/override.conf <<'EOF'
# netplan の 10-netplan.conf の --dns は、起動直後（まだ名前を引いていない）に満たされず、毎回 2 分の時間切れになった（2026-11-20）。
# DNS は待たず、enp7s0 のリンクと、USB の口（デフォルトの経路と DNS がある）の経路だけを待つ。docs/operation/server-setup.md の 4.1
[Service]
ExecStart=
ExecStart=/usr/lib/systemd/systemd-networkd-wait-online -i enp7s0:degraded -i enx3495db2cd6c6:routable --timeout=60
EOF
sudo systemctl daemon-reload
systemctl cat systemd-networkd-wait-online | grep ExecStart    # 最後の 2 行が上の 2 行
```

- 待つのは最大 60 s。USB の口が抜けていても 60 s で先に進む。名前が引けるかは待たない（自分とマージの計算機の名前は `/etc/hosts` にある）
- **口の名前を変えたら（USB の NIC を差し替えたなど）、ここの `-i` も直す**。netplan の yaml を直しただけでは、この上書きは変わらない
- 何を待っているかを見るには、`[Service]` に `Environment=SYSTEMD_LOG_LEVEL=debug` を足して起こし直し、`journalctl -b -u systemd-networkd-wait-online`

- 確かめ方: `systemctl cat systemd-networkd-wait-online`（drop-in も出る）。手で `systemd-networkd-wait-online` を引数なしで動かしても drop-in は通らないので、原因は見えない
- `networkctl status` の `Required For Online` は、netplan 1.2 では `optional` でも `yes` のまま（wait-online の引数で決まるため）

- IPv6 は使わない（`accept-ra: false`）。DHCP の間は RA で IPv6 のアドレスをとっていた。apt が「Waiting for headers」で待たされた件（2.0）の疑いの一つでもある
- ホスト名: `sudo hostnamectl set-hostname yk-c`。`/etc/hosts` の `127.0.1.1 yk-new` の行を消し、自分の 2 つの名前を実際の IP で書く（仮機の記録の 4.3）
- `grep -rs manage_etc_hosts /etc/cloud/` は `templates/hosts.debian.tmpl` の説明の行だけ（設定は無い。`/etc/hosts` は書き直されない）
- 2026-11-20: 3 つの口とも仮機の IP になり、デフォルトゲートウェイは USB 側。DNS は DHCP でもらっていた 2 台を書いた。
  観測所の NTP 2 台に届いた（`^*`）。LDAP に届いた（`ldapsearch` で自分のアカウントが返った）

（記入: 手順を決める。観測のない時間に行う）

- 固定 IP・ホスト名を仮機のものにする（仮機の記録の 4.1。入れ替えの前に仮機を止めるか、仮機の IP を外す）
- SSH のホストの鍵（`/etc/ssh/ssh_host_*`）を仮機から写すと、マージの計算機などの `known_hosts` を直さずに済む
- 仮機の `/srv/download` と `/home/yk/bklog` に残っているものを写す
- `yk` の `~/.ssh`（ボードの鍵・マージの鍵）、`config/bklog.local.json`、Apache の設定と証明書

### 4.2 仮機から写す（2026-11-20）

仮機は止めて、`yk-x` 側の口だけを DHCP にして起こした（固定 IP は本番機が使っているため）。
**仮機から本番機の `yk` に送る**（仮機は `PasswordAuthentication no`）。写すための一時的な鍵（`~/.ssh/id_ed25519_move`）を仮機の `yk` に作り、本番機の `yk` の `authorized_keys` に入れた。
送り先は IP で書く（仮機の `/etc/hosts` では `yk-x`・`yk-c` が仮機自身を指す）。

```sh
# 仮機の yk で（tmux の中で）
N=yk@<yk-x の IP>; K="ssh -i ~/.ssh/id_ed25519_move"
rsync -aH -e "$K" --exclude tmp/ ~/bklog/ $N:bklog/              # tmp/ は 10.24 ms の一時ファイル（マージ済みなら要らない）
rsync -aH -e "$K" --exclude authorized_keys --exclude 'id_ed25519_move*' ~/.ssh/ $N:.ssh/
rsync -aH -e "$K" ~/YATSUGATAKE-NRO45-sam45compat/config/bklog.local.json $N:YATSUGATAKE-NRO45-sam45compat/config/
rsync -aH --info=progress2 -e "$K" /srv/download/ $N:/srv/download/
```

- 量（2026-11-20）: `/srv/download` ≒ 207 GB、`bklog`（`tmp/` を除く）≒ 2.2 GB（`tmp/` を含めると 26 GB）
- root の持ち物（ホストの鍵・snakeoil の証明書と鍵・`/etc` の設定）は仮機で `tar` にまとめて送り、本番機の `/root/yk-c-etc/` に展開して、1 つずつ見て入れた。
  `/root` の中は `ykadm` の `*` では展開されない（`sudo sh -c "cat .../*"` で見る）
- **ホストの鍵は、`/srv/download` の転送が終わってから**入れる（途中で変わると、転送のやり直しで仮機の ssh が止まる）
- 転送の途中で暫定 LC を実機の設定で起こしてよい（使う口と書く場所が違う。観測のファイル名には時刻が入り、`deliver` は同じ名前を上書きしない）
- 写し漏れの確かめ方: 仮機から `rsync -aHn --stats` で `Number of regular files transferred: 0`。
  `bklog` の版の記録（`sam45compat-versions.tsv`）は、本番機の LC が書き足すので違って出る。**写し直さない**（本番機の行が消える）
- 写し終えたら: 一時的な鍵を両方から消す。仮機で root・777 だったフォルダ（`rsync` で 777 のまま来る）を 755 にする
  （`sudo find /srv/download -maxdepth 2 -type d -perm -o+w -exec chmod 755 {} +`）。ホストの鍵を `/root/yk-c-etc/etc/ssh/` から写して `systemctl restart ssh`
- 最後に再起動して、wait-online が待たないこと、`sam45compat-lc`・`apache2`・`nfs-server`・`chrony`・`ssh` が起きること、bklog の automount・ZFS・NFS の公開・NTP を確かめた（2026-11-20）。
  本番機を起こし直しても、マージの計算機の NFS のマウント（`hard`）はつながり直した

仮機の設定から本番機に入れたもの:

| 項目 | 本番機 |
|---|---|
| bklog の NFS（`/etc/fstab`） | 仮機と同じ 1 行（サーバーは `zcos1c`。マージの計算機と同じ）。`sudo mkdir -p /cosmos3/45m/bklog` してから。本番機は仮機の `yk-c` の IP なので許されている |
| `/etc/hosts` | `zcos1c` の行を足した（仮機の記録の 4.3） |
| NFS の公開（`/etc/exports.d/yksp-merge.exports`） | 仮機と同じ。ただし `anonuid`・`anongid` は本番機の `yk`（1001）。`nfs-kernel-server` を入れ、`nfs-server.service.d/zfs.conf` に `RequiresMountsFor=/srv/download` |
| chrony | `conf.d/serve.conf` に `allow <ボードのセグメント>/24`（ボードに時刻を配る）。ボードは `systemd-timesyncd` で、本番機のボード用の IP をとりに来る（落ち着くと 34 分おき）。確かめ方: `sudo chronyc clients`（root でないと `501 Not authorised`）、ボードで `timedatectl show-timesync` |
| Apache | `download.conf`・`default-ssl.conf` は仮機と同じ（コメントだけ違う）。80 番は仮機の名前の `Listen` にせず、`Listen 80`（全部の口）のまま（名前を引けないときや USB の口が抜けたときにも Apache が起きるように） |
| 暫定 LC のサービス | 仮機は `sam45compat-lc.service.d/override.conf` で `ExecStart` を `--config config/bklog.local.json`（`--fake` なし）にしていた |

- 仮機の `/etc/chrony/sources.d/` には `ubuntu-ntp-pools.sources` が残っていた（行がコメントでなければ、仮機は外の NTP も使っていた）
- **マージの計算機の NFS のマウントが古くなる**: マージの計算機は仮機の `/srv/download` をマウントしたままで、本番機が同じ IP・同じパスで公開しても、
  中身は別のファイルシステム（ZFS）なので `Stale file handle` になった（2026-11-20）。マージの計算機でマウントを外して付け直す（COSMOS 側の操作なので、担当者と相談して行う）:

  ```sh
  # マージの計算機で（root）
  mount | grep yksp
  umount -l /cosmos3/45m/bklog/yksp
  mount -t nfs4 -o vers=4.2,hard,noatime yk-c:/srv/download /cosmos3/45m/bklog/yksp
  ls /cosmos3/45m/bklog/yksp | head -3
  touch /cosmos3/45m/bklog/yksp/.test && ls -ln /cosmos3/45m/bklog/yksp/.test && rm /cosmos3/45m/bklog/yksp/.test   # 持ち主が本番機の yk の uid
  ```

  2026-11-20 に付け直し、本番機の中身が見え、書くと uid 1001（本番機の `yk`）になった。
  **マージの計算機の `/etc/fstab` にも autofs にも `yksp` の行は無かった**（仮機の頃から手でマウントしていた）。マージの計算機を起こし直すとマウントが消え、マージが失敗する。
  仮機の記録の 5.3 の fstab の行（`nofail`・`x-systemd.automount` 付き）を足すかは、担当者と決める（記入: ）

- マージの ssh は、LC の設定（`merge_cmd`）と同じユーザー名・名前（FQDN）で試す。`known_hosts` は FQDN で入っているので、短い名前では `Host key verification failed` になる

### 4.3 残っていること

- マージの計算機の `/etc/fstab` に `yksp` の行を足すか（今は手でマウント。起こし直すと消える）。担当者と決める
- 仮機: 止めて、ディスクは消さずに予備・控えとして残す（`/srv/download` の写しがある）
- ZFS・SMART の異常を知らせる手段（ZED のメール・smartd）。運用者への通知の手段（[`../plan.md`](../plan.md) の 6 章）と一緒に決める
- 配信の速さの制限・優先度（仮機の記録の 5.2 の「まだ」）
- USB の NIC（ゲートウェイと DNS がある）が抜けると、外と名前を引くのが止まる（観測・マージ・NTP は `yk-c` 側なので止まらない）

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


決めた割り当て（2026-11-20。IP は仮機と同じ。値は文書に書かない）:

| NIC | 名前 | 速さ | つなぐ先 | ホスト名 |
|---|---|---|---|---|
| `enp7s0` | オンボード | 2.5 G | 所内 LAN の `yk-c` のセグメント（仮機の `yk-c` の IP） | `yk-c`（この計算機のホスト名） |
| `enp2s0` | PCIe | 10 G | ボード用（プライベート、RFSoC 4x2） | — |
| `enx3495db2cd6c6` | USB | 1 G | 所内 LAN の `yk-x` のセグメント（仮機の `yk-x` の IP） | `yk-x` |

- USB の NIC は名前が MAC から決まる（`enx` ＋ MAC）。差し替えると名前が変わる
- USB の NIC は、netplan に書く前から DHCP をとっていた（2026-11-20）。initrd（dracut）が毎回 `/run/systemd/network/zzzz-dracut-default.network`（ループバック以外の口を全部 DHCP）を作るため。netplan に書いた口は netplan の設定が先に当たる。**新しい口を挿すと、netplan に書くまで勝手に DHCP をとる**

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
| 2026-11-20 | 試験を直した v0.3.3 を入れ、`pytest` が全部通った（3 章） |
| 2026-11-20 | chrony を観測所の NTP 2 台だけにした（3.1）。今の置き場所からは届かない |
| 2026-11-20 | needrestart から LC を外し、暫定 LC をダミーで起こした（3.2） |
| 2026-11-20 | Apache を入れ、状態のページが本番機の上で開けた（3.3） |
| 2026-11-20 | 配信の設定（`download.conf`）を入れた。LDAP は今の置き場所から届かないので、配信の試験は据えた後。試験のフォルダを消して電源を切り、本番の置き場所へ移した |
| 2026-11-20 | 本番の置き場所で、固定 IP（3 つの口）とホスト名 `yk-c` にした（4.1）。NTP・LDAP に届いた |
| 2026-11-20 | 起動が wait-online で毎回 2 分待った。netplan の `--dns` が原因で、wait-online を上書きして直した（4.1） |
| 2026-11-20 | 仮機を DHCP で起こし、データ・鍵・設定を写し始めた（4.2）。bklog のマウント・`/etc/hosts`・NFS の公開・`yk-c` 側の DNS を入れた |
| 2026-11-20 | ボードが本番機から時刻をとることを確かめた。証明書（仮機の snakeoil）と `ServerName` を入れ、`/srv/download` の転送の途中で暫定 LC を実機の設定（`--config config/bklog.local.json`）で起こした（14:15） |
| 2026-11-20 | マージの ssh が通った。マージの計算機の NFS のマウントが古くなっていた（`Stale file handle`）ので付け直した（4.2） |
| 2026-11-20 | 本番機で最初の観測（14:34、OTF）が、ロギング・マージ・配信まで通った。写し漏れが無いことを確かめ、一時的な鍵を消し、777 のフォルダを直し、ホストの鍵を入れ、再起動して全部が起きることを確かめた（4.2） |
