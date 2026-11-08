# Research & Design Decisions: daruma-game

## Summary
- **Feature**: `daruma-game`
- **Discovery Scope**: Extension（`rover-core`・`voice-gateway`・`presence`・`daily-routine` の上に、機能を 1 つ足す）。軽い調査（`design-discovery-light.md`）。新しい外部ライブラリはないので、Web の調査はしていない
- **Key Findings**:
  - 押下は、BUTTON を借りている機能のリースの待ち行列に入る（`ButtonRouter`）。遊びの間ずっと BUTTON を借りていれば、押下は入口の待ち手に届かず、入口は聞き取りを始めない。ただし、入口が言葉を渡してから遊びが資源を借りるまでのすき間に押されると、入口（優先度 55）が先に借りる。遊びの優先度を入口より上（`INTERACTIVE + 6` = 56）にすると、このすき間でも遊びが入口から資源を取り上げられ、遊びの間に入口が資源を取ることもない
  - `NeckHandle.dance(drive=None)` は首（pan・tilt）だけを動かし、ステアリングにもタイヤにも触らない（`MotionService.dance(drive=False)`）。遊びは DRIVE を申告しないので、走行の口を作れない。タイヤを回さないことは、申告の形で守れる（5.1）
  - 日課は、時刻が確かでない間は `next_task_at=None` を出し、タスクも始めない。日課が止まると状況は `None` になる。遊びは日課の状況だけを見て判断すればよく、時刻が確かかを自分で確かめなくてよい
  - `presence` は PC（`--fake-hardware`）ではカメラを持たない（`choose_devices` が None）。遊びの道具を PC で動かすには、遊びの側に偽のカメラの貸し出し（`CameraLoan` の形）が要る
  - roadmap の daruma-game の依存に `daily-routine` が書かれていない。`routine.api` を読むので、roadmap に足す必要がある

## Research Log

### 資源とボタンの扱い
- **Context**: 遊びの間の押下を遊びだけに使い、入口に聞き取りを始めさせない（6.1）
- **Sources Consulted**: `src/osekkai/core/buttons.py`、`core/arbiter.py`、`core/handles.py`、`voice/feature.py`、`voice/api.py` の説明
- **Findings**:
  - `ButtonRouter._on_falling`: BUTTON のリースの持ち主がいれば、その待ち行列に入れて終わり。いなければ待ち手のうち最も優先度の高いもの（入口）を呼ぶ
  - 入口は、待ち手として押下を受けたときだけ資源を借りる（`VoiceGatewayFeature._do`）。借りる資源は VOICE・DISPLAY・LED・BUTTON・NECK、優先度は 55、待つのは 2 秒
  - 入口は、言葉を渡す前に資源を返す（`ListenFlow._deliver` → `session.release()` → `handler(heard)`）
  - 入口は、中断を知らされたら聞き取りを取り消してすぐ返す（voice-gateway 1.7）
  - `ButtonHandle` は押下の時刻を返さない。`clear()`・`has_press()`・`wait_press(timeout)` だけ。押した時の区切り（唱える間と見ている間）は、区切りの時点で待ち行列を見ることで決める
  - リースの待ち行列は、リースが終わると捨てられる
- **Implications**:
  - 遊びは、始めから終わりまで VOICE・DISPLAY・BUTTON・NECK を 1 つのリースで借り続ける
  - 優先度は 56（`Priority.INTERACTIVE + 6`）。理由は「Design Decisions」

### 首とダンス
- **Context**: 首だけで踊る。タイヤを回さない（3.3、5.1）
- **Sources Consulted**: `core/handles.py`（`NeckHandle.dance`、`nod`、`look`、`home`）、`core/motion.py`、`core/config.py`（`MotionSettings`）
- **Findings**:
  - `dance(drive=None)` → `MotionService.dance(drive=False)`: pan を ±30 度、tilt を 15 度で 6 拍（0.25 秒ごと）。ステアリングは `drive` が真のときだけ。タイヤは `drive` が真で、かつ `allow_drive_in_dance` が真のときだけ。最後に首だけを正面に戻す
  - `nod(times)` は段階ごとに中断を確かめ、最後に首を正面（pan 0・tilt 0）に戻す
  - `look(pan, tilt)` は中断の知らせを見ない（リースが有効かだけ）。角度は基盤が `pan_range`・`tilt_range` に収める（既定 ±80、−30〜60）
  - NECK のリースが終わると、基盤が首を正面に戻す（`Arbiter._finish`）
