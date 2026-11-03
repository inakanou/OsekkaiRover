# OsekkaiRover（おせっかいローバー）

子どもの生活習慣づくりを手伝う、相棒ロボットのソフトウェアです。
決まった時刻に声をかけ、できたらボタンを押してもらい、ほめて星をためます。

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
| ソースコード | 基盤（`src/osekkai/core`）とハードなしの実行（`src/osekkai/adapters/fake`）を実装中。実機のアダプタはこれから |
| ステアリング（product / tech / structure） | 作成済み（`.kiro/steering/`） |
| スペック | `rover-core`（基盤）を実装中（`.kiro/specs/rover-core/`） |
| 実機へのデプロイの仕組み | 未作成 |

## 今後の予定

1. **サンプルの取り込み**: スペックを起票して取り込みます。あわせて、停止時にモーターを必ず止める対策と、CI の lint・テストも入れます
2. **ステアリング**: コードが入ったら `/kiro-steering` で product / tech / structure を作ります
3. **デプロイの仕組み**: 実機にタグの版を入れる、動作確認する、失敗したら前の版に戻す。この仕組みをスペックにして作ります

## ディレクトリ構成

```
.
├── CLAUDE.md                 # Claude Code 向けのプロジェクト指示（開発ルール・ワークフロー）
├── .github/                  # CI（GitHub Actions）と PR テンプレート
├── scripts/                  # 開発用スクリプト
├── .claude/
│   └── skills/kiro-*/        # /kiro-* コマンドの本体（SKILL.md）
└── .kiro/
    ├── settings/templates/   # ステアリング・スペックのテンプレート
    ├── steering/             # プロジェクト全体の方針（git-workflow。product / tech / structure は未作成）
    └── specs/                # 機能ごとのスペック（requirements / design / tasks）※未作成
```

- **ステアリング**（`.kiro/steering/`）: プロダクトの目的、技術スタック、構成など、プロジェクト全体で守るルールと前提。
- **スペック**（`.kiro/specs/<feature>/`）: 機能ごとの要件・設計・タスク。文書の言語は `spec.json` の `language`（既定は `ja`）に従います。

## はじめかた

前提: [Claude Code](https://claude.com/claude-code) が使えること。

1. **ステアリングを作る**（推奨）
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
   pip install -e '.[dev]'
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

## 開発の流れ（ブランチ・PR・リリース）

詳しくは [`.kiro/steering/git-workflow.md`](.kiro/steering/git-workflow.md) を参照してください。

- `main` と短命な作業ブランチ（`feat/<feature>`、`fix/*`、`docs/*`、`chore/*`）だけを使います。`main` への直接 push はできません。
- PR は CI の `guard`（禁止ファイルと秘密情報のチェック）が通ってから squash でマージします。
- ハードに関わる変更は、マージ前に実機（Raspberry Pi 5）で確認します。
- 実機に入れるのはタグ `vX.Y.Z` を付けた版だけです。
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
