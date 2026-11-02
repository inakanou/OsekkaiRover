# Brief: device-deployment

## Problem
開発者は、作ったものを実機の Raspberry Pi 5 に安全に入れたい。実機は 1 台で子どもが毎日使っているため、入れ替えで壊れたり、走行中に止まらなくなったりしてはいけない。サンプルには手作業の手順しかない。

## Current State
- サンプルは、実機の `~/works/OsekkaiRover`（git の作業コピー）から systemd のユーザーサービスで直接動いている
- 依存関係・声のファイル・顔のモデルの準備は README の手作業
- 実機は家の LAN にあり、GitHub の CI からは届かない。リポジトリは公開なので、セルフホストランナーは使わない
- `git-workflow.md` で、実機に入れるのはタグの版だけ、試したら最新のタグに戻す、と決めてある

## Desired Outcome
- 開発 PC から 1 つのコマンドで、指定したタグ（または作業ブランチ）を実機に入れられる
- 入れる前に、走行中でないこと、作業中の機能がないことを確かめる。入れ替えの前に記録と設定をバックアップする
- 入れた後に動作確認（self-check と起動後の確認）を行い、失敗したら前のタグに自動で戻す
- 今どの版が入っているかが分かる（ログと LCD）
- 新しい SD カードに OS を入れた状態から、スクリプトで実機の環境を作り直せる

## Approach
開発 PC から ssh で実機のスクリプトを実行する。実機の作業コピーでタグを切り替え、依存関係が変わっていれば入れ、サービスを再起動し、確認する。接続先は開発 PC の `~/.ssh/config` に置き、リポジトリには書かない。初期設定はリポジトリ内のスクリプトにまとめる。

## Scope
- **In**:
  - デプロイのスクリプト（タグまたはブランチの指定、事前確認、バックアップ、切り替え、依存関係、再起動、確認、自動の切り戻し）
  - self-check のコマンド（設定、LCD、声、モデルなどの確認。各機能が確認項目を足せる形にする）
  - 版の表示
  - 実機の初期設定スクリプト（apt、仮想環境、声とモデルの取得、systemd の登録、ユーザーのリンガリング）
  - デプロイの手順書
- **Out**:
  - CI からの自動デプロイ（今回は開発 PC から行う。Tailscale などは後で検討）
  - 複数台への配置
  - OS イメージの作成

## Boundary Candidates
- デプロイと切り戻し
- self-check（項目は各機能が足す）
- 初期設定（provision）

## Out of Boundary
- 個々の機能の確認の中身（各機能が self-check の口に足す）
- サービスの停止時の安全処理（`rover-core`）

## Upstream / Downstream
- **Upstream**: `rover-core`（起動・停止、self-check の口、版の情報、systemd のユニット）
- **Downstream**: すべての機能のスペックの実機確認

## Existing Spec Touchpoints
- **Extends**: なし
- **Adjacent**: `rover-core`

## Constraints
- 公開リポジトリに、実機の IP アドレス・ホスト名・秘密情報を書かない
- 実機は子どもが使う 1 台。使っていない時間帯に入れ替える
- 個人の設定と記録（リポジトリの外）は入れ替えで消さない
- 初期設定で必要なこと（技術構成の確認より）:
  - ユーザーのリンガリング（`loginctl enable-linger`）。これがないと、起動時に PipeWire が動かず声が出ない
  - 画面のない環境では、WirePlumber のセッションの監視を止める必要がある場合がある
  - SunFounder の `install.sh` は root で全体に入れるので、システムの準備（I2C、I2S、lgpio）にだけ使う。robot_hat は仮想環境に、固定したコミットで入れ直す
  - OpenCV、numpy、picamera2 は apt から入れる（仮想環境は `--system-site-packages`）
  - OS は 64-bit の Trixie 系を推奨する（実機の OS は要確認）