- **Implications**:
  - サンプルのステアリングの動き（∓25 度）は入れない。走行の資源を借りないので作れない
  - 首の範囲（5.2）は基盤が守る。遊びは決まった角度を渡すだけ
  - 首を正面に戻すこと（4.7）は、リースを返せば基盤が必ず行う。流れは、言葉を話し終えた後に `home()` も呼ぶ

### 声と中断
- **Context**: 中断が来たら、いまの一言を言い終えてから返し、それ以上話さない（4.4）
- **Sources Consulted**: `core/handles.py`（`VoiceHandle.say`）、`core/arbiter.py`
- **Findings**:
  - 中断を知らされたリースは「中断の知らせあり」の状態で、まだ有効（`active` が真）。`say` は有効かだけを見るので、知らされた後でも話せてしまう
  - 知らせてから 2 秒で取り上げる。話し始めた後は、取り上げられても発話の途中では止めない
- **Implications**:
  - 流れは、話す・首を動かす・待つの前に、毎回「中断・静かな状態・止める旗」を確かめる（会話の `ReplyFlow` と同じ）

### カメラの貸し出し
- **Context**: 動きの量を使う（2.5）。カメラを使えないときの終わり方（1.4、4.6）
- **Sources Consulted**: `features/presence/api.py`、`watcher.py`、`feature.py`、`checks.py`、`cli.py`、`adapters/opencv.py`
- **Findings**:
  - `borrow_camera(owner, alive=...)` → `CameraLoan`。presence が動いていない・カメラがない → `CameraNotAvailable`、ほかが借りている → `CameraBusy`
  - `loan.motion(seconds)`: 見るスレッドが 1 秒に 12 回撮って比べ、「続く 2 つの小さい方の最大」を返す（1 回だけの大きな値は数えない）。待つのは `seconds + 5` 秒まで。時間切れは `TimeoutError`。途中でカメラが使えなくなれば `CameraNotAvailable`、貸し出しが終わっていれば `CameraNotBorrowed`、そのほかの失敗はその例外
  - 動きの量は「160×120 の灰色でぼかし、明るさの差が 25 を超えた点の割合」。サンプルの「変わった点の割合」と同じ種類なので、既定のしきい値 0.01 をそのまま使う
  - 貸している間、presence は顔を探さず、首も動かさない。首のリースは遊びが NECK を借りるときに取り上げられる。あいさつは声と画面を待たずに借りるので、遊びが持っている間はできない
  - presence の `choose_devices` は、picamera2 と cv2 とモデルがなければカメラなし。PC（`--fake-hardware`）ではカメラを貸せない
- **Implications**:
  - 遊びは `borrow_camera` を包む口（`BorrowCamera`）を受け取り、本番は presence、PC の道具は台本の偽のカメラを使う
  - カメラの誤りは、`motion` のすべての例外（`CameraNotAvailable`・`CameraNotBorrowed`・`TimeoutError`・そのほか）を「カメラが使えなくなった」として扱う

### 日課の状況と時計
- **Context**: 次のタスクが近いとき・タスクの最中に始めない・終える（1.5、4.3、4.8）
- **Sources Consulted**: `features/routine/api.py`、`features/routine/feature.py`（`_tick`、`_publish`）、`core/clock.py`
- **Findings**:
  - `current_status()` は `RoutineStatus(next_task_at, in_task, asleep, updated_at)` か `None`（日課が動いていない）
  - 日課は 1 秒ごとに状況を出し直す。時刻が確かでないときは `next_task_at=None`（`known_time=False`）で、タスクも予告も始めない
  - `SystemClock.is_synchronized()` は一度確かになったら真を保つ
- **Implications**:
  - 判定は「状況が None → 予定なし」「タスクの最中 → 近い」「`next_task_at` が None → 予定なし」「`next_task_at - now` が決めた長さより短い → 近い」
  - 時刻が確かでない間は、日課もタスクを始めないので、遊びを止める理由がない（日課が何も知らせないなら遊ぶ）。時刻が確かになると日課が `next_task_at` を出し、次の回の前の確かめで終える
  - `now` は `ctx.clock.now()`（日課と同じ時計）

