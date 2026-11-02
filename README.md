# OsekkaiRover

Kiro 式の **スペック駆動開発（Spec-Driven Development）** で進めるプロジェクトです。
Claude Code のスキル（`/kiro-*` コマンド）を使い、要件 → 設計 → タスク → 実装 の順に、各フェーズで人がレビュー・承認しながら開発します。

> **現在の状態**: 開発の土台だけを用意した段階です。製品コード、ステアリング（`.kiro/steering/`）、スペック（`.kiro/specs/`）はまだありません。

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
