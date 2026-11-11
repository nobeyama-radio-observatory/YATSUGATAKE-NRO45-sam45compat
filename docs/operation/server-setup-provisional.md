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
| OS | Ubuntu 26.04.1 LTS（`/etc/os-release` で確認。2026-11-10。記録を作ったときは 24.04 の予定だった） |

## 1. インストール用の USB を作る

1. <https://ubuntu.com/download/server> から LTS（仮機は 26.04）の最新のポイントリリースの ISO を落とす。
2. 同じ場所の `SHA256SUMS` と照合する: `shasum -a 256 ubuntu-<版>-live-server-amd64.iso`
3. USB に書く（Mac の例。`N` は `diskutil list` で確かめる。間違えると手元のディスクを消す）:

   ```sh
   diskutil list
   diskutil unmountDisk /dev/diskN
   sudo dd if=ubuntu-<版>-live-server-amd64.iso of=/dev/rdiskN bs=4m status=progress
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
python3 -m venv .venv          # Ubuntu はシステムの python に pip で入れられない（PEP 668）
. .venv/bin/activate
pip install -e . pytest
pytest
```

- 仮機でつまずいたこと:
  - submodule が取れておらず、`s45proto` の import で止まった（鍵が無く SSH で取れなかった）。取り方を記入: （鍵 / HTTPS）
  - `pytest` が `external/YATSUGATAKE` の中の `test_nch.py`（単体で実行するスクリプト。最後に `sys.exit`）まで集めて止まった。
    `pyproject.toml` に `testpaths = ["tests"]` を足して直した。直す前は `pytest tests` で回避できる
- Python の版: 3.14.4（OS は Ubuntu 26.04.1 LTS）
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

### 5.2 観測生データ（YKSP）の配信（Apache）

方式は [`../design/data-delivery.md`](../design/data-delivery.md)。所内 LAN から HTTPS で、LDAP のアカウントでログインし、自分の `<group>` の下だけを見せる。

1. 入れる。設定ができるまでは、自分の計算機からだけ受ける:

   ```sh
   sudo apt update
   sudo apt install -y apache2 ldap-utils        # ldap-utils は ldapsearch で LDAP の接続を確かめるため
   sudo sed -i 's/^Listen 80$/Listen 127.0.0.1:80/' /etc/apache2/ports.conf
   sudo a2dissite 000-default                    # 既定のページを消す
   sudo a2enmod ldap authnz_ldap ssl ratelimit headers rewrite
   sudo systemctl restart apache2
   ```

2. LDAP の接続を確かめる。`AuthLDAPURL` の絞り込み（`<絞り込み>`）に、ログインさせたいアカウントが当てはまること:

   ```sh
   ldapsearch -x -H ldap://<LDAP のサーバー> -b "<base DN>" "(&(uid=<アカウント>)<絞り込み>)" uid
   ```

3. HTTPS のサイトを作る。証明書: （記入: 所内の CA か自己署名か、置き場所）
4. 待ち受けをダウンロード用の NIC の IP にする（`/etc/apache2/ports.conf` の `Listen <ダウンロード用の IP>:443`）。
   ファイアウォール: （記入: ufw の状態と、開けた口）
5. 書き換えを有効にし、[`tools/apache/yksp-download.conf`](../../tools/apache/yksp-download.conf) の中身を入れて `<...>` を埋める
   （入れた場所を記入: ）:

   ```sh
   sudo a2enmod rewrite
   sudo apache2ctl configtest && sudo systemctl reload apache2
   ```

6. 外の計算機から確かめる（2026-11-10 に全部期待どおり）。試験用の `<group>` を 2 つ作る:

   ```sh
   sudo mkdir -p /srv/download/<自分のアカウント>/proj1 /srv/download/zz-other/proj1
   echo mine  | sudo tee /srv/download/<自分のアカウント>/proj1/a.txt
   echo other | sudo tee /srv/download/zz-other/proj1/b.txt
   U=<自分のアカウント>; H=https://<サーバー>
   curl -sk -u $U -o /dev/null -w '%{http_code} %{redirect_url}\n' $H/download     # 301 → $H/download/（末尾の / を足すだけ）
   curl -sk -u $U -o /dev/null -w '%{http_code} %{redirect_url}\n' $H/download/    # 302 → $H/download/<自分>/
   curl -sk -u $U -o /dev/null -w '%{http_code}\n' $H/download/$U/                 # 200（自分の一覧）
   curl -sk -u $U -o /dev/null -w '%{http_code}\n' $H/download/$U/proj1/a.txt      # 200
   curl -sk -u $U -o /dev/null -w '%{http_code}\n' $H/download/zz-other/proj1/b.txt   # 403（他人）
   curl -sk -u $U -o /dev/null -w '%{http_code}\n' --path-as-is "$H/download/$U/../zz-other/proj1/b.txt"    # 403 か 404
   curl -sk -u $U -o /dev/null -w '%{http_code}\n' --path-as-is "$H/download/$U/%2e%2e/zz-other/proj1/b.txt" # 403 か 404
   curl -sk       -o /dev/null -w '%{http_code}\n' $H/download/                    # 401（ログインなし）
   wget -q --no-check-certificate --user=$U --ask-password -r -np -nH --cut-dirs=2 $H/download/$U/   # 丸ごと取れる
   ```

   確かめたら、試験用のフォルダ（特に `zz-other`）を消す。

