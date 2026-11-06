# Project Structure

## Organization Philosophy

**基盤と機能を分け、機能ごとにまとめる**（feature-first）。ハードの実装はアダプタに隔離します。
- パッケージの境界は、スペックの境界にそろえる。1 つのスペックが持つコードは、1 つのパッケージにまとめる
- 依存の向き:
  - 機能 → 基盤（ポートと Feature の契約）
  - アダプタ → ポート
  - 基盤は機能もアダプタの実装も知らない
- 機能同士は、相手の**公開 API**（`api.py`）だけを使う。依存してよい組み合わせは `roadmap.md` で宣言したものだけ

## Directory Patterns

### パッケージ本体
**Location**: `src/osekkai/`（src レイアウト。パッケージ名は `osekkai`）
**Purpose**: 製品のコード。`python -m osekkai` で起動する

### 基盤
**Location**: `src/osekkai/core/`
**Purpose**: ポート、資源の調停、Feature の契約、起動と停止、設定、保存、時計、待機画面（`rover-core`）
**Example**: `core/ports.py`（`DisplayPort`、`VoicePort` など）、`core/arbiter.py`、`core/feature.py`

### 基盤の機能（入口）
**Location**: `src/osekkai/voice/`
**Purpose**: 音声の入口。聞き取りのポートと、意図の登録・振り分け（`voice-gateway`）

### 機能
**Location**: `src/osekkai/features/<feature>/`
**Purpose**: 1 つの機能（スペック）を 1 つのパッケージにまとめる。判定ロジック、Feature の実装、公開 API を置く
**Example**:
- `features/routine/`（`daily-routine`）、`features/presence/`、`features/conversation/`、`features/daruma/`、`features/remote/`
- 中の構成: `feature.py`（Feature の実装と登録）、`logic.py` など（純粋な判定）、`api.py`（ほかの機能へ公開する口。必要なときだけ）

### アダプタ
**Location**: `src/osekkai/adapters/pi/`、`src/osekkai/adapters/fake/`
**Purpose**:
- `pi/`: 実機用。ハードのライブラリを読み込んでよいのはここだけ
- `fake/`: テストと PC での実行に使う偽物
- 機能に固有のデバイス（例: カメラ、マイク、LLM）のアダプタは、その機能のパッケージの中に `adapters/` を作ってもよい。その場合も、ハードのライブラリを読み込むのはそのモジュールの中だけにする

### テスト
**Location**: `tests/`（`src/osekkai/` と同じ構成）
**Purpose**: `tests/core/`、`tests/features/routine/` など。共通の偽物は `osekkai.adapters.fake` を使い、テスト専用の補助は `tests/conftest.py` に置く
- `tests/deploy/`: 配置の道具のテスト。外のコマンドは偽の Runner（`tests/deploy/fake_runner.py`）と一時ディレクトリで確かめ、実機には触れない
- `tests/fixtures/`: 基盤と道具が共有するファイル（`status.json`・`notice.json`・self-check の JSON・版のログ）の見本。基盤のテストと道具のテストの両方が読み、形のずれを防ぐ（`tests/shared_fixtures.py` で読む）
- `tests/` には `__init__.py` がないので、別のディレクトリでも同じ名前のテストのファイルを置かない

### 設定の見本
**Location**: `config/`
**Purpose**: 設定の**見本だけ**を置く（例: `config/osekkai.example.yaml`）。実際の設定はリポジトリの外（XDG のディレクトリ）に置く

