# Research & Design Decisions

## Summary
- **Feature**: `rover-core`
- **Discovery Scope**: New Feature（グリーンフィールド。full discovery）
- **Key Findings**:
  - SunFounder `robot_hat`（2.5.x）は、読み込むだけで重い依存（pygame、pyaudio）と副作用（`/proc/device-tree` の読み取り、PWM を作るたびの `i2cdetect` の実行、オブジェクトごとのログハンドラ）がある。スレッドから安全に使えず、I2C のエラーを 5 回の再試行の後に黙って捨てる
  - HAT のマイコンは PWM の値をレジスタに保つ。そのため、プロセスが落ちてもモーターは回り続けるとみられる（未検証）。プロセスの中の停止処理だけでは足りない
  - Python は既定で SIGTERM を受けると `finally` を実行せずに終わる。systemd のユーザーサービスの停止（既定は SIGTERM。90 秒後に SIGKILL）に備えて、ハンドラが必要

## Research Log

### SunFounder robot_hat 2.5.x と picar-x 2.1.x
- **Context**: 要件 1.1、2.x、5.x、7.6、8.x。`picarx` に依存せずに、PiCar-X を動かす部品を書くため
- **Sources Consulted**:
  - https://github.com/sunfounder/robot-hat/tree/2.5.x/robot_hat（`device.py`、`pin.py`、`pwm.py`、`servo.py`、`motor.py`、`i2c.py`、`utils`）
  - https://github.com/sunfounder/picar-x/blob/2.1.x/picarx/picarx.py
- **Findings**:
  - **I2C の PWM**:
    - HAT のマイコンは I2C の 0x14 にある（予備は 0x15、0x16）
    - PWM は 20 チャンネル。チャンネルの値はレジスタ 0x20+ch、プリスケーラは 0x40+timer、周期（ARR）は 0x44+timer に書く
    - タイマーはチャンネルの組で共有している（P0–3、P4–7、P8–11、P12–15 …）。CLOCK は 72 MHz
  - **サーボ**:
    - 50 Hz、周期 4095、500〜2500 µs で −90〜90° を表す
  - **GPIO（lgpio）**:
    - USR ボタンは BCM 25（プルアップ、押すと 0）、LED は BCM 26、MCU リセットは BCM 5
    - 引数なしの `value()` は、ピンを黙ってプルなしの入力に変えてしまう（robot_hat の落とし穴）
  - **モーター**:
    - HAT v4: PWM ＋ 方向ピン（TC1508S）
    - HAT v5: PWM を 2 本使う（TC618S）
  - **PiCar-X の割り当て**:
    - 首のパンは P0、チルトは P1、ステアリングは P2
    - 左モーターは P13 ＋ D4（BCM 23）、右モーターは P12 ＋ D5（BCM 24）
    - モーターの PWM は周期 4095、プリスケーラ 10（約 1.76 kHz）
    - 速さ s（1〜100）を、デューティ `|s|/2 + 50` % に変える
    - 右モーターは左右が反転して付いている（前進は M1=+s、M2=−s）
    - 曲がるときは、内側の車輪を `(100−|角度|)/100` 倍にする
  - **動かせる範囲**: ステアリング ±30°、パン ±90°、チルト −35〜65°。パンとチルトは符号が反転している（`angle(-(v - cali))`）
  - **調整値**: picarx は `/opt/picar-x/picar-x.conf` に置く（root が必要）
  - **初期化**: MCU リセット（BCM 5 を 10 ms Low → High）の後、0.2 秒待つ
  - **スピーカーの有効化**: HAT v4 は BCM 20、v5 は BCM 12（`pinctrl set N op dh`）。HAT の版は `/proc/device-tree/*hat*/uuid` で見分ける（v5 の UUID は `9daeea78-0000-076e-0032-582369ac3e02`）
- **Implications**:
  - 自前で薄い HAT の部品（I2C のレジスタ書き込み＋lgpio）を書き、1 つのロックで直列にする。書き込みの失敗は例外にする
  - リポジトリは GPL-3.0-or-later なので、GPL-3.0 の robot_hat を参考にした実装でもライセンス上の問題はない

