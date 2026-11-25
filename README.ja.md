# oracle-snatch

日本語 | [English](README.en.md)

OCI公式Python SDKを使い、VM.Standard.A1.Flexインスタンスを空きが出るまで順次リクエストするツールです。無料枠を確認できない場合や、設定した無料枠を超える場合は作成せずに停止します。OCIリソースを削除・変更・停止する機能はありません。

利用にはOCIアカウント、APIキー、既存のVCNとサブネットが必要です。VCNやSSH鍵は作成しません。

## 無料枠と課金上の制約

Always Freeのコンピュート資源はテナンシーのホームリージョンで使用します。無料アカウント向けAlways Free資料はA1を月1,500 OCPU時間・9,000 GB時間（常時稼働換算で合計2 OCPU・12 GB）と説明しています。PAYGアカウントでは、Ampereの資料に月3,000 OCPU時間・18,000 GB時間（4 OCPU・24 GB相当）とあります。一方、Always Free資源一覧には「すべてのテナンシーで1,500/9,000」とも記載があり、Oracleの資料間で表現が一致していません。

そのため既定の `ACCOUNT_TYPE=FREE` は安全側の2 OCPU/12 GBです。PAYGの4/24枠を使うには `ACCOUNT_TYPE=PAYG` に加えて、Consoleで無料枠を確認したうえで `PAYG_FREE_LIMITS_CONFIRMED=YES` が必要です。PAYGへ変更する前に、Oracle Consoleのテナンシー、使用量、課金見込みを確認してください。アカウント種別はOCI APIから確実に判定できないため、自動推定しません。

AMDの `VM.Standard.E2.1.Micro` は最大2台の別枠です。E2 Microの台数やCPU/メモリはA1のCPU/メモリ枠に加算しません。ただし、その起動ボリュームはストレージ枠に含めます。現在E2 Microを2台運用している場合も、A1を追加するCPU枠とは別に、2台の起動ボリュームと既存ブロックボリュームを合算します。

起動ボリュームとブロックボリュームにはホームリージョンで合計200 GBの共有Always Free枠があります。既定の起動ボリュームは50 GBです。ツールは全コンパートメントのA1/E2インスタンス、起動ボリューム、ブロックボリュームを列挙し、今回の起動ボリュームを加えて判定します。既存A1は停止状態も含め、終了済み以外を保守的に計上します。

OCI APIの一覧や形状割当て情報だけでは請求額を完全には保証できません。OCIの課金集計、無料枠の適用状況、APIから参照できないリソース、ほかのリージョンやサービスの利用が別に存在します。ホームリージョン以外で作成したリソースは無料枠対象外となる場合があります。このツールはホームリージョン以外へ作成しませんが、他リージョンの既存リソースやアカウント全体の請求はOracle Consoleの「Billing & Cost Management」「Cost Analysis」、予算・通知でも確認してください。

このツールは現在のインスタンス構成から継続稼働時の枠を保守的に判定します。当月に終了・削除したインスタンスの累積OCPU時間・メモリ時間はリソース一覧から復元できないため、当月の使用済み量と無料枠の残量はOracle Consoleでも確認してください。

## OCI APIキーの準備

1. OCI Consoleでユーザーのプロフィールを開き、APIキーを追加します。
2. 公開鍵をOCIに登録します。OCIの `config` ファイルにはユーザーOCID、テナンシーOCID、フィンガープリント、リージョン、秘密鍵ファイルのパスを設定します。
3. API秘密鍵と `config` はGitに追加せず、Dockerでは読み取り専用でマウントします。
4. `key_file` はコンテナ内のパスを指定します。Compose例のまま使う場合は、例えば `/run/oci/oci_api_key.pem` です。

### IAMポリシー

専用ユーザーと専用グループを作り、下記をポリシーの出発点にしてください。`<group>`、`<target-compartment>`、`<network-compartment>` は実環境の名前に置き換えます。リソースを作るコンパートメントだけでなく、全テナンシーの在庫を読み取る権限も必要です。

```text
Allow group <group> to inspect compartments in tenancy
Allow group <group> to inspect tenancies in tenancy
Allow group <group> to read instances in tenancy
Allow group <group> to inspect vnic-attachments in tenancy
Allow group <group> to inspect vnics in tenancy
Allow group <group> to inspect volume-family in tenancy
Allow group <group> to read instance-images in tenancy
Allow group <group> to manage instance-family in compartment <target-compartment>
Allow group <group> to use virtual-network-family in compartment <network-compartment>
```

`manage instance-family` はインスタンスの作成に必要ですが、ポリシー上は作成以外の操作権限も含みます。このツール自身は既存リソースを変更・削除・停止しません。より厳しい権限にしたい場合は、Oracleのコアサービス権限表を確認し、利用するイメージやサブネットに合わせて個別権限を調整してください。全コンパートメントを一覧できない、必要なボリュームやインスタンスを読めない場合、ツールは安全確認に失敗して作成を停止します。

