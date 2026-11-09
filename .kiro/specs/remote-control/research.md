# Research & Design Decisions: remote-control

## Summary
- **Feature**: `remote-control`
- **Discovery Scope**: New Feature（新しい外部の口: Web の画面、超音波と床センサー、USB カメラ）
- **Key Findings**:
  - 基盤には、センサーを読む口（ADC の読み取り、GPIO の入力）がない。`rover-core` の設計で、超音波と床センサーはこのスペックが持つと決めてある。機能が自分のアダプタで `smbus2` と `lgpio` を開く（presence がカメラを自分で開くのと同じ形）
  - `presence.api` のカメラの貸し出しは、調停の外にある。首のカメラの映像を貸し出しのまま放っておくと、だるまさん（優先度が上）がカメラを借りられない。首のカメラを見ている間は首（NECK）のリースを持ち、中断の知らせでカメラも返す
  - Bookworm の apt の Flask は 2.2 で古い。extras `remote`（`flask>=3.1,<3.2`）で入れる。werkzeug の `make_server(threaded=True)` は要求のスレッドを daemon にするので、映像の配信は止める合図を自分で見て終える

## Research Log

### 基盤の HAT とセンサー
- **Context**: 前の物の距離と床の段差を読む口があるか
- **Sources Consulted**: `src/osekkai/core/ports.py`、`src/osekkai/adapters/pi/{hat,i2c_bus,gpio,devices}.py`、`.kiro/specs/rover-core/design.md`
- **Findings**:
  - `HatPort` は `reset()` だけ。`I2cBus` は書き込みだけで、読み取りの関数がない。`Gpio` は出力と立ち下がりの監視だけ
  - LCD と HAT のマイコンは同じ `I2cBus`（同じロック）を使う
  - `FeatureContext` はデバイスを渡さない。機能の読み込みの向きのテストは、機能から `osekkai.adapters` を読むのを禁じている
  - `rover-core` の設計は、超音波と床センサーを基盤の範囲の外とし、`presence` か `remote-control` が持つとしている
- **Implications**: このスペックが自分のアダプタ（`features/remote/adapters/sensors.py`）を持つ。基盤は変えない

### robot_hat 2.5.x と picarx 2.1.x のセンサーの仕様（参照だけ。実行時には使わない）
- **Sources Consulted**: https://github.com/sunfounder/robot-hat/tree/2.5.x/robot_hat（`modules.py`・`adc.py`・`device.py`）、https://github.com/sunfounder/picar-x/blob/2.1.x/picarx/picarx.py
- **Findings**:
  - 超音波: trig は D2（BCM 27）、echo は D3（BCM 22）。10 µs のパルスの後、echo が上がって下がるまでの時間 × 343.3 m/s ÷ 2。全体の待ちは 20 ms。echo が上がらない・下がらないと -1
  - ADC: アドレス 0x14（だめなら 0x15）。チャンネル n のレジスタは `(7 - n) | 0x10`（A0=0x17、A1=0x16、A2=0x15）。`[reg, 0, 0]` を書いてから 2 バイト読む。値は 0〜4095
  - 床センサー: A0〜A2 の 3 つ。picarx の段差の既定の基準は `[500, 500, 500]`。どれか 1 つでも値が基準以下なら段差（反射が少ない）
  - gpiochip の番号は、Pi 5 のカーネルの版で変わる。sysfs の `of_node/compatible`（`raspberrypi,rp1-gpio`）か label `pinctrl-rp1` で探す
- **Implications**:
  - ADC の読み取りは、書き込みと読み取りを 1 回の `i2c_rdwr`（2 つのメッセージ）で行う。カーネルが 1 回の転送として扱うので、基盤の LCD や PWM の書き込みが間に入らない。マイコンがこの読み方（繰り返しスタート）に答えるかは、実機の道具（要件 14）で最初に確かめる
  - **実機で確かめた（2026-11-08）**: 1 回の転送の読み方は 0 0 を返した。書き込みと読み取りを別々の転送にすると正しい値（床で 1230〜1500）が返る。2 回読んで差が 64 以下のときだけ使う形にした。実機の lgpio（131584）には `gpio_trigger` がないので、trig は `gpio_write` で上げ下げする
  - 段差の基準値は設定（`cliff_reference`、既定 500）にする

