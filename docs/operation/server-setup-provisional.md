# 【仮機】ストレージ・制御サーバーのセットアップの記録

> **これは動作確認用の仮機の記録です。本番機の手順ではありません。**
> 本番機は 2026-11 中旬に届く予定で、そのセットアップは別の文書（`server-setup.md`）に残す。
> 本番機と違うところは、末尾の「本番機との違い」に書く。

- 記録を始めた日: 2026-11-09
- 作業者: 西村
- 状態: 作業中

## 仮機の構成

| 項目 | 値 |
|---|---|
| 機種 | （記入） |
| CPU・メモリ | （記入） |
| ディスク | SATA SSD 500 GB × 1（RAID なし） |
| NIC | （記入） |
| ホスト名 | （記入。仮機とわかる名前にする。例: `sam45compat-tmp`） |
| IP | 所内 LAN、固定（値を記入: アドレス / ゲートウェイ / DNS） |
| OS | Ubuntu Server 24.04 LTS（ISO の版を記入: `ubuntu-24.04.x-live-server-amd64.iso`） |

## 1. インストール用の USB を作る

1. <https://ubuntu.com/download/server> から 24.04 LTS の最新のポイントリリースの ISO を落とす。
2. 同じ場所の `SHA256SUMS` と照合する: `shasum -a 256 ubuntu-24.04.x-live-server-amd64.iso`
3. USB に書く（Mac の例。`N` は `diskutil list` で確かめる。間違えると手元のディスクを消す）:

   ```sh
   diskutil list
   diskutil unmountDisk /dev/diskN
   sudo dd if=ubuntu-24.04.x-live-server-amd64.iso of=/dev/rdiskN bs=4m status=progress
   diskutil eject /dev/diskN
   ```

## 2. BIOS / UEFI

- 起動は UEFI。Secure Boot は有効のままでよい
- 時計を合わせておく（UTC か JST かを記録: （記入））
- USB から起動する

## 3. インストーラー

| 画面 | 選んだもの | メモ |
|---|---|---|
| Language | English | コンソールの文字化けを避ける |
| Keyboard | Japanese | （手元のキーボードに合わせる） |
| Type of install | Ubuntu Server（minimized ではない） | |
| Network | DHCP | インストール後に固定 IP にした（4.1） |
| Proxy | （所内で要るなら記入） | |
| Mirror | 既定（`jp.archive.ubuntu.com` になっていることを確認） | |
| Storage | Use an entire disk ＋ LVM | 既定では `ubuntu-lv` に VG の一部（100 GB 程度）しか割り当てないので、確認画面で `ubuntu-lv` を編集して最大にする |
| Profile | ユーザー名・ホスト名を記入 | |
| Ubuntu Pro | Skip | |
| SSH | Install OpenSSH server にチェック | GitHub から公開鍵を取り込める |
| Featured snaps | 何も選ばない | |

## 4. インストール後

```sh
sudo apt update && sudo apt full-upgrade -y
sudo timedatectl set-timezone Asia/Tokyo
sudo apt install -y git python3-venv python3-pip build-essential chrony nfs-common
```

- 時刻同期: chrony。所内の NTP サーバーを使うなら `/etc/chrony/chrony.conf` に書く（記入）。確認は `chronyc tracking`
- SSH: 公開鍵で入れることを確かめてから、`/etc/ssh/sshd_config.d/` で `PasswordAuthentication no` にする
- 自動更新: unattended-upgrades は入ったまま。仮機なので自動再起動の設定は既定のまま（本番機では観測中に再起動しないよう決める）

### 4.1 固定 IP

仮機では、インストーラーが `/etc/netplan/00-installer-config.yaml`（DHCP）を書いていた（`50-cloud-init.yaml` は無かった）。
元のファイルを残してから、固定の設定に置き換える。

```sh
ip -br link                       # インターフェース名を確かめる（記入: ）
cat /etc/netplan/00-installer-config.yaml
ls /etc/cloud/cloud.cfg.d/ 2>/dev/null   # 仮機: 00-subiquity-disable-cloudinit-networking.cfg と 99-disable-network-config.cfg があり、止まっていた
sudo mv /etc/netplan/00-installer-config.yaml /root/00-installer-config.yaml.orig
sudoedit /etc/netplan/01-static.yaml
sudo chmod 600 /etc/netplan/01-static.yaml
sudo netplan try                  # 120 s 以内に Enter を押さないと元に戻る（SSH 越しでも安全）
```

- cloud-init があり、ネットワークの設定を止めるファイル（`99-disable-network-config.cfg` など）が無いときだけ、
  `echo 'network: {config: disabled}' | sudo tee /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg` で止める

`/etc/netplan/01-static.yaml`（値は記入）:

```yaml
network:
  version: 2
  ethernets:
    <所内 LAN の IF>:
      dhcp4: false
      addresses: [<アドレス>/<プレフィックス長>]
      routes:
        - to: default
          via: <ゲートウェイ>
      nameservers:
        addresses: [<DNS 1>, <DNS 2>]
    # RFSoC を別の NIC につなぐときは、ゲートウェイなしで足す
    # <RFSoC 側の IF>:
    #   dhcp4: false
    #   addresses: [<アドレス>/<プレフィックス長>]
```

確認: `ip -br addr`・`ip route`・`resolvectl status`・`ping -c 3 archive.ubuntu.com`

### 4.2 時刻（観測所 NTP → このサーバー → RFSoC 4x2）