### プロセスが落ちたときのモーター
- **Context**: 要件 2.1、2.4、2.5
- **Findings**:
  - PWM はマイコンのレジスタが保ち、robot_hat には終了時の処理も監視（watchdog）もない
  - SIGKILL や電源以外の異常終了では、最後の速さのまま走り続けるとみられる（未検証）
- **Implications**:
  - 3 重の対策をとる
    1. プロセスの中のシグナルハンドラで直ちにモーターを 0 にする
    2. systemd の `ExecStopPost=` で、小さな安全停止のコマンドを必ず実行する。異常終了の後にも動く
    3. 起動時に MCU をリセットする
  - 実機で、プロセスを SIGKILL したときの振る舞いを確かめる（タスクに入れる）

### systemd のユーザーサービスの停止
- **Sources**:
  - https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html
  - https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html
  - https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
- **Findings**:
  - 既定は `KillSignal=SIGTERM`、`TimeoutStopSec`＝90 秒（過ぎると SIGKILL）
  - `Restart=on-failure` は、0 以外の終了コードや異常なシグナルのときに再起動する。`systemctl stop` の後は再起動しない
  - `RestartPreventExitStatus=` で、特定の終了コードのときは再起動させないようにできる
  - `$XDG_RUNTIME_DIR`（`/run/user/UID`）は、ユーザーのサービスに必ず設定される。`RuntimeDirectory=` で専用のディレクトリを作れる
  - 起動時に動かすには `loginctl enable-linger` が必要
- **Implications**:
  - ユニットは次の設定にする
    - `KillMode=mixed`（子プロセスの open_jtalk・paplay は最後に SIGKILL）
    - `TimeoutStopSec=15s`（要件 2.2 の 10 秒に余裕を持たせる）
    - `Restart=on-failure`、`RestartPreventExitStatus=75`（すでに動いているとき）
    - `ExecStopPost=`（安全停止）、`RuntimeDirectory=osekkai`

### 時刻の同期の判定
- **Sources**:
  - https://www.freedesktop.org/software/systemd/man/latest/systemd-timesyncd.service.html
  - https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.timedate1.html
  - https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#real-time-clock-rtc
- **Findings**:
  - 一般ユーザーでも `timedatectl show -p NTPSynchronized --value` で `yes`/`no` を得られる（chrony でも有効）
  - timesyncd を使っていれば、`/run/systemd/timesync/synchronized` ファイルも使える
  - Pi 5 には RTC があるが、電池がないと電源を切ったあと時刻を保てない
  - timesyncd は起動時に、時計を前回の時刻まで進める。そのため、同期の前でも「それらしいが古い」時刻になる
- **Implications**:
  - 判定には `timedatectl` を使い、使えないときは `synchronized` ファイルを見る
  - 同期するまでは 30 秒ごとに確かめ、同期したら知らせる

### 単一の実行と原子的な書き込み
- **Findings**:
  - 単一の実行: `$XDG_RUNTIME_DIR/osekkai/` のファイルに `fcntl.flock(LOCK_EX|LOCK_NB)` をかけ、プロセスが続くあいだ保つ（プロセスが死ねば自動で外れる）
  - 原子的な書き込み:
    1. 同じディレクトリに一時ファイルを作る
    2. 書いて、`flush` と `os.fsync` をする
    3. `os.replace` で置き換える
    4. ディレクトリにも `fsync` をする
- **Sources**: https://docs.python.org/3/library/os.html#os.replace

### 設定の検証
- **Findings**:
  - pydantic 2.14.2（MIT、Python 3.10 以上）。pydantic-core には aarch64 の cp311 と cp313 の wheel がある
  - Bookworm の apt の `python3-pydantic` は v1 なので使わない
  - msgspec は速いが、制約とエラーの表現が弱い
- **Implications**: 仮想環境に PyPI から pydantic v2 を入れる。各機能が `BaseModel` で自分の設定を定める

### XDG のディレクトリ
- **Findings**:
  - Linux だけなら、環境変数（`XDG_CONFIG_HOME` など）と既定値を自分で読めば、十数行で済む。相対パスの値は無視する（仕様）
  - platformdirs は不要
- **Sources**: https://specifications.freedesktop.org/basedir-spec/latest/