### self-check の口
- **Context**: 遊べるか（カメラを使えるか）を確かめた結果を出す（7.4）
- **Sources Consulted**: `core/selfcheck.py`、`core/status.py`、`features/presence/checks.py`、`features/conversation/checks.py`、`tests/test_cli_selfcheck.py`
- **Findings**:
  - self-check は別のプロセスで動くので、presence の見る流れはない。`borrow_camera` は使えない
  - `CheckContext` には、検証した設定（`report.features`）と、動いているサービスの報告（`service`、`features` に開始した機能の名前）がある
  - サービスの報告に、presence のカメラが使えるかは入っていない。`presence.camera` はハードの項目で、サービスが動いている間は飛ばされる
  - 機能の項目は、版を戻しても直らない問題を「だめ」にせず、「よい」に理由を添える（conversation・presence の決まり）
- **Implications**:
  - `daruma.camera` は、設定（遊び・presence）とサービスの報告（presence が開始しているか）で判断する。カメラのハードそのものは `presence.camera` の役目
  - サービスが動いていないときは、遊べるかを確かめる手段がないので「飛ばした」
  - 上流の穴として記録する（Risks）

### 道具（CLI）の形
- **Context**: 手で遊びを始める道具と、判定を確かめる道具（8.1〜8.4）
- **Sources Consulted**: `features/presence/cli.py`（`_run_alone`、`make_motion_feature`、`InstanceLock`、終了コード 75）、`features/routine/trial.py`、`core/registry.py`、`tests/features/test_features_import_direction.py`
- **Findings**:
  - 道具は単一の実行のロックを取り、取れなければ 75 で終える。取れたら、基盤（`Runtime`）をその用の機能だけの登録で動かし、ロックをそのまま渡す
  - `osekkai.__main__.build_devices` を読み込めるのは、読み込みの向きのテストの `ENTRY_ALLOWLIST` にあるモジュールだけ
  - 機能のクラスは `FeatureRegistry()`（エントリーポイント）から名前で選べる。presence の本体（`features.presence.feature`）を読み込まずに、presence を登録できる
- **Implications**:
  - `osekkai.features.daruma.cli` を `ENTRY_ALLOWLIST` に足す
  - `--play` と `--judge-test` は、本番では presence（エントリーポイントから選ぶ）と道具用の機能の 2 つで基盤を動かす。`--fake-hardware` では presence を登録せず、台本の偽のカメラを使う

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| 純粋な決まり＋流れ＋IO の口（採用） | 回の判定と終わり方を純粋な関数にし、1 回の遊びの流れは `GameIO` の口だけを使う。本体が資源とカメラを借りて口を作る | 偽の口と固定の乱数で、すべての流れを PC でテストできる（9.1）。会話の `ReplyFlow` と同じ形 | 口の数が多い（話す・出す・首・押下・動きの量・待つ） | 口は 1 つの Protocol にまとめる |
| 状態機械のクラス（イベント駆動） | 状態とイベントの表で進める | 状態の図とそのまま対応 | 流れは一本道で、表にすると読みにくい。待ちと中断の扱いが分散する | 不採用 |
| サンプルの移植 | 1 つのクラスでハードを直接触る | 速い | 資源を直接触る問題がそのまま残る | 不採用 |

## Design Decisions

### Decision: 優先度を `INTERACTIVE + 6`（56）にする
- **Context**: 遊びの間、押下で入口が聞き取りを始めないようにする（6.1）。入口は 55
- **Alternatives Considered**:
  1. `INTERACTIVE`（50、会話と同じ）— BUTTON を借りていれば、遊びの間の押下は入口に届かない。ただし、入口が言葉を渡してから遊びが借りるまでのすき間の押下で、入口が先に資源を借りると、遊びは借りられない（入口が上なので待つだけになる）。また、すき間で遊びが先に借りた後に入口が借りに来ると、遊びが中断される
  2. `INTERACTIVE + 6`（56）— 入口より上。入口は遊びから資源を取り上げられない（待って諦め、押下を捨てる）。遊びがすき間で入口から資源を求めると、入口は中断を知らされて聞き取りを取り消す（voice-gateway 1.7）