このサーバーは観測所の NTP サーバーだけから時刻をとり、RFSoC のいるネットワークに配る。

1. `/etc/chrony/chrony.conf` の `pool ...` の行をコメントにする（外の NTP を使わない）
2. `/etc/chrony/sources.d/observatory.sources`:

   ```
   server <観測所 NTP サーバー> iburst prefer
   ```

3. `/etc/chrony/conf.d/serve.conf`:

   ```
   allow <RFSoC のネットワーク>/<プレフィックス長>
   ```

   - `local stratum 10` は入れていない。観測所 NTP に届かないときに、ずれた時刻を正しいものとして配らないため（記入: 入れたか）

4. `sudo systemctl restart chrony`
5. 確認: `chronyc sources -v`（観測所 NTP に `^*`）・`chronyc tracking`（Stratum・オフセット）・`chronyc clients`（RFSoC から来ているか）
6. ufw を有効にしている場合だけ: `sudo ufw allow from <RFSoC のネットワーク>/<プレフィックス長> to any port 123 proto udp`

RFSoC 側は、このサーバーだけを NTP サーバーに指定する（使っている時刻同期のデーモンを確かめて記入: ）。
NTP が合わせるのは時計の秒まで（ms 程度）。積分の区間の境界は 1PPS で合わせる。

## 5. sam45compat を動かす

submodule の URL が `git@github.com:` なので、仮機から GitHub に SSH で入れる鍵（個人の鍵か deploy key）が要る。

```sh
git clone --recurse-submodules https://github.com/nobeyama-radio-observatory/YATSUGATAKE-NRO45-sam45compat.git
cd YATSUGATAKE-NRO45-sam45compat
python3 -m venv .venv          # 24.04 はシステムの python に pip で入れられない（PEP 668）
. .venv/bin/activate
pip install -e . pytest
pytest
```

- 仮機でつまずいたこと:
  - submodule が取れておらず、`s45proto` の import で止まった（鍵が無く SSH で取れなかった）。取り方を記入: （鍵 / HTTPS）
  - `pytest` が `external/YATSUGATAKE` の中の `test_nch.py`（単体で実行するスクリプト。最後に `sys.exit`）まで集めて止まった。
    `pyproject.toml` に `testpaths = ["tests"]` を足して直した。直す前は `pytest tests` で回避できる
- Python の版: 3.14.4（Ubuntu 24.04 の標準は 3.12。OS の版を `lsb_release -a` で確かめて記入: ）
- 試験の結果: `pytest` で全部通過（2026-11-09）

### 5.1 起動時に bklog をマウントし、暫定 LC を起こす

bklog（`<bklog のサーバー>:/mstorage/cosmos3/45m/bklog`。サーバーの IP は担当者に聞く）は `@gh_nfs` に許された計算機だけがマウントできる（仮機は許可済み）。
読み取り専用でマウントする。`/etc/fstab` に 1 行:

```
<bklog のサーバー>:/mstorage/cosmos3/45m/bklog  /cosmos3/45m/bklog  nfs  ro,soft,timeo=50,retrans=3,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=30  0 0
```

- `_netdev`: ネットワークが上がってからマウントする。`nofail`: ストレージが止まっていても起動を止めない
- `x-systemd.automount`: 最初に読まれたときにマウントする。起動時に失敗しても、次に読まれたときにやり直す
- 確かめる: `sudo systemctl daemon-reload && sudo mount -a && ls /cosmos3/45m/bklog/sam45psw | tail -3`

暫定 LC は systemd のサービスにする（[`tools/systemd/sam45compat-lc.service`](../../tools/systemd/sam45compat-lc.service)。利用者 yk、`.venv` の python、落ちたら 10 s 後に起こし直す）:

```sh
sudo cp tools/systemd/sam45compat-lc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sam45compat-lc
systemctl status sam45compat-lc
journalctl -u sam45compat-lc -f        # ログ（「置き場所を読めた」「新しいロギング」など）
```

- 起動した時点（マウントが後なら、置き場所を初めて読めた時点）に既にあるロギングは扱わない
- **観測の途中で起こし直すと、その観測は扱わない**（YKSP のロギングは途中で切れたまま残る）。
  `git pull` で更新したら、観測の合間に `sudo systemctl restart sam45compat-lc`

## 6. まだやらないこと

- **COSMOS のネットワークへの接続と、SAM45 のロギングのマウント**は、SAM45 の運用に影響しうるので、
  つなぎ方を決めて確認をとってから行う（[`plan.md`](../plan.md) の 1 章・6 章）
- RFSoC とのネットワーク

## 本番機との違い

| 項目 | 仮機 | 本番機（予定） |
|---|---|---|
| ディスク | SSD 500 GB × 1、RAID なし | （本番機の構成を記入） |
| IP | 所内 LAN に固定（仮の値） | 固定（記入） |
| ホスト名 | 仮機とわかる名前 | （記入） |

## 作業の記録

| 日付 | 内容 |
|---|---|
| 2026-11-09 | 記録を作った |
| 2026-11-09 | 手順 4 まで済んだ。固定 IP と NTP の手順を足した（4.1・4.2） |
| 2026-11-09 | 手順 5: submodule を取り、`testpaths` を足して試験が通った |
| 2026-11-10 | bklog をマウントし、暫定 LC（`--fake`）で sam45psw の観測 2 つに反応することを確かめた。起動時の自動のマウントと起動（5.1）を足した |