## SUBNET_IDなどの取得

- `SUBNET_ID`: OCI Consoleで対象ホームリージョンを選び、Networking → Virtual Cloud Networks → 対象VCN → Subnetsから取得します。パブリックIPが必要ならパブリックサブネットを選びます。
- `COMPARTMENT_ID`: 作成先コンパートメントのOCIDです。省略するとテナンシーのルートコンパートメントを使います。
- `IMAGE_ID`: 省略時は指定コンパートメントから最新のCanonical Ubuntuイメージを探します。形状に対応した別イメージを使う場合のみ指定します。
- OCIリージョン: API設定のリージョンはテナンシーのホームリージョンと一致させます。別リージョンなら停止します。

## Docker Composeでの導入

```bash
git clone https://github.com/nanosize/oracle-snatch.git
cd oracle-snatch
cp .env.example .env
mkdir -p secrets/oci data
```

`.env` の `SUBNET_ID` を設定し、OCIの `config` と秘密鍵を `secrets/oci/` に置きます。OCI `config` 内の `key_file` は `/run/oci/<秘密鍵ファイル名>` に合わせてください。SSHの公開鍵だけを `secrets/ssh_authorized_keys.pub` に置きます。SSH秘密鍵はマウントしません。

Debianホストでは、マウントする設定ファイルとデータディレクトリの所有者に合わせて `.env` の `PUID` と `PGID` を設定します（`id -u`、`id -g` で確認）。OCI設定とSSH公開鍵はコンテナ内で読み取り専用です。`data/` だけが状態・結果の保存用に書き込み可能です。

```bash
docker compose build
docker compose run --rm oracle-snatch --dry-run
docker compose up -d
docker compose logs -f oracle-snatch
```

取得が成功すると結果を `data/snatched.json` に保存し、コンテナは正常終了します。Composeの再起動ポリシーは `no` です。恒久的な設定エラーで無限再起動しません。ホスト再起動後に再開する場合は、Portainerまたは `docker compose up -d` で起動します。状態ファイルを使って未確定の作成要求を調査してから再開します。

