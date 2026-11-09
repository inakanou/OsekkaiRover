# Roadmap

## Overview
OsekkaiRover は、SunFounder PiCar-X（Raspberry Pi 5 + Robot HAT + I2C LCD 1602）で動く、**仕事や生活の補佐役**となるロボットのソフトウェアです。最初の領域は、子どもの生活習慣づくりと遊びのサポートです。ゴールは、このリポジトリの成果物を実機の Raspberry Pi 5 に入れて正しく動かすことです。

手元にある参照実装（サンプル、Python 約 1,900 行。リポジトリ直下の `OsekkaiRover-src.zip`、Git の対象外）の振る舞いをスペックに起こし、スペック駆動開発のフローで**作り直します**。子どもから見た振る舞い（声かけ・ほめる・星・あいさつ・あそび・ラジコン）はサンプルを踏襲し、安全・個人データ・壊れにくさの問題は要件として直します。

## Approach Decision
- **Chosen**: 基盤と機能を分ける。ハードを切り離す（ポートとアダプタ）
  - 基盤（`rover-core`、`device-deployment`、`voice-gateway`）が「機能を足す口」と共有資源（声・画面・モーター・ボタン・音声入力）の調停を持つ
  - 各機能は口を使って自分を登録し、優先度と受け付ける言葉を自分で申告する。基盤は個々の機能を知らない
  - 判定ロジックはハードから切り離し、PC と CI でテストする。ハードはアダプタとして差し替える
- **Why**:
  - 今後、仕事や生活の補佐機能を増やしていく。機能を足すたびに基盤を書き換えずに済む形にしたい
  - 音声が各機能への入口になる見込みがある。入口（振り分け）と会話（Claude）は別の責任として分ける
  - 実機は 1 台で、子どもが毎日使っている。実機に入れる前に、PC で大半を確かめたい
- **Rejected alternatives**:
  - サンプルのモジュール構成を踏襲: 速いが、ハードを先頭で読み込むため PC でテストできる範囲が狭い。メインループが全機能の優先順位を持つので、機能追加のたびに中央を書き換えることになる
  - 層ごとのスペック（雛形・ハードのアダプタ・ランタイムを別々に）: 利用者から見た価値がなく、人が読んで何ができるのか分からない。ハードのアダプタを複数の機能が共有して担当が重なる
  - ROS 2: 機能に対して重い。Pi 1 台・Python のみの規模では学習と運用の負担が大きい

## Scope
- **In**: サンプルの全機能の作り直し（生活習慣の声かけ、顔を見つけてあいさつ、おしゃべり、だるまさんがころんだ、スマホのラジコン）、機能を足す仕組み、音声の入口、実機へのデプロイと切り戻し、PC と CI でのテスト
- **Out**: 仕事や生活の補佐としての新しい機能（このロードマップの後に、同じ口を使って追加する）、複数台の運用、家の外からの操作、クラウドへの記録の保存

## Constraints
- 実行環境: Raspberry Pi 5、Raspberry Pi OS（64-bit）、Python、systemd のユーザーサービス
- ハード: PiCar-X（Robot HAT）、I2C LCD 1602（0x27）、首のカメラ、USB カメラ（Logicool C270、マイク兼用）
- 実機は 1 台で、子どもが毎日使っている。試すのは使っていない時間帯にする（`git-workflow.md`）
- リポジトリは公開。子どもの名前・記録・顔の特徴・写真や音声・API キー・pin はリポジトリに入れない。個人の設定と記録はリポジトリの外に置く
- 子どもの名前は外部の API に送らない
- CI（GitHub Actions、x86）ではハードを使わずにテストできること
- 技術構成の確認（2026-11-03）で分かった前提:
  - OS は 64-bit の Trixie 系 Raspberry Pi OS（Python 3.13、apt の OpenCV 4.10）を推奨する。Bookworm の apt の OpenCV 4.6 では、YuNet（2023mar）が読み込めない。実機の OS は要確認
  - OpenCV と numpy は、実機では apt から入れる。CI は `opencv-python-headless>=4.10,<5` に固定する（5.x にするなら YuNet の 2026may モデルに替える）
  - SunFounder の `robot_hat` と `picarx` には依存しない。HAT は smbus2（I2C のレジスタ）と lgpio（GPIO）の上の自前の薄い部品で操作する（`rover-core` の設計で決定。robot_hat は読み込みの副作用が重く、スレッドから安全でない）。RPi.GPIO は Pi 5 で動かない
  - smbus2・lgpio・picamera2・libcamera はファイルの先頭で読み込まない（CI で読み込めない）。実機用のアダプタの中だけで読み込む
  - 声の再生（PipeWire）には、ユーザーのリンガリングが必要。画面のない環境では WirePlumber の設定も要る場合がある
  - faster-whisper の速さは実機で測る（`base` int8 で 5 秒の音声に 1.5〜3 秒の見込み。日本語の精度が足りなければ `small`）。冷却ファン（GPIO のスペーサー付き）が要る

