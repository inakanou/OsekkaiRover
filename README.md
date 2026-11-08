# OsekkaiRover（おせっかいローバー）

家庭で暮らしに寄り添う、補佐役のロボットのソフトウェアです。SunFounder PiCar-X（Raspberry Pi 5）の上で動きます。
最初の領域は、子ども（4 歳前後）の生活習慣づくりと遊びのサポートです。決まった時刻に声をかけ、できたらボタンを押してもらい、ほめて星をためます。

**ゴール**: このリポジトリの成果物を実機の Raspberry Pi 5 に入れて、正しく動かすこと。

## 実機

| 部品 | 内容 |
| --- | --- |
| 本体 | SunFounder PiCar-X（Raspberry Pi 5 + Robot HAT） |
| 表示 | I2C LCD 1602（アドレス 0x27） |
| カメラ | PiCar-X の首のカメラ、USB カメラ（Logicool C270。マイクとしても使う） |
| 台数 | 1 台（子どもが毎日使っている機械で開発中の版も試す） |

## 開発の進め方

Kiro 式の **スペック駆動開発（Spec-Driven Development）** で進めます。
Claude Code のスキル（`/kiro-*` コマンド）を使い、要件 → 設計 → タスク → 実装 の順に、各フェーズで人がレビュー・承認しながら開発します。

## 現在の状態

| 項目 | 状態 |
| --- | --- |
| 開発ワークフロー（ブランチ・PR・CI） | 整備済み（[`git-workflow.md`](.kiro/steering/git-workflow.md)） |
| ステアリング（product / tech / structure / roadmap） | 作成済み（`.kiro/steering/`） |
| 基盤 `rover-core` | 完了。起動と安全な停止、設定と保存、LCD・声・ボタン・首・走行、資源の調停、機能を足す口、待機画面（`src/osekkai/core`、実機のアダプタ `src/osekkai/adapters/pi`、ハードなしの偽物 `src/osekkai/adapters/fake`） |
| 実機への配置 `device-deployment` | 完了。版を入れる・確かめる・だめなら戻す・初期設定（`deploy/`、手順書は [`docs/deploy.md`](docs/deploy.md)） |
| 日課 `daily-routine` | 完了。実機で確かめた（`src/osekkai/features/routine`、手順書は [`docs/routine.md`](docs/routine.md)） |
| 音声の入口 `voice-gateway` | 完了。実機で確かめた（`src/osekkai/voice`、手順書は [`docs/voice.md`](docs/voice.md)） |
| 見つけてあいさつ `presence` | 実装済み。実機の確認はこれから（`src/osekkai/features/presence`、手順書は [`docs/presence.md`](docs/presence.md)） |
| おしゃべり `conversation` | 完了。実機で確かめた（`src/osekkai/features/conversation`、手順書は [`docs/conversation.md`](docs/conversation.md)） |
| だるまさん `daruma-game` | 完了。実機で確かめた（`src/osekkai/features/daruma`、手順書は [`docs/daruma.md`](docs/daruma.md)） |
| ラジコン `remote-control` | 実装済み。実機の確認はこれから（`src/osekkai/features/remote`、手順書は [`docs/remote.md`](docs/remote.md)） |

## 機能

基盤（`src/osekkai/core`）に、機能を足していきます。機能は `src/osekkai/features/`（音声の入口は `src/osekkai/voice/`）にあり、`pyproject.toml` のエントリーポイントで登録します。

| 機能 | 名前 | 内容 | 手順書 |
| --- | --- | --- | --- |
| 日課 | `routine` | 決まった時刻に声をかけ、できたらほめて星をためる。寝る時刻におやすみモードに入る | [`docs/routine.md`](docs/routine.md) |
| 音声の入口 | `voice` | ボタンを押して話すと、聞き取った言葉を、その言葉を登録した機能に渡す（`src/osekkai/voice`） | [`docs/voice.md`](docs/voice.md) |
| 見つけてあいさつ | `presence` | 首のカメラで顔を見つけて首と目で追い、覚えた顔に呼び名であいさつする | [`docs/presence.md`](docs/presence.md) |
| おしゃべり | `conversation` | どの機能の言葉にも当てはまらない言葉に、Claude で子ども向けの短い返事をする | [`docs/conversation.md`](docs/conversation.md) |
| だるまさん | `daruma` | 「だるまさん しよう」と話しかけると、ロボットが鬼になって「だるまさんがころんだ」をする。走らない | [`docs/daruma.md`](docs/daruma.md) |
| ラジコン | `remote` | 家の Wi-Fi の中のスマホやタブレットの画面から、ロボットを走らせる。首・ダンス・映像・写真・話させる | [`docs/remote.md`](docs/remote.md) |