- **Selected Approach**: 56
- **Rationale**: 6.1 を、すき間でも守れる。遊びが資源を求めるのは、言葉を受け取った直後（入口は返した後）か、ロボットを止めた道具の中だけなので、入口のふつうの聞き取りを横取りすることはない。日課（80）より下なので、日課には必ず譲る
- **Trade-offs**: 帯の中で入口より上の機能は初めて。入口を後から 56 以上にすると、この前提が崩れる（Revalidation Triggers）
- **Follow-up**: 入口と遊びを同じ基盤で動かし、遊びの間の押下で入口の聞き取りが始まらないことをテストする

### Decision: 押下の区切りは、区切りの時点で待ち行列を見る
- **Context**: 唱える間の押下は勝ち、見ている間の押下は「動いた」（2.6、3.1）。`ButtonHandle` は押した時刻を返さない
- **Alternatives Considered**:
  1. 押下の時刻（`ButtonPress.at`）で分ける — リースの口は時刻を返さない。基盤を変える必要がある
  2. 区切りで待ち行列を見る（採用）
- **Selected Approach**:
  - 唱え言葉を話し終えたら `has_press()`。真なら勝ち
  - ランダムな間は `wait_press(間の長さ)`。押されたら直ちに勝ち（サンプルより早く気づく）
  - 間が終わった後の押下は、見ている間の押下とする。振り向く（首を正面に向ける・LCD）のは「ころんだ！」の直前の一瞬なので、区切りを間の終わりに置く
  - 見終わったら `has_press()`。真なら「動いた」
  - 「動いた」の言葉と 5 秒の待ちの後に `clear()`。1 回目の前も `clear()`
- **Rationale**: 基盤を変えずに要件の区切りを守れる。区切りのずれは振り向く一瞬だけで、そこで押せたなら見られていたとみなすのが鬼のルールに合う
- **Trade-offs**: 「ころんだ！」を話し始める直前の数ミリ秒の押下も「動いた」になる
- **Follow-up**: テストで、間の終わりの直後の押下が「動いた」になることを確かめる

### Decision: 首だけで踊り、踊りと話すを同時に行う
- **Context**: 「首だけで踊りながら」話す（3.3）。タイヤを回さない（5.1）
- **Selected Approach**: `lease.neck.dance()`（走行の口を渡さない）を短命のスレッド（`daruma-dance`）で動かし、その間に勝ちの言葉を話す。話し終えたら踊りのスレッドの終わりを待つ（最大 3 秒）。踊りの中断（`LeaseRevoked`）はスレッドの中で捨てる
- **Rationale**: サンプルと同じ見え方。基盤の首の操作はロックで守られているので、別のスレッドから呼んでよい
- **Trade-offs**: スレッドが 1 本増える。踊りは 1.5 秒、言葉は約 3 秒なので、踊りが先に終わる

### Decision: 日課の状況だけで次の予定を判断する（時計の扱い）
- **Context**: 次のタスクが近いときに始めない・終える（1.5、4.3、4.8）。時刻は確かでないことがある
- **Alternatives Considered**:
  1. 時刻が確かでないときは遊ばない — 日課が何も始めないときにも遊べなくなる。日課が動いていないなら遊ぶ（4.8）と合わない
  2. 日課の状況だけを見る（採用）
- **Selected Approach**: `schedule_blocks(status, now, margin)`。`None` → 近くない。`in_task` → 近い。`next_task_at` が `None` → 近くない。`next_task_at - now < margin` → 近い
- **Rationale**: 日課は時刻が確かでない間、タスクを始めず `next_task_at=None` を出す。日課が始めないなら、遊びを止める理由はない。確かになれば、次の回の前の確かめで終える。判断の元を日課の 1 か所にそろえる
- **Trade-offs**: 日課のスレッドが止まって状況が古いままのときは、古い状況で判断する（日課は止まると `None` を出すので、ふつうは起きない）

### Decision: パッケージ名と設定の節は `daruma`
- **Context**: スペック名は `daruma-game`。structure.md に `features/daruma/` の例がある。日課（`daily-routine` → `routine`）と同じく短い名前にする
- **Selected Approach**: パッケージ `osekkai.features.daruma`、機能の名前・設定の節・保存先の名前・エントリーポイントの名前は `daruma`、self-check の項目は `daruma.camera`

