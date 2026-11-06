# Technology Stack

## Architecture

**ポートとアダプタ**（ヘキサゴナル）に、**機能を足す口**を組み合わせます。
- **基盤**（`rover-core`）:
  - ハードの操作をポート（インターフェース）として定める
  - 共有資源（声・画面・モーター・首・LED・ボタン）を調停する
  - 機能の登録・起動・停止を管理する
- **機能**（日課、あいさつ、会話、遊び、ラジコン、今後の補佐機能）:
  - 基盤の口に自分を登録する
  - 優先度と使う資源を自分で申告する
  - 基盤は個々の機能を知らない
- **アダプタ**:
  - 実機用（Robot HAT、LCD、Open JTalk、カメラ、マイク、Claude API など）と、テストや PC での実行に使う偽物（fake）を用意する
  - どちらもポートを実装する
- **調停**:
  - 資源は 1 か所が持つ。機能は資源を「借りて返す」
  - 優先度の高い機能が借りに来たら、低い機能は中断の通知を受けて手放す
  - スレッドから直接ハードを触らない

```
features/* ──(Feature の契約・ポート)──> core ──(ポート)──> adapters/{pi,fake}
     └──(公開 API だけ)──> 他の機能の api（依存は roadmap で宣言したものだけ）
```

## Core Technologies

- **Language**: Python 3.11 以上（実機の推奨は Trixie 系の 3.13。Bookworm の 3.11 でも動くこと）
- **Runtime**:
  - Raspberry Pi OS（64-bit）上の systemd **ユーザーサービス**
  - 仮想環境は `--system-site-packages` で作り、apt のパッケージを使えるようにする
- **Hardware**: SunFounder PiCar-X（Raspberry Pi 5 + Robot HAT）、I2C LCD 1602（PCF8574、0x27）、首のカメラ、USB カメラ（Logicool C270、マイク兼用）
- **CI**: GitHub Actions（ubuntu-latest、x86）。ハードなしで lint・型チェック・テストを行う

## Key Libraries

| 用途 | ライブラリ | 決まりごと |
| --- | --- | --- |
| Robot HAT（サーボ・モーター・ピン・センサー） | 自前の薄い部品（`smbus2` で HAT のマイコンのレジスタ、`lgpio` で GPIO） | SunFounder の `robot_hat` と `picarx` は使わない（読み込みの副作用、スレッドから安全でない、エラーを捨てる）。レジスタの仕様は robot_hat 2.5.x のコードを参照する（同じ GPL 系） |
| LCD | `smbus2` | LCD と HAT は同じ I2C バスを使うので、書き込みはロックする |
| 声 | Open JTalk（apt、サブプロセス）＋ PipeWire（`paplay`） | 再生先のデバイスは明示する |
| 音声の文字化 | `faster-whisper` | 日本語に固定し、VAD を使う。実機の中だけで処理する |
| カメラ・顔 | `picamera2`（apt）、OpenCV 4.10 以上 5 未満（実機は apt、CI は headless）、YuNet・SFace | モデルはリポジトリに入れず、初期設定で取得する |
| 会話 | `anthropic`（Python SDK） | 呼び出し方は設計の段階で claude-api のスキルで確かめる |
| ラジコンの Web | Flask | 家の LAN だけに公開する |
| 祝日 | `jpholiday` | 毎年、法改正に追随しているかを確かめる |
| 設定 | PyYAML、pydantic v2（PyPI） | 機能ごとに `BaseModel` で項目・既定値・範囲を定める。Bookworm の apt の pydantic は v1 なので使わない |

ハードのライブラリ（`smbus2`、`lgpio`、`numpy`、`picamera2`、`faster_whisper`、`anthropic`、`cv2`）は、**アダプタのモジュールの中だけ**で読み込みます。基盤・機能・テストからは読み込みません（CI で読み込めない、または読み込みに時間がかかるため）。

### 配置の道具（`deploy/rover_deploy/`）の決まり

- **標準ライブラリだけ**で書く。Python 3.11 以上で動くこと（実機ではシステムの `python3` で、仮想環境なしに動く）
- **`osekkai` のパッケージを読み込まない**（テストで縛っている）。基盤とは次のものだけでつながる
  - ファイル: 基盤が書く `$XDG_RUNTIME_DIR/osekkai/status.json`（走行中・作業中・心拍）と、道具が書く `notice.json`（LCD の知らせ）。形は `tests/fixtures/` の見本にそろえる
  - コマンド: 版の中の `.venv/bin/python -m osekkai --self-check --json` と `-m osekkai.core.safe_stop --strict` をサブプロセスで呼ぶ