日課の設定は、設定ファイルの `routine:` の節に書きます。見本では全部コメントなので、書くまではタスクは 0 個です。
音声の入口の設定は、設定ファイルの `voice:` の節に書きます。実機では `recognizer: whisper` にします。
見つけてあいさつの設定は、設定ファイルの `presence:` の節に書きます。顔は、ロボットを止めてからコマンドで覚えさせます（手順書を見てください）。
だるまさんの設定は、設定ファイルの `daruma:` の節に書きます。動いたかのしきい値は、ロボットを止めてから道具で確かめて合わせます（手順書を見てください）。
ラジコンの設定は、設定ファイルの `remote:` の節に書きます。既定は無効です。pin は実機の設定ファイルだけに、引用符で囲んで書きます（手順書を見てください）。

## 今後の予定

スペックを 1 つずつ、要件 → 設計 → タスク → 実装 → PR の順に進めます（[`roadmap.md`](.kiro/steering/roadmap.md)）。

1. **見つけてあいさつ**（`presence`）: 顔を見つけて首を向け、覚えた顔ならあいさつする。実機で確かめる
2. **スマホのラジコン**（`remote-control`）: 家の Wi-Fi から操作する。実機で確かめる

## ディレクトリ構成

```
.
├── CLAUDE.md                 # Claude Code 向けのプロジェクト指示（開発ルール・ワークフロー）
├── src/osekkai/              # ロボットのプログラム（python -m osekkai）
│   ├── core/                 # 基盤（ポート・資源の調停・機能を足す口・設定・保存・待機画面）
│   ├── adapters/pi/          # 実機用のアダプタ（ハードのライブラリはここだけで読む）
│   ├── adapters/fake/        # テストと PC で使う偽物
│   ├── features/routine/     # 日課
│   ├── features/presence/    # 見つけてあいさつ
│   ├── features/conversation/ # おしゃべり
│   ├── features/daruma/      # だるまさん
│   ├── features/remote/      # ラジコン
│   └── voice/                # 音声の入口
├── tests/                    # テスト（src/osekkai と同じ構成。配置の道具は tests/deploy）
├── config/                   # 設定の見本（osekkai.example.yaml）。実際の設定はリポジトリの外
├── deploy/                   # 実機への配置の道具（rover-deploy）と systemd のユニット
├── docs/                     # 手順書（deploy.md、routine.md、voice.md、presence.md、conversation.md、daruma.md、remote.md）
├── scripts/                  # 開発用スクリプト
├── .github/                  # CI（GitHub Actions）と PR テンプレート
├── .claude/
│   └── skills/kiro-*/        # /kiro-* コマンドの本体（SKILL.md）
└── .kiro/
    ├── settings/templates/   # ステアリング・スペックのテンプレート
    ├── steering/             # プロジェクト全体の方針（product / tech / structure / roadmap / git-workflow）
    └── specs/                # 機能ごとのスペック（brief / requirements / design / tasks）
```

- **ステアリング**（`.kiro/steering/`）: プロダクトの目的、技術スタック、構成など、プロジェクト全体で守るルールと前提。
- **スペック**（`.kiro/specs/<feature>/`）: 機能ごとの要件・設計・タスク。文書の言語は `spec.json` の `language`（既定は `ja`）に従います。

## はじめかた