### Decision: PC の道具には、遊びの側の台本の偽のカメラを使う
- **Context**: `--fake-hardware` で道具を動かす（8.4）。presence は PC でカメラを貸せない
- **Alternatives Considered**:
  1. presence に偽のカメラを足す — presence の境界を変える。このスペックの外
  2. 遊びの側に `CameraLoan` の形の偽物を置く（採用。会話の `ScriptedChatModel` と同じ考え）
- **Selected Approach**: `ScriptedCamera`（`camera.py`）。`motion(seconds)` は時計で `seconds` 秒待ち、決まった列（0.000、0.004、0.030 …）を順に返す。しきい値の上と下の両方の判定が出る
- **Trade-offs**: 偽物が製品のコードに入る。`--fake-hardware` の道具だけが使い、本番の機能は使わない

## Synthesis Outcomes
- **Generalization**: 終わり方 9 種類（勝ち・回数（ローバーの勝ち）・次の予定・カメラなし・カメラの誤り・譲る・おやすみ・止める・誤り。初めは「だれもいない」もあったが、実機の遊びのあとでやめた）を 1 つの `EndReason` にまとめ、話す言葉は `texts.END_SAYS[reason]` の表で決める。話さない終わり方は表に入れない。「次の回の前の確かめ」と「始める前の確かめ」は同じ `schedule_blocks` を使う。判定の道具も、遊びと同じ `judge_watch` を使う
- **Build vs. Adopt**: 新しいライブラリはない。資源・ボタン・首・声・カメラ・日課の状況は、すべて既存の口を使う。乱数は標準の `random.Random`（注入して固定できる）
- **Simplification**:
  - 状態機械の表は作らず、1 回の遊びを一本道の流れ（`GameFlow.play`）にし、回の数え方と終わりの判定だけを純粋な関数にする
  - 保存はしない（星も記録もない。3.5、Boundary）。`StateStore` は使わない
  - 遊びの入口は、言葉の受け手と道具の 2 つだけ。どちらも同じ `run_game`（資源を借りる → 流れ → 返す）を通る

## Risks & Mitigations
- 入口の優先度が後から 56 以上になると、遊びの間の押下で入口が資源を取る — Revalidation Triggers に書く。入口と遊びを同じ基盤で動かすテストで見張る
- self-check は、サービスが動いている間、presence のカメラが本当に使えるかを知れない（サービスの報告にカメラの状態がない）— `daruma.camera` は presence が開始しているかまでを見て、カメラのハードは `presence.camera`（ロボットを止めた self-check）に任せる。上流の穴として、presence がカメラの状態を報告に出すようになったら見直す
- presence に偽のカメラがないので、`python -m osekkai --fake-hardware` で話しかけて遊ぶと、「カメラが ないと あそべない」になる — PC で流れを見るのは `--play --fake-hardware` の道具で行う（手順書に書く）
- 動きの量の測り方（1 回だけの大きな値を数えない）がサンプルと少し違い、既定の 0.01 が部屋に合わないことがある — 判定の道具（`--judge-test`）で、保護者が部屋で確かめて設定を変える
- roadmap の依存の宣言に `daily-routine` がない — 実装の PR で roadmap を直す（Modified Files）
- 中断の後も `say` が通る（リースは有効なまま）— 流れは話す前に必ず中断を確かめる。テストで、中断の後に何も話さないことを確かめる

## References
- `.kiro/specs/rover-core/design.md` — 資源の調停、優先度の帯、ボタンの配達、首と走行
- `.kiro/specs/voice-gateway/design.md` — 言葉の登録、資源を返してから渡す、入口の優先度 55
- `.kiro/specs/presence/design.md` — カメラの貸し出し、動きの量
- `.kiro/specs/daily-routine/design.md` — 日課の状況（9.4、9.5）
- `.kiro/specs/conversation/design.md` — 直近の機能の設計の形（流れと IO の口、self-check、読み込みの向き）
- サンプルの `osekkai/games.py` — 子どもから見た振る舞い（唱え言葉・角度・間・言葉）