### lgpio での超音波の時間の測り方
- **Sources Consulted**: https://abyz.me.uk/lg/py_lgpio.html、HC-SR04 のデータシート（周期 60 ms 以上、物がないと echo が約 38 ms 上がったまま）
- **Findings**:
  - `gpio_claim_alert` と `callback` は、端の時刻（ns）をカーネルが記録する。Python のスレッドの遅れが距離に乗らない
  - Python の busy-wait は、GIL のため 1 ms（約 17 cm）ずれることがある
- **Implications**:
  - echo の上がりと下がりの時刻の差で距離を出す。待ちは 30 ms、測る間は 60 ms 以上あける
  - 0.1 秒ごとの確かめ（要件 5.1）に間に合う
  - 「上がったが 30 ms で下がらない」は前に物がない（遠い）として安全とする。「上がらない」は読み取りの失敗として危険とする

### Flask と werkzeug
- **Sources Consulted**: https://pypi.org/pypi/flask/json、https://sources.debian.org/api/src/flask/、https://github.com/pallets/werkzeug/blob/3.1.3/src/werkzeug/serving.py
- **Findings**:
  - Flask 3.1.3（PyPI）。Trixie の apt は 3.1.1、Bookworm は 2.2.2
  - `make_server(host, port, app, threaded=True)` を別のスレッドで `serve_forever()` し、止めるときは別のスレッドから `shutdown()` する
  - 要求のスレッドは daemon で、`shutdown()` は待たない。続く応答（映像）は、自分で止める合図を見て終えないと残る。要求を 1 行ずつログに出す（端末のアドレスを含む）
  - `test_client()` で、通信なしに画面と API と配信を確かめられる
- **Implications**:
  - extras `remote = ["flask>=3.1,<3.2"]`。配置の extras に `remote` を足す
  - 要求のログを出さない要求ハンドラにする（要件 2.8）
  - 映像の配信は、止める合図を 0.5 秒ごとに見る

### ブラウザでの映像（MJPEG）
- **Sources Consulted**: WebKit のメーリングリスト（2015、2019）、https://bugs.webkit.org/show_bug.cgi?id=23402
- **Findings**:
  - `<img>` の `multipart/x-mixed-replace` は、Android の Chrome と iOS の Safari で表示できる
  - Safari では、`<img>` を消しても接続が残ることがある。画面が隠れて戻ると、映像が止まったままになることがある
  - 部分ごとに Content-Length を付けると Safari で安定する
- **Implications**:
  - 画面が隠れたら `src` を空にし、戻ったら付け直す
  - 同じカメラは 3 つの端末まで（要件 8.2）。1 秒に 10 枚まで

### USB カメラ（Logicool C270）
- **Sources Consulted**: OpenCV の V4L2 の資料、https://github.com/loyd/rscam/issues/20
- **Findings**:
  - `/dev/v4l/by-id/usb-046d_0825_*-video-index0` を `cv2.CAP_V4L2` で開き、MJPG 640×480 にする
  - `CAP_PROP_CONVERT_RGB=0` でカメラの JPEG をそのまま受け取れる
  - C270 の JPEG はハフマン表（DHT）を含まない。ブラウザは表示できるが、保存した写真をほかのソフトが開けないことがある。標準の表を差し込めば直る
  - 映像（UVC）とマイク（UAC）は別のインターフェースで、同時に使える
- **Implications**:
  - JPEG に DHT がなければ、標準の表を差し込む
  - `CONVERT_RGB` が効かない版では、`cv2.imencode` で JPEG にする
  - 音声の入口のマイクと同時に使う