前提: [Claude Code](https://claude.com/claude-code) が使えること。

1. **ステアリングを作る・直す**（このリポジトリでは作成済み。方針が変わったら直す）
   ```
   /kiro-steering
   ```
   `product.md` / `tech.md` / `structure.md` が `.kiro/steering/` に作られます。
   セキュリティやテスト方針など個別の方針は `/kiro-steering-custom` で追加します。

2. **アイデアを整理する**（任意）
   ```
   /kiro-discovery "作りたいもの"
   ```
   進め方を判断し、`brief.md`（複数スペックに分かれる場合は `roadmap.md` も）を作ります。

3. **スペックを作る**
   - まとめて作る: `/kiro-spec-quick <feature> [--auto]`
   - 1 段ずつ作る:
     | 手順 | コマンド |
     | --- | --- |
     | 初期化 | `/kiro-spec-init "機能の説明"` |
     | 要件 | `/kiro-spec-requirements <feature>` |
     | 既存コードとの差分確認（任意） | `/kiro-validate-gap <feature>` |
     | 設計 | `/kiro-spec-design <feature> [-y]` |
     | 設計レビュー（任意） | `/kiro-validate-design <feature>` |
     | タスク | `/kiro-spec-tasks <feature> [-y]` |
   - `roadmap.md` の全スペックを並列で作る: `/kiro-spec-batch`

4. **実装する**
   ```
   /kiro-impl <feature> [タスク番号] [--review required|inline|off]
   ```
   - タスク番号なし: タスクごとにサブエージェントが実装し、独立したレビューと最終検証まで自動で進めます。
   - タスク番号あり: 指定したタスクを手動で進めます（完了前のレビューはあります）。
   - 最後に `/kiro-validate-impl <feature>` で機能全体を検証します。

5. **進捗を見る**: `/kiro-spec-status <feature>`（いつでも実行できます）

## 開発の始め方（ロボットのプログラム）

前提: Python 3.11 以上。実機がなくても PC で動かせます。

1. **インストール**
   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -e '.[dev,remote]'
   ```

2. **ハードなしで動かす**
   ```sh
   python -m osekkai --fake-hardware
   ```
   - 画面（`[LCD]`）・話す文章（`[声]`）・LED・首と走行の状態が端末に出ます。ログは標準エラーに出ます
   - Enter キーを押すと、USR ボタンを押したことになります
   - Ctrl+C で止まります（終了コード 0）
   - `--verbose` で詳しいログ、`--config PATH` で別の設定ファイルを使います

3. **話して終わる**（声の確認）
   ```sh
   python -m osekkai --fake-hardware --say こんにちは
   ```
   ほかに動いているときは、`すでに動いています` と出て終了コード 75 で終わります。

4. **確認する**（CI と同じ 4 つ）
   ```sh
   ruff check .
   ruff format --check .
   mypy
   pytest -q
   ```

### 設定ファイルと保存の場所

| 種類 | 場所 |
| --- | --- |
| 設定ファイル | `~/.config/osekkai/config.yaml`（`$XDG_CONFIG_HOME`）。ないときは見本 [`config/osekkai.example.yaml`](config/osekkai.example.yaml) から作ります |
| 状態 | `~/.local/state/osekkai/`（`$XDG_STATE_HOME`） |
| キャッシュ（声など） | `~/.cache/osekkai/`（`$XDG_CACHE_HOME`） |
| 単一の実行のロック | `$XDG_RUNTIME_DIR/osekkai/rover.lock`（未設定なら `/tmp/osekkai-<uid>/rover.lock`） |

どれもリポジトリの外にあるので、版を入れ替えても残ります。個人の設定はコミットしません。

## 実機への配置

実機に版を入れる・普段の版に戻す・初期設定・バックアップから戻す・self-check は、開発 PC から道具 `deploy/rover-deploy` で行います。
手で実機の版を切り替えないでください。

```sh
deploy/rover-deploy deploy <タグかブランチ>   # 版を入れる（だめなら自動で前の版に戻す）
deploy/rover-deploy back                    # 普段の版に戻す
deploy/rover-deploy status                  # 版の確認
```

接続先の設定の仕方、初期設定、失敗したときに人がすること、モーターを確実に止める方法は、手順書 [`docs/deploy.md`](docs/deploy.md) を見てください。
接続先（ホスト名・IP アドレス・ユーザー名）は、開発 PC の `~/.ssh/config` と `~/.config/osekkai-deploy/config.toml` にだけ書きます。

## 開発の流れ（ブランチ・PR・リリース）

詳しくは [`.kiro/steering/git-workflow.md`](.kiro/steering/git-workflow.md) を参照してください。

- `main` と短命な作業ブランチ（`feat/<feature>`、`fix/*`、`docs/*`、`chore/*`）だけを使います。`main` への直接 push はできません。
- PR は CI の `guard`（禁止ファイルと秘密情報のチェック）が通ってから squash でマージします。
- ハードに関わる変更は、マージ前に実機（Raspberry Pi 5）で確認します。
- 実機の普段の版にするのは、タグ `vX.Y.Z` を付けた版だけです。作業ブランチは、試しの版として入れて確かめられます（[`docs/deploy.md`](docs/deploy.md)）。
- 公開リポジトリなので、個人設定・記録・顔の特徴・写真や音声・API キー・pin は絶対にコミットしません。手元で確かめるには次を実行します。
  ```sh
  bash scripts/check-forbidden-files.sh
  ```

## 開発ルール

- 要件 → 設計 → タスク → 実装 の各フェーズで人が承認してから次へ進みます。`-y` での承認省略は意図して使う場合だけにします。
- ステアリングは常に最新に保ち、`/kiro-spec-status` で整合を確認します。
- 詳しくは [`CLAUDE.md`](CLAUDE.md) を参照してください。

## ライセンス

[GNU General Public License v3.0 or later](LICENSE)（SPDX: `GPL-3.0-or-later`）

### 第三者のソフトウェアとデータ

このリポジトリには含めず、実機の初期設定で取得または導入します。

| 名前 | 用途 | ライセンス |
| --- | --- | --- |
| [SunFounder robot-hat](https://github.com/sunfounder/robot-hat) | Robot HAT のレジスタの仕様の参考（このリポジトリは使わず、自前の部品で操作する） | GPL-3.0 |
| [HTS voice tohoku-f01](https://github.com/icn-lab/htsvoice-tohoku-f01) | 声。© 2015 Intelligent Communication Network (Ito-Nose) Laboratory, Tohoku University | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| [Open JTalk](https://open-jtalk.sourceforge.net/) | 音声合成 | Modified BSD |
| [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)（OpenCV Zoo） | 顔検出のモデル | MIT |
| [SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface)（OpenCV Zoo） | 顔認識のモデル | Apache-2.0 |