### 運用（実機への配置）
**Location**: `deploy/`（`device-deployment`）、`scripts/`（開発用のスクリプト）、`docs/deploy.md`（手順書）
**Purpose**: 開発 PC から実機に版を入れ、確かめ、だめなら戻す道具と、systemd のユニット
- `deploy/rover-deploy`: 開発 PC の入口（python3 の実行ファイル）。`rover_deploy.devcli` を呼ぶ
- `deploy/rover_deploy/`: 道具のパッケージ。開発 PC の側と実機の側の両方を 1 つにまとめ、開発 PC から実機の `~/osekkai-rover/tool/` に写して `python3 -m rover_deploy.agent <sub>` で動かす
  - 開発 PC の側: `devcli.py`（サブコマンドの表と流れ）、`devconfig.py`（利用者の設定）、`refs.py`（タグとブランチの解決）、`remote.py`（ssh）
  - 実機の側: `agent.py`（サブコマンドの入口。標準出力に JSON を 1 つ）、`transaction.py`（入れ替えの流れ）、`releases.py`（版のディレクトリ）、`backups.py`、`verify.py`（入れた後の確認）、`service_status.py`（安全の判断）、`provision.py`（初期設定）、`notices.py`、`calibration.py`、`state.py`（状態・記録・ロック）、`layout.py`（実機の置き場所は全部ここから取る）
  - 両側: `common.py`（型・終了コード・版の表示）、`system.py`（外のコマンドの口 Runner と、systemctl・journalctl・git の薄い包み）
  - サブコマンドを足すときは、`devcli._subcommands()` と `agent._handlers()` の表に足す
- `deploy/systemd/`: `osekkai.service`（新しい基盤。`~/osekkai-rover/current` を動かす。サンプルの `osekkai-rover.service` と `Conflicts=`）、戻し忘れの `osekkai-trial-reminder.service`・`.timer`

## Naming Conventions

- **Files / modules**: `snake_case.py`
- **Classes**: `PascalCase`
  - ポートは `〜Port`（例: `DisplayPort`、`MotorPort`）
  - 実機用のアダプタはデバイス名（例: `Lcd1602Display`）
  - 偽物は `Fake〜`（例: `FakeDisplay`）
  - 機能は `〜Feature`（例: `RoutineFeature`）
- **Functions / variables**: `snake_case`。定数は `UPPER_SNAKE_CASE`
- **設定のキー**: `snake_case`。機能ごとにトップレベルのキーを 1 つ持つ（例: `routine:`、`presence:`）
- **ブランチ・スペック名**: kebab-case（`git-workflow.md` と `.kiro/specs/` に合わせる）
- **利用者に見える言葉**（声・LCD）: ひらがな中心のやさしい日本語。LCD は半角カタカナ

## Import Organization

```python
# 絶対インポートを使う
from osekkai.core.ports import DisplayPort
from osekkai.features.presence.api import MotionSensor   # 他の機能は api だけ

# ハードのライブラリはアダプタの中で、使うときに読み込む
class Lcd1602Display:
    def __init__(self, address: int) -> None:
        from smbus2 import SMBus  # adapters/pi/ の中だけ
        self._bus = SMBus(1)
```

- 相対インポートは使わない
- インポートの並びは ruff（isort 相当）に従う

## Code Organization Principles

- **判定と操作を分ける**: 「いつ・何を・どうするか」は純粋な関数や状態遷移にする。声・画面・動きはポート越しに行う
- **資源は借りて返す**: 機能はハードを直接持たない。調停役から借り、中断の通知を受けたら手放す
- **優先度は帯から選ぶ**: 機能の優先度は、基盤の `Priority` の帯から選ぶ。帯は振る舞いの種類で決める
  - `SCHEDULED`（決まった時刻の声かけ）> `INTERACTIVE`（会話・遊び）> `REMOTE`（遠隔の操作）> `BACKGROUND`（見守り）
  - 同じ帯の中の順番は、0〜9 を足して付ける
  - 新しい補佐機能も、このどれかに当てはめる
- **機能を足しても基盤を書き換えない**: 新しい機能は `features/<feature>/` を足し、登録するだけにする。基盤に機能の名前や優先順位の表を書かない
- **個人データの置き場所は固定**: 設定・記録・顔の特徴・キャッシュは、基盤の保存の仕組みが決める XDG のディレクトリにだけ置く

---
_Document patterns, not file trees. New files following patterns shouldn't require updates_