### Open JTalk と PipeWire
- **Findings**:
  - `open_jtalk` は標準入力の**最初の 1 行**（1023 バイトまで）だけを読む。オプションは `-x`、`-m`、`-ow`、`-r`、`-fm`、`-g`
  - PipeWire では `paplay --device=<sink>` で再生先を選べる。シンクの名前は `pactl list short sinks` で分かる
- **Implications**:
  - 改行を取り除き、長い文章は文の区切りで分けて合成する
  - 再生先は設定で選べるようにする（空なら既定のシンク）

### journald へのログ
- **Findings**:
  - サービスの標準エラーは journal に入る。`$JOURNAL_STREAM` があれば journal に出ていると分かる
- **Implications**:
  - journal のときは時刻なしの形式、端末のときは時刻ありの形式で出す
  - `PYTHONUNBUFFERED=1` にする

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| ポートとアダプタ＋資源のリース（採用） | デバイスはポートの裏に隠す。機能は調停役から資源を「借りる」 | テストしやすい。取り合いを 1 か所で調停できる。機能を足しても基盤を書き換えない | リースの API を、機能を書く人が理解する必要がある | `tech.md` の方針どおり |
| イベント駆動（asyncio） | 全体を 1 つのイベントループで動かす | スレッドの競合がない | 声の合成・録音・カメラなど、止まる処理が多い。機能を書く人の負担が大きい | 却下 |
| サンプルの中央ループ（tick） | メインループが優先順位どおりに機能を呼ぶ | 単純 | 中央が全機能を知る。機能追加のたびに書き換える | 却下（roadmap の決定） |

## Design Decisions

### Decision: HAT の操作は自前の薄い部品で行い、robot_hat を使わない
- **Context**:
  - 要件 1.6、2.x、4.1。`tech.md` では「robot_hat を GitHub から入れる」と決めていた
  - 調査で、読み込み時の副作用、スレッドから安全に使えないこと、エラーを黙って捨てることが分かった
- **Alternatives Considered**:
  1. robot_hat をアダプタの中で使い、こちらでロックをかける
  2. smbus2 と lgpio の上に、HAT のレジスタを書く薄い部品を自前で書く
- **Selected Approach**: 2
  - I2C の PWM レジスタ、サーボの角度とパルス幅の変換、モーターの方式（PWM＋方向ピン、または PWM 2 本）、MCU リセット、スピーカーの有効化を、1 つのロックで直列にして実装する
- **Rationale**:
  - 依存が減る（pygame、pyaudio、i2c-tools が要らない）
  - 書き込みの失敗を検出でき、スレッドから安全に使える
  - 安全停止のコマンドを小さく作れる
- **Trade-offs**:
  - SunFounder の今後の修正（例: Pi 5 の gpiochip の判定）を、自分で追う必要がある
  - レジスタの仕様は robot_hat のコードを参照する
- **Follow-up**:
  - `tech.md` の該当箇所を更新する（このスペックの PR に含める）
  - 実機で HAT の版とモーターの方式を確かめる

### Decision: 資源は「リース」で貸し、操作の口はリースからだけ得られる
- **Context**: 要件 4.x、5.x。サンプルでは、スレッド同士がモーター・声・LCD を直接取り合っていた
- **Selected Approach**:
  - 調停役 `Arbiter` が資源の集合をまとめて貸す（全部か無しか）
  - 声・画面・LED・首・走行・ボタンの操作の口は、`Lease` からしか得られない
  - 中断の通知は、イベントとコールバックで行う。2 秒たっても手放さなければ、強制的に取り上げる。取り上げた後のリースの口は `LeaseRevoked` を投げる
- **Rationale**: 要件 4.9（資源を持たない機能は操作できない）を、型と API で保証できる
- **Trade-offs**: 機能のコードは、`LeaseRevoked` を受けて片付ける書き方が必要になる

### Decision: 機能はエントリーポイントで見つける
- **Context**: 要件 3.1、3.2（基盤は機能の名前を持たない）
- **Alternatives**:
  1. 基盤に機能の一覧を書く
  2. 設定ファイルにクラスのパスを書く
  3. Python のパッケージのエントリーポイント（`osekkai.features`）
- **Selected Approach**: 3。`pyproject.toml` に `[project.entry-points."osekkai.features"]` を足すだけで登録できる。テストでは、機能のリストを直接渡せる
- **Rationale**: 標準の仕組み（`importlib.metadata`）を使える。基盤のコードを書き換えずに済む