- 外のコマンドは `system.Runner` で呼び、テストでは偽の Runner に差し替える
- 実機の接続先（ホスト名・IP アドレス・ユーザー名）はリポジトリに書かない。開発 PC の `~/.ssh/config` と `~/.config/osekkai-deploy/config.toml` から読む
- 表示と記録には、決まった文言・手順の名前・例外の型の名前だけを出す。外のコマンドの標準エラー・設定の値・記録の中身は出さない
- 終了コード: 0 成功、10 中止（実機は変えていない）、11 切り戻し、12 停止（人の手が要る）、13 結果が分からない（接続が切れた）

## Development Standards

### Type Safety
- 公開する関数・クラス・ポートには型ヒントを付ける
- ポートは `typing.Protocol` で定義する
- `mypy` で型チェックする（`core` と `features` は厳しめの設定）

### Code Quality
- `ruff` で lint と整形を行う
- 時計（現在時刻・経過時間）は差し替えられるようにし、`datetime.now()` や `time.monotonic()` を直接呼ばない
- ログは `logging` で出し、journal に流す。**子どもの名前・話した言葉・入力された文字・API キー・pin をログに出さない**
- 予期しない例外では、その機能を止めて基盤は動き続ける。どの止まり方でも、モーターを止めて首を正面に戻す

### Testing
- `pytest`。判定ロジック（時刻の判定、状態遷移、安全判定、振り分け）は、偽のアダプタと偽の時計で PC だけでテストする
- ハードに触れる部分は、self-check と実機確認で確かめる（`git-workflow.md` の「実機確認」）
- テストの名前は、振る舞いを日本語で書いてよい（例: `test_寝る時刻には催促中でもおやすみモードに入る`）

## Development Environment

### Required Tools
- Python 3.11 以上、`pip`、`venv`
- 実機向け: Raspberry Pi OS（64-bit）、`open-jtalk`、`open-jtalk-mecab-naist-jdic`、PipeWire、ユーザーのリンガリング

### Common Commands
```bash
# Dev:  python -m osekkai --fake-hardware   # PC で偽のハードを使って動かす
# Run:  python -m osekkai                   # 実機で動かす
# Lint: ruff check . && ruff format --check .
# Type: mypy
# Test: pytest
# Deploy: deploy/rover-deploy deploy <タグかブランチ>   # 開発 PC から実機へ（docs/deploy.md）
```
（コマンドの最終形は `rover-core` の設計で決める）

## Key Technical Decisions

| 決めたこと | 理由 |
| --- | --- |
| ポートとアダプタ。ハードは偽物と差し替える | 実機は子どもが毎日使う 1 台。大半を PC と CI で確かめたい |
| 機能は自分で優先度と資源を申告し、基盤が調停する | 機能を足すたびに中央を書き換えないため。サンプルではスレッド同士がモーターや声を取り合っていた |
| 音声の入口（振り分け）と会話（LLM）を分ける | 今後、音声が各機能への入口になる |
| 設定・記録・キャッシュは XDG のディレクトリ（リポジトリの外）に置く | 公開リポジトリ。デプロイでタグを切り替えても消えないようにする |
| 記録は一時ファイルに書いてから置き換える | 停電で記録が壊れ、起動できなくなるのを防ぐ |
| 1 日の上限など、再起動をまたぐ状態は保存する | 再起動で制限が外れないようにする |
| 子どもの名前は外部に送らず、ロボットの中で差し込む | 個人データの保護 |
| SIGTERM と SIGINT の両方で、安全に止める。systemd の `ExecStopPost` でも必ずモーターを止める | systemd の停止でモーターが止まらない危険を防ぐ。HAT のマイコンは、プロセスが落ちても PWM を保つため |
| HAT は自前の薄い部品で操作する | robot_hat は読み込みの副作用が重く、スレッドから安全に使えず、I2C のエラーを黙って捨てる（`rover-core` の調査） |
| 依存関係は extras に分ける（実機用・音声・カメラ・会話・ラジコン） | CI と PC で、実機用のライブラリなしに入れられるようにする |

---
_Document standards and patterns, not every dependency_
_updated_at: 2026-11-05（device-deployment: 配置の道具の決まり）_