### 家のネットワークの判定
- **Sources Consulted**: https://docs.python.org/3/library/ipaddress.html、CVE-2024-4032
- **Findings**: `ipaddress.is_private` の範囲は、Python 3.11〜3.13 のパッチの版で変わった
- **Implications**: 決まった範囲（10/8、172.16/12、192.168/16、127/8、::1、fc00::/7、fe80::/10。IPv4 を写した IPv6 は IPv4 に直す）を自分で持つ

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| 機能がセンサーのアダプタを持つ | `features/remote/adapters/` で `smbus2`・`lgpio`・`cv2` を関数の中で読む | 基盤を変えない。presence と同じ形 | I2C の別の口を開く。ADC の読み方を実機で確かめる | 採用 |
| 基盤にセンサーの口を足す | `HatPort` に ADC の読み取りを足し、`FeatureContext` から渡す | I2C の口が 1 つ | `rover-core` の再確認が要る。範囲の外と決めたことに反する | 不採用 |
| 写真と映像を 1 つの貸し出しで | 首のカメラは映像の貸し出しから静止画も撮る | 開け閉めが減る | 見る端末がないときは借り直す | 採用 |

## Design Decisions

### Decision: 首のカメラを見ている間は首のリースを持つ
- **Context**: カメラの貸し出しは調停の外。だるまさんが資源を借りた後、カメラを借りようとして `CameraBusy` になる
- **Alternatives Considered**:
  1. 映像だけならリースを持たない — だるまさんがカメラを借りられない
  2. `presence.api` に優先度のある貸し出しを足す — 上流の変更になる
  3. 首のカメラを見ている間は NECK だけのリースを持ち、中断の知らせでカメラを返す
- **Selected Approach**: 3。操作する端末があるときは DRIVE・NECK・VOICE・DISPLAY、首のカメラを見る端末だけのときは NECK、どちらもないときはリースなし。持つ資源が変わるときは、今のリースを返して借り直す
- **Rationale**: 調停の仕組みだけで、優先度の高い機能にカメラも譲れる。presence は、カメラを貸している間は顔を探さないので、首を持たれても困らない
- **Trade-offs**: 要件 12.4 を「操作する端末がなくなったら、走行・声・LCD を返す。首のカメラを見ている端末もなければ首も返す」に直した（設計のときの直し。保護者の承認を設計と一緒にもらう）
- **Follow-up**: だるまさんを始めたとき、ラジコンの映像を見ていてもだるまさんがカメラを借りられることを、通しのテストで確かめる

### Decision: ログインの状態は、サーバーの中の表で持つ
- **Context**: 起動し直したらログインを無効にする（2.6）。12 時間で切る。「おわる」で切る
- **Alternatives Considered**:
  1. Flask の署名付きクッキー（サーバーの中に状態がない） — 個別に無効にできない。鍵を保存しないと起動ごとに変わるが、それ以外の無効化ができない
  2. 乱数の印をクッキーに入れ、サーバーの中の表で持つ
- **Selected Approach**: 2。印は `secrets.token_urlsafe(32)`、表はメモリだけ。保存しない
- **Rationale**: 起動し直すと表が空になる。12 時間と「おわる」を表で切れる。鍵を保存しなくてよい

### Decision: まちがいの数え方は、1 つのロックの中で数える
- **Context**: サンプルは、まちがいごとに 1 秒寝るだけで、並列の要求には効かない
- **Selected Approach**: `LoginGuard` が 1 つのロックの中で、続いたまちがいの数と、止める期限を持つ。止めている間は pin を確かめない（5 分）。どの端末からでも同じ数え
- **Rationale**: 同時に何度送られても 1 回ずつ数える（2.4）。総当たりは、5 回ごとに 5 分止まる（4 けたなら最悪で約 7 日）