### Decision: 待機画面は、最も低い優先度の内部の持ち主として画面を借りる
- **Context**: 要件 9.1（どの機能も画面を使っていないとき）
- **Selected Approach**:
  - 待機画面は優先度 0 の内部の持ち主として、画面の資源を借りる
  - どの機能が画面を求めても（機能の優先度は 1 以上）、待機画面は中断される
  - 画面が空いたら、待機画面が借り直す
- **Rationale**: 調停の仕組みを使い回せ、特別な扱いが要らない（Generalization）

### Decision: 設定の検証は pydantic v2。XDG は自前で読む
- **Context**: 要件 11.x、12.1
- **Rationale**:
  - pydantic なら、機能ごとに既定値と範囲を宣言でき、エラーの場所も分かる
  - XDG は十数行で読め、依存を増やさずに済む

### Decision: 優先度は基盤が定める帯から選ぶ（設計の確認で追加）
- **Context**: `/kiro-validate-design` の指摘。優先度が「1 以上の整数」だけだと、8 つのスペックが勝手に値を選び、「日課が最優先」を保証できない
- **Selected Approach**: `Priority` の帯（SCHEDULED 80 > INTERACTIVE 50 > REMOTE 30 > BACKGROUND 10、IDLE 0 は内部）を基盤が定める。帯は振る舞いの種類で、機能の名前を含まない。同じ帯の中では 0〜9 を足して順番を付けられる
- **Rationale**: 基盤は機能を知らないまま、機能の作者が迷わず値を選べる。`product.md` の優先の原則と一致する

### Decision: 段階のある動きは段階ごとに中断を確かめる（設計の確認で追加）
- **Context**: `/kiro-validate-design` の指摘。ダンスなどが 1 回の呼び出しで 1.5 秒続き、取り上げた後も首とタイヤを動かし続けるおそれがあった（サンプルの不具合と同じ）
- **Selected Approach**: MotionService の段階のある動きは `should_continue` を段階ごとに確かめ、偽なら止めて中止する

### Decision: 安全停止はモーターの方式によらず止める（設計の確認で追加）
- **Context**: `/kiro-validate-design` の指摘。HAT v5（PWM 2 本）のチャンネルが未確認で、P12・P13 を 0 にするだけでは止まらないおそれがある
- **Selected Approach**:
  - `safe_stop` は、P12〜P15 を 0 → 方向ピン Low → MCU リセット、の順に行う
  - モーターのアダプタは、実機で割り当てを確かめる調査の後に作る

## Risks & Mitigations
- **プロセスが落ちても PWM が残る** — `ExecStopPost` の安全停止と、起動時の MCU リセットで防ぐ。実機で SIGKILL を試して確かめる
- **HAT v5 のモーターの方式の違い** — 設定で `auto`（UUID で判定）、`pwm_dir`、`dual_pwm` を選べるようにする。実機で確かめる
- **lgpio がスレッドから安全に使えるか不明** — GPIO の操作も、HAT の部品の 1 つのロックで直列にする
- **`pinctrl` と GPIO の権限** — 利用者を `gpio` グループに入れる（`device-deployment` の初期設定）。スピーカーの有効化は lgpio で行い、`pinctrl` に頼らない
- **時刻の同期の判定に使うコマンドがない環境** — `timedatectl` が失敗したら `synchronized` ファイルを見る。どちらも使えなければ「確かでない」として扱い、ログに残す

## References
- [sunfounder/robot-hat 2.5.x](https://github.com/sunfounder/robot-hat/tree/2.5.x) — HAT のレジスタ、ピン、モーターの方式
- [sunfounder/picar-x 2.1.x picarx.py](https://github.com/sunfounder/picar-x/blob/2.1.x/picarx/picarx.py) — PiCar-X の割り当てと調整値
- [systemd.kill](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)、[systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、[systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
- [org.freedesktop.timedate1](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.timedate1.html) — 時刻の同期
- [XDG Base Directory](https://specifications.freedesktop.org/basedir-spec/latest/)
- [open_jtalk.c（Debian 1.11-5）](https://sources.debian.org/src/open-jtalk/1.11-5/bin/open_jtalk.c) — 入力は 1 行
- [pydantic](https://pypi.org/project/pydantic/)