amd64とarm64の両方に対応するPython slimベースイメージです。マルチプラットフォーム用イメージを作る場合はBuildxを使います。

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t oracle-snatch:latest .
```

## Portainer Stacks

1. StacksからGit repositoryを指定して `nanosize/oracle-snatch` を登録します。
2. Composeファイルに `compose.yaml` を指定します。
3. Stackの環境変数に `.env.example` の設定値を入力します。Portainerで環境変数を保存する場合、Webhook URLやTelegramトークンは機密値として扱ってください。
4. Portainerホスト上にOCI設定ディレクトリ、SSH公開鍵、永続化するデータディレクトリを用意し、`OCI_CONFIG_DIR`、`SSH_PUBLIC_KEY_HOST_PATH`、`DATA_DIR` にその絶対パスを設定します。
5. `PUID`/`PGID` をファイル所有者に合わせてデプロイします。最初はStackのコンソールまたは一時的なコマンドで `--dry-run` を実行し、確認後に通常実行します。

Composeファイルにはポート公開設定がなく、CPUは0.5コア、メモリは256 MBに制限されています。Docker JSONログは10 MB×3ファイルでローテーションします。

## 設定項目

| 項目 | 必須 | 既定値 | 説明 |
|---|---:|---:|---|
| `SUBNET_ID` | はい | — | ホームリージョンのサブネットOCID |
| `SSH_PUBLIC_KEY_FILE` | はい | Composeでは `/run/secrets/ssh_authorized_keys.pub` | SSH公開鍵ファイル。秘密鍵は不可 |
| `ACCOUNT_TYPE` | いいえ | `FREE` | `FREE` は2 OCPU/12 GB、`PAYG` は4/24 |
| `PAYG_FREE_LIMITS_CONFIRMED` | PAYG時 | — | 無料枠確認後に `YES`。未確認ならPAYG設定を拒否 |
| `OCPUS` | いいえ | `2` | 作成するA1のOCPU。1〜80の整数 |
| `MEMORY_GB` | いいえ | `12` | メモリGB。1 OCPUあたり最大64 GB、アカウント枠内に制限 |
| `BOOT_VOLUME_GB` | いいえ | `50` | 起動ボリューム容量。50〜32,768 GB |
| `COMPARTMENT_ID` | いいえ | テナンシーOCID | 作成先。使用量確認は常に全コンパートメント |
| `IMAGE_ID` | いいえ | 自動選択 | OCIイメージOCID |
| `DISPLAY_NAME` | いいえ | `snatch-arm` | インスタンス名 |
| `INTERVAL_SECONDS` | いいえ | `120` | すべてのADを試した後の待機秒数。120秒未満は不可 |
| `OCI_CONFIG_FILE` | いいえ | `~/.oci/config` | OCI SDK設定ファイル。Composeでは `/run/oci/config` |
| `OCI_PROFILE` | いいえ | `DEFAULT` | OCI設定プロファイル |
| `STATE_FILE` | いいえ | `/data/state.json` | 作成状態、リトライトークン、インスタンスOCIDの永続化先 |
| `RESULT_FILE` | いいえ | `/data/snatched.json` | 取得結果の保存先 |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | いいえ | 無効 | Telegram通知。両方設定 |
| `DISCORD_WEBHOOK_URL` | いいえ | 無効 | Discord Webhook通知 |

## ドライラン、状態、バージョン

ドライランは認証、ホームリージョン、サブネット、イメージ、全コンパートメントのインスタンス・ストレージ量、今回の構成を読み取り確認します。作成APIやOCIリソース変更は呼び出しません。

```bash
python3 snatch.py --dry-run
python3 snatch.py --status
python3 snatch.py --version
```

Docker Composeでは `docker compose run --rm oracle-snatch --dry-run` と `--status` を使います。`--status` はローカルの状態ファイルだけを読み、OCI APIには接続しません。

## 作成状態と再試行

- 作成要求ごとに `opc_retry_token`、要求内容、AD、操作タグをJSONへ原子的に保存してからOCIを呼びます。SDK内の自動リトライは無効にし、アプリ側で要求の状態を管理します。
- OCI応答に成功した直後、他の処理より先にインスタンスOCIDを状態ファイルへ保存します。
- 通信断などで結果が不明なら、再開時に全コンパートメントから操作タグを検索し、見つからなければ同じ要求内容と同じリトライトークンを再使用します。別のトークンで二重作成しません。
- OCIのリトライトークンは最大24時間で失効します。結果不明のまま23時間を超えた要求は、新規要求に置き換えず停止します。OCI Consoleでタグ `oracle-snatch-operation` を確認し、必要に応じて状態を人手で解決してください。
- 成功後はインスタンスがRUNNINGになるのを待ち、結果と通知を保存して終了します。同じ状態ファイルで再起動しても次のインスタンスは作りません。
- 状態ディレクトリにプロセスロックを置き、同時に複数プロセスが動くのを防ぎます。SIGTERM/SIGINTでは要求状態を保存したまま終了します。

在庫不足では通知しません。HTTP 429は `Retry-After` を尊重し、120秒を初期値とする指数バックオフとジッターを使用します。429や一時通信障害は再試行します。クォータ超過、認証/権限エラー、設定エラー、解決できない要求結果は通知して停止します。通知障害は取得処理を再実行しません。リクエストは直列で、429回避のための並列化や頻繁なポーリングは行いません。

## ログとトラブルシューティング

```bash
docker compose logs -f oracle-snatch
docker compose run --rm oracle-snatch --status
```

- `SUBNET_ID` / `COMPARTMENT_ID` が見つからない: OCIDとリージョン、OCIプロファイルを確認します。
- ホームリージョンエラー: OCI設定のリージョンをテナンシーのホームリージョンに合わせます。
- クォータ/無料枠で停止: Oracle ConsoleでA1使用量、全ボリューム容量、既存コンパートメントの資源を調べます。
- 認証・権限エラー: API鍵フィンガープリント、秘密鍵パス、グループ所属、上記IAMポリシーを確認します。
- コンテナでPermission denied: `PUID`/`PGID` とOCI設定・公開鍵・dataディレクトリの所有者/権限を合わせます。
- `requesting` のまま終了: 状態ファイルを保持し、`--status` とOCI Consoleの操作タグを確認します。状態JSONを削除して再実行すると、以前の要求が未確定のまま新しい要求を出す危険があります。

認証情報やWebhook URLはログに出しません。状態・結果JSONにはインスタンス情報とSSH公開鍵が含まれるため、リポジトリへ登録せずアクセス権を制限してください。

## テストとライセンス

```bash
python3 -m pip install -r requirements-dev.txt
ruff check .
pytest -q
```

テストのOCI APIはすべてモック化されており、実際のOCIリソースは作りません。GitHub Actionsでもruffとpytestを実行します。ライセンスと著作権表示は元リポジトリのMIT Licenseを維持します。

## 公式資料

- [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Oracle Cloud Infrastructure Free Tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm)
- [Arm-Based Compute](https://docs.oracle.com/en-us/iaas/Content/Compute/References/arm.htm)
- [Creating an Instance](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/launchinginstance.htm)
- [Python SDK ComputeClient](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/client/oci.core.ComputeClient.html)
- [OCI Core Services IAM Policy Reference](https://docs.oracle.com/en-us/iaas/Content/Identity/Reference/corepolicyreference.htm)
- [OCI IAM Policy Syntax](https://docs.oracle.com/en-us/iaas/Content/Identity/Concepts/policysyntax.htm)