### Decision: ダンスは自分で段階を進める
- **Context**: 基盤の `NeckHandle.dance()` は、止める合図を受け取れない（リースの中断だけを見る）。「とめる」でもダンスを止める（7.6）
- **Selected Approach**: サンプルと同じ動き（6 段 × 0.25 秒、首を左右 30 度・上 15 度）を、`NeckHandle.look()` で 1 段ずつ行い、段ごとに止める合図を見る
- **Rationale**: 基盤を変えない。タイヤを動かさない（走行の口を使わない）

### Decision: 後退の制限と前の安全は、純粋な関数にする
- **Selected Approach**: `drive_rules.py` に、命令・センサーの読み・時刻から「どう走るか」を決める関数を置く。走行の 0.1 秒の繰り返しは、この関数の結果を走行の口に渡すだけ
- **Rationale**: 偽の時計と偽の読みで、すべての止まり方を PC で確かめられる（要件 15）

### Decision: 写真の名前と整理
- **Selected Approach**: `YYYYMMDD-HHMMSS-NN-<neck|usb>.jpg`（NN はその秒の通し番号）。保存先は `Paths.data_dir / "remote" / "photos"`（0700、ファイルは 0600）。名前の時刻で、決めた日数より古いものを消す。時刻が確かでないときは消さない
- **Rationale**: 重ならない（9.5）。キャッシュの場所（大きさで消される）を使わない

### Decision: 1 日の上限を持たない
- **Context**: 要件づくりのときに、保護者が上限をなくすと決めた（11.6）
- **Selected Approach**: 記録の保存（`StateStore`）を使わない。保存するのは写真だけ

## Synthesis
- **一般化**: 「いまラジコンを使えるか」（時間帯の外・おやすみ・時刻が確かでない・ほかの機能が使っている・ほかの端末が操作中）を、1 つの `Availability` の値と知らせの文にまとめる。画面の知らせ、操作の断り、映像の止め方が同じ値を見る
- **採用と自作**: Web は Flask（steering の決まり）、カメラは OpenCV と presence の口。センサーの読み方と安全の判定は自作（既存の部品は robot_hat で、使わない決まり）
- **簡素化**:
  - 1 日の上限、使った時間の記録、写真の一覧と削除の画面を作らない
  - 速さの切り替えは、操作する端末の状態ではなく、機能の中の 1 つの値にする（操作する端末が 1 台なので十分）
  - 映像は、カメラごとに 1 つの読み取りのスレッドと最新の 1 枚だけを持つ

## Risks & Mitigations
- ADC の読み取りを 1 回の転送で行う読み方に、HAT のマイコンが答えない — センサーを確かめる道具を実装の早い段階で実機に入れて確かめる。答えないときは、読み取りを 2 回して同じ値のときだけ使う形に替え、設計を直す
- 超音波の「物がない」と「壊れている」の見分けをまちがえる — 道具で、物がないとき・近いとき・抜いたときの値を確かめる
- Safari で映像が止まる — 画面が戻ったら `src` を付け直す。実機確認で iPhone も試す
- 首のカメラの映像の 10 枚/秒が presence の口に重い — 1 枚ごとの時間を測り、重ければ 1 秒に 5 枚に下げる（設定にしない）
- C270 を picamera2（libcamera）も見つける — presence は首のカメラ（1 台目）を選ぶ。実機確認で、だるまさんと映像を両方試す

## References
- [robot-hat 2.5.x](https://github.com/sunfounder/robot-hat/tree/2.5.x/robot_hat) — 超音波・ADC のレジスタ（参照だけ）
- [picar-x 2.1.x](https://github.com/sunfounder/picar-x/blob/2.1.x/picarx/picarx.py) — 段差の基準値・ピン
- [lgpio Python](https://abyz.me.uk/lg/py_lgpio.html) — 端の時刻つきの通知
- [werkzeug serving.py 3.1.3](https://github.com/pallets/werkzeug/blob/3.1.3/src/werkzeug/serving.py) — スレッドのサーバーと止め方
- [Python ipaddress](https://docs.python.org/3/library/ipaddress.html) — 範囲の判定
- サンプル `remote.py`（リポジトリの外。振る舞いの参照だけ）