- 頭（`/download`）を開くと、ログインの後に自分のフォルダへ転送する。転送の作りで踏んだ穴（2026-11-10）:
  - `<LocationMatch>` の中の `Redirect "/download/%{REMOTE_USER}/"` は、名前が空のまま評価され、`/download//` に飛んだ
  - `<VirtualHost>` の直下の `RewriteCond %{LA-U:REMOTE_USER}`（先にログインを確かめて名前を得る書き方）は、ログインが 401 になった
  - `<Directory>` の中の `RewriteRule` は、`FollowSymLinks` か `SymLinksIfOwnerMatch` が無いと 403（`AH00670`）。
    頭のフォルダだけ `SymLinksIfOwnerMatch` を許して通った。その後、`<group>` より下も同じ理由で 403 になったので、
    `<DirectoryMatch>` で `RewriteEngine Off` にした（書き換えの設定は下のフォルダにも引き継がれる）
- `SymLinksIfOwnerMatch` を頭で許すので、`/srv/download` の直下に持ち主の同じシンボリックリンクがあればたどる。
  `deliver.py` は本物のフォルダしか作らない。`<group>` より下はたどらない
- ログインできるのは、`AuthLDAPURL` の絞り込みに当てはまるアカウントだけ。観測者のアカウントが当てはまるかを確かめる
- `AuthLDAPURL` は `ldap://`（暗号化なし）。Apache と LDAP の間を観測者のパスワードが平文で流れる。LDAPS か StartTLS にできるかを LDAP の管理者に確かめる
- LDAP の大文字・小文字: LDAP は区別せずにログインを通すが、`<group>` との比較は区別する（`Foo` でログインすると `foo` に入れない。入れないだけで、他人のものは見えない）
- まだ: ダウンロードの速さの制限（`mod_ratelimit`）、Apache の優先度（`nice`・`ionice`）

### 5.3 マージの計算機に `/srv/download` を渡す（NFS と ssh。2026-11-11）

マージは別の計算機（以下「マージの計算機」）で走る（[`../design/data-delivery.md`](../design/data-delivery.md) の「マージ」）。
仮機は、マージの計算機と同じセグメント（管理者しか入れない）にも NIC を持つ。NFS と ssh はそちらを通す。

**NFS の公開（仮機）**: マージの計算機のセグメントにだけ、読み書きで公開する。書き込みは全部 yk として扱う（`all_squash`）。

```bash
S=<マージの計算機のセグメント（例: 192.168.x.0/24）>
U=$(id -u yk); G=$(id -g yk)

sudo apt install nfs-kernel-server
sudo mkdir -p /etc/exports.d            # 無いことがある。無いと tee が失敗し、何も公開されない
echo "/srv/download  $S(rw,sync,no_subtree_check,all_squash,anonuid=$U,anongid=$G)" \
  | sudo tee /etc/exports.d/yksp-merge.exports   # 名前は .exports で終わること
sudo exportfs -ra
sudo exportfs -v                        # 何も出なければ公開されていない
sudo ufw allow from $S to any port 2049 proto tcp
systemctl is-enabled nfs-server         # enabled なら再起動しても公開される
```

**マウント（マージの計算機）**: マウント先は `/cosmos3/45m/bklog/yksp`。`/etc/fstab`:

```
<仮機の管理者のセグメント側の名前か IP>:/srv/download  /cosmos3/45m/bklog/yksp  nfs4  vers=4.2,hard,noatime,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=30  0 0
```

書き込めて、持ち主が yk の uid になることを `touch` と `ls -ln` で確かめる。

**ssh の鍵（仮機の yk）**: 暫定 LC（`User=yk`）がパスワードなしで入れるよう、マージ専用の鍵を作る。

```bash
ssh-keygen -t ed25519 -N "" -C "yk@<仮機> sam45compat merge" -f ~/.ssh/id_ed25519_merge
ssh-copy-id -i ~/.ssh/id_ed25519_merge.pub <user>@<マージ計算機>    # 1 回目はホストの鍵を yes で受け入れる（known_hosts に残す）
ssh -i ~/.ssh/id_ed25519_merge -o BatchMode=yes <user>@<マージ計算機> 'hostname; ls /cosmos3/45m/bklog/yksp'
```

マージの計算機の `authorized_keys` では、この鍵の行の頭に `from="<仮機の管理者のセグメント側の IP>",restrict` を付ける。

**暫定 LC の設定**: `config/bklog.json` の `merge_cmd`・`merge_root` を写し、`<...>` を埋める。実際のユーザー名・ホスト名・スクリプトの場所は、
`config/*.local.json`（git に入らない）にだけ書く。

- まだ: `/srv/download` を別のディスク（ZFS）にしたら、`systemctl edit nfs-server` で `RequiresMountsFor=/srv/download` を足す（ストレージより先に空のフォルダを公開しない）

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
| 2026-11-10 | Apache を入れ、HTTPS・LDAP の Basic 認証・自分の `<group>` だけを通す設定（5.2）。外の計算機から、自分は見られ、他人と頭の一覧は 403 になることを確かめた |
| 2026-11-10 | 公開の URL を `/download`（`/srv/download`）にし、頭を開くと自分のフォルダへ転送するようにした（5.2）。OS が 26.04.1 だったので記録を直した |
| 2026-11-11 | マージの計算機に `/srv/download` を NFS で公開し、yk のマージ専用の ssh の鍵を登録した（5.3）。`authorized_keys` に `from=`・`restrict` を入れた。マウントして書き込めること、ssh で入れることを確かめた |