## Direct Implementation Candidates
- [x] LICENSE と第三者の表記 -- 公開リポジトリに LICENSE がない（今は「全権利留保」の扱い）。robot_hat が GPL-3.0 のため、GPL-3.0-or-later が最も安全。README に YuNet（MIT）・SFace（Apache-2.0）・tohoku-f01（CC BY 4.0）・robot_hat（GPL-3.0）の表記を足す。ライセンスの選択は人が決める
- [x] CI のアクションの更新 -- `gitleaks/gitleaks-action@v2` と `actions/checkout@v4` は Node 20 で、サポートが終わっている。v3 と v7 に上げる

## Boundary Strategy
- **Why this split**:
  - 依存の向きを「機能 → 基盤」だけにする。基盤は個々の機能を知らない
  - 共有資源ごとに担当を 1 つにする: ハード（モーター・サーボ・LCD・声・ボタン・LED）は `rover-core`、音声の入力と振り分けは `voice-gateway`、カメラと顔の認識は `presence`
  - 機能のスペックは、利用者（保護者と子ども）が読んで分かる単位にする
- **Shared seams to watch**:
  - `rover-core` の「機能を足す口」と資源の調停: 全機能が使う。優先度の申告方法と、割り込み・中断のルールは最初に固める
  - `voice-gateway` の言葉の登録と、当てはまらない言葉の受け手（`conversation`）
  - `presence` が下流に出すカメラの機能（動きの量・静止画・映像）: `daruma-game` と `remote-control` が使う。下流の都合を入れず、汎用の形で出す
  - 1 日の利用上限など、再起動をまたいで残す状態の保存先（`rover-core` の保存の仕組みを使う）

## Specs (dependency order)
- [x] rover-core -- ロボット本体の基盤。起動と安全な停止、設定と保存先、LCD・声・ボタン・サーボ・モーターの操作、資源の調停、機能を足す口、待機画面、PC と CI でのテスト。Dependencies: none
- [x] device-deployment -- 実機への配置。タグの版を入れる、動作確認、失敗時の切り戻し、実機の初期設定。Dependencies: rover-core
- [x] daily-routine -- 生活習慣の声かけ。時刻の声かけ、予告、催促、ほめる、星、おやすみモード。Dependencies: rover-core
- [x] voice-gateway -- 音声の入口。ボタンで聞き取り、言葉に合う機能へ渡す。Dependencies: rover-core
- [x] presence -- 見つけてあいさつ。顔の検出と追従、覚えた顔の認識とあいさつ、顔の登録と削除、カメラの機能の提供。Dependencies: rover-core
- [x] conversation -- おしゃべり。他の機能に当てはまらない言葉を Claude との会話で受ける。子ども向けの安全対策と利用上限。Dependencies: rover-core, voice-gateway
- [x] daruma-game -- あそび「だるまさんがころんだ」。Dependencies: rover-core, voice-gateway, presence, daily-routine
- [x] remote-control -- スマホのラジコン。家の Wi-Fi からの操作、映像、写真、話させる。Dependencies: rover-core, presence

## Progress Notes
- 進め方: スペックを 1 つずつ、要件 → 設計 → タスク → 実装 → PR の順に、各段階で人が承認してから次へ進む
- 実機で日課が動くのは `rover-core`・`device-deployment`・`daily-routine` が終わった時点

---
_created_at: 2026-11-03_
