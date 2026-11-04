# Requirements Document

## Project Description (Input)
**誰が困っているか**: OsekkaiRover の保護者（開発者を兼ねる）。ロボットに、仕事や生活の補佐となる機能を今後も足していきたい。子どもは、毎日このロボットと関わる。

**今の状況**: 参照実装（サンプル）では、基盤の役割がメインループと各モジュールに散らばっている。ハードの読み込み、機能の優先順位、保存先がそこに混ざっている。そのため次の問題がある。
- 機能を足すたびに、中央を書き換える必要がある
- PC ではテストできない
- 止めたとき（SIGTERM）にモーターが止まらない
- 複数のスレッドが、モーター・声・LCD を取り合う
- ボタンの押下が消費されず、ほかの機能が止まり続ける
- 設定と記録がリポジトリの中にあり、停電で記録が壊れる

**何を変えるか**: ロボット本体の基盤（rover-core）を作る。
- 実機で起動すると待機画面が出て、ボタンとサーボが反応する
- どの止まり方でも、モーターは必ず止まり、首は正面に戻る
- 新しい機能は「機能を足す口」に登録するだけで加われる。そのとき、優先度と使う資源（声・画面・モーター・首・LED・ボタン）を自分で申告し、基盤が資源の取り合いを調停する
- 設定・記録・キャッシュは、リポジトリの外に壊れにくく保存する
- 判定ロジックは、ハードなしで PC と CI でテストできる

範囲・境界・制約の詳細は `brief.md` を参照する。

## Introduction
Rover Core は、OsekkaiRover のすべての機能が共通して使う「ロボット本体の基盤」です。主な役割は次のとおりです。
- 起動と停止を安全に行う
- 声・画面・ボタン・LED・首・モーターを、機能が使える形で提供する
- 複数の機能が同時に動くときの資源の取り合いを調停する
- 設定と記録を、壊れにくく、リポジトリの外に保存する

新しい機能は、Rover Core に自分を登録するだけで加われます。Rover Core は、個々の機能の中身を知りません。

この文書の「機能」は、Rover Core に登録される個々の振る舞い（日課、あいさつ、会話、遊び、ラジコン、今後の補佐機能）を指します。
「資源」は、声の出力・画面（LCD）・LED・ボタン・首（パンとチルトのサーボ）・走行（ステアリングとモーター）を指します。

## Boundary Context
- **In scope**:
  - 起動・停止・異常時の安全な停止
  - 設定の読み込みと検証
  - 状態とキャッシュの保存
  - 声・画面・ボタン・LED・首・走行の操作
  - 資源の調停と機能の登録
  - 待機画面
  - 静かな状態（画面を消して声を控える）の切り替えの口
  - ハードなしでの実行
  - 開発用の発話の確認
- **Out of scope**:
  - 実機への配置・動作確認（self-check）・切り戻し（`device-deployment`）
  - 音声の聞き取りと言葉の振り分け（`voice-gateway`）
  - カメラと顔の認識（`presence`）
  - 個々の機能の振る舞い（日課の判定、あいさつ、会話、遊び、ラジコン）と、機能ごとの優先度の値
- **Adjacent expectations**:
  - 各機能は、登録のときに自分の優先度と使う資源を申告し、中断の通知を受けたら資源を手放す
  - `device-deployment` は、Rover Core が出す版の情報と、起動・停止の振る舞いを前提にする
  - 時刻に基づく機能（日課など）は、Rover Core が知らせる「時刻が確かかどうか」を見て、自分の振る舞いを決める

## Requirements

### Requirement 1: 起動
**Objective:** As a 保護者, I want ロボットが電源を入れるだけで使える状態になってほしい, so that 毎日操作しなくても子どもと関われる

#### Acceptance Criteria
1. When Rover Core が起動したとき, the Rover Core shall 首とステアリングを正面に戻し、モーターを止め、LED を消した状態にしてから、登録された機能を開始する
2. When 起動の準備が終わったとき, the Rover Core shall 待機画面を LCD に表示する
3. When 起動の準備が終わり、現在時刻が設定した「起動を声で知らせる時間帯」（既定は 8:00〜20:00）の中にあるとき, the Rover Core shall 起動したことを声で知らせる
4. While 現在時刻が「起動を声で知らせる時間帯」の外にあるとき, the Rover Core shall 起動したことを声で知らせない
5. When Rover Core が起動したとき, the Rover Core shall 動いている版（リリースの名前またはコミット）をログに記録する
6. If 起動時にいずれかのデバイス（LCD・声・ボタン・Robot HAT）の準備に失敗したとき, the Rover Core shall 失敗したデバイスと理由をログに記録し、そのデバイスを使えない状態として残りの起動を続ける
7. If Rover Core がすでに同じ実機で動いているときに、もう 1 つ起動されたとき, the Rover Core shall 後から起動した方ではデバイスを操作せず、すでに動いていることを知らせて終了する

### Requirement 2: 安全な停止
**Objective:** As a 保護者, I want どんな止まり方でもロボットが走り続けたり首が曲がったままになったりしないこと, so that 子どもがそばにいても安心できる

#### Acceptance Criteria
1. When 停止の要求（サービスの停止、OS からの終了の合図、キーボードからの割り込み）を受けたとき, the Rover Core shall 1 秒以内にモーターを止める
2. When 停止の要求を受けたとき, the Rover Core shall 動いている機能に停止を知らせ、首とステアリングを正面に戻し、LED を消し、LCD の表示を消してから、10 秒以内に終了する
3. If 機能が停止の知らせに 10 秒以内に応えないとき, the Rover Core shall その機能を待たずに、モーターの停止と首の復帰を済ませて終了する
4. If 基盤そのものに予期しない異常が起きたとき, the Rover Core shall モーターを止め、首とステアリングを正面に戻してから、異常終了したことが分かる形で終了する
5. When 走行の資源を持つ機能がなくなったとき, the Rover Core shall モーターを止める

### Requirement 3: 機能の登録
**Objective:** As a 開発者, I want 新しい機能を基盤に書き足さずに加えたい, so that 仕事や生活の補佐機能を少しずつ増やしていける

#### Acceptance Criteria
1. When 機能が登録されたとき, the Rover Core shall その機能の名前・優先度・使う資源を受け付け、起動時にその機能を開始する
2. The Rover Core shall 個々の機能の名前や優先順位を、基盤の側にあらかじめ持たない
3. Where 設定でその機能が無効にされているとき, the Rover Core shall その機能を開始しない
4. If 機能の開始に失敗したとき, the Rover Core shall 失敗した機能と理由をログに記録し、ほかの機能を開始して動き続ける
5. If 動いている機能に予期しない異常が起きたとき, the Rover Core shall その機能を止め、その機能が持っていた資源を取り戻し（モーターは止める）、ほかの機能を動かし続ける
6. If 同じ名前の機能が 2 つ登録されたとき, the Rover Core shall 後から登録された方を受け付けず、その理由をログに記録する

### Requirement 4: 資源の調停
**Objective:** As a 保護者, I want 日課の声かけ中にラジコンが走り出したり、声が重なったりしないこと, so that 子どもが混乱せず、安全に使える

#### Acceptance Criteria
1. The Rover Core shall 声の出力・画面・LED・ボタン・首・走行の各資源を、同時に 1 つの機能だけに使わせる
2. When 機能が誰も使っていない資源を求めたとき, the Rover Core shall その資源をその機能に使わせる
3. When 機能が、自分より優先度の低い機能が使っている資源を求めたとき, the Rover Core shall 低い方の機能に中断を知らせ、手放された資源を高い方の機能に使わせる
4. If 中断を知らされた機能が 2 秒以内に資源を手放さないとき, the Rover Core shall その資源を取り上げて高い方の機能に使わせる
5. When 走行の資源を持つ機能が中断を知らされたとき, the Rover Core shall 手放されるのを待たずに、直ちにモーターを止める
6. While 声の出力の資源で発話が続いているとき, the Rover Core shall その発話が終わってから、資源を優先度の高い機能に渡す
7. When 機能が、自分と同じか高い優先度の機能が使っている資源を求めたとき, the Rover Core shall 求めた機能に、資源が空くまで待つか、すぐに諦めるかを選ばせる
8. When 首の資源を誰も使わなくなったとき, the Rover Core shall 首を正面に戻す
9. The Rover Core shall 資源を持たない機能からの、声・画面・LED・首・走行の操作を受け付けない

### Requirement 5: ボタン
**Objective:** As a 子ども, I want ボタンを押したら必ず何かが応えてほしい, so that ロボットとのやり取りが分かりやすい

#### Acceptance Criteria
1. When Robot HAT の USR ボタン、または設定で指定した追加のボタンが押されたとき, the Rover Core shall 押されたことを、ボタンの資源を持つ機能に 1 回だけ知らせる
2. When ボタンの資源を誰も持っていないときにボタンが押されたとき, the Rover Core shall 「ボタンを待っている」と申告した機能のうち最も優先度の高いものに押下を知らせる
3. If ボタンが押されたときに、受け取る機能がないとき, the Rover Core shall その押下を捨て、後の判定に持ち越さない
4. The Rover Core shall 1 回の押下を、チャタリングで複数回の押下として知らせない

### Requirement 6: 声
**Objective:** As a 子ども, I want ロボットの声が聞き取りやすく、言葉が重ならないこと, so that 何を言われたか分かる

#### Acceptance Criteria
1. When 機能が文章を話すよう求めたとき, the Rover Core shall 設定した声・高さ・速さで、文章を声にしてスピーカーから流し、話し終わったことを機能に知らせる
2. The Rover Core shall 小さなスピーカーでも聞き取りやすいよう、音が割れない範囲で音量をそろえる
3. When 以前と同じ文章を話すとき, the Rover Core shall 前に作った声を使い、話し始めるまでの待ち時間を短くする
4. Where 機能が「残さない」と指定して話させたとき, the Rover Core shall 話し終わった後に、その文章の声を残さない
5. The Rover Core shall 話した文章の中身を、ログに記録しない
6. If 声を作れないとき、または流せないとき, the Rover Core shall 理由をログに記録し、話せなかったことを機能に知らせて、動き続ける

### Requirement 7: 画面（LCD）と LED
**Objective:** As a 子ども, I want ロボットの顔や短い言葉が画面で分かること, so that 声だけでなく目でも気持ちが伝わる

#### Acceptance Criteria
1. When 機能が 2 行の文字を表示するよう求めたとき, the Rover Core shall 各行を 16 文字に収めて LCD に表示する
2. The Rover Core shall ひらがなとカタカナを、LCD で表示できる半角カタカナに変えて表示し、表示できない文字は「?」に置き換える
3. The Rover Core shall 目・笑った目・閉じた目・口・星・ハートなどの絵の文字と、それを組み合わせた顔（ふつう・呼びかけ・うれしい・ねむい）を、機能が使えるようにする
4. When 機能が文字の中央寄せを求めたとき, the Rover Core shall 濁点と半濁点も 1 文字分として数えて、16 文字の中央に置く
5. When 機能がバックライトの点灯または消灯を求めたとき, the Rover Core shall LCD のバックライトをそのとおりにする
6. When 機能が LED の点灯または消灯を求めたとき, the Rover Core shall Robot HAT の LED をそのとおりにする

### Requirement 8: 首と体の動き
**Objective:** As a 子ども, I want ロボットがうなずいたり踊ったりしてほしい, so that ほめられたことや気持ちが伝わる

#### Acceptance Criteria
1. When 機能が首の向き（左右・上下）を求めたとき, the Rover Core shall 首をその向きに動かす。ただし動かせる範囲を超える向きは、範囲の端に収める
2. The Rover Core shall 「正面に戻す」「うなずく」「見回す」「ねむそうにする」「ダンス」の動きを、機能が使えるようにする
3. While 設定で「ダンスでタイヤを動かす」が許可されていないとき, the Rover Core shall ダンスの間もタイヤを回さない
4. When 機能が走行（向きと速さ）を求めたとき, the Rover Core shall ステアリングとモーターをその向きと速さにする
5. The Rover Core shall 首とステアリングの正面の位置を、実機ごとの調整値に合わせる。調整値は、デプロイで版を入れ替えても消えない

### Requirement 9: 待機画面と静かな状態
**Objective:** As a 子ども, I want ロボットが何もしていないときも時計と顔を見せてほしい, so that ロボットがいつもそばにいると感じられる

#### Acceptance Criteria
1. While どの機能も画面の資源を使っていないとき, the Rover Core shall 待機画面として、1 行目に日付と時刻、2 行目に顔を表示し、時刻の変化に合わせて更新する
2. Where 機能が待機画面に短い情報（例: 星の数）を出すよう登録したとき, the Rover Core shall その情報を待機画面の決まった場所に表示する
3. Where 機能が待機画面の顔の向きや表情を求めたとき, the Rover Core shall 待機画面の顔をそのとおりに変える
4. When 機能が静かな状態を求めたとき, the Rover Core shall 待機画面を消してバックライトを消し、静かな状態になったことを登録されたすべての機能に知らせる
5. When 機能が静かな状態の終わりを求めたとき, the Rover Core shall バックライトを点けて待機画面に戻し、静かな状態が終わったことを登録されたすべての機能に知らせる
6. When 再起動したとき, the Rover Core shall 再起動の前に静かな状態だったかどうかを、機能が確かめられるようにする

### Requirement 10: 時刻
**Objective:** As a 保護者, I want 時計がずれたまま声かけが始まらないこと, so that 夜中に起こされたり、違う時刻に声をかけられたりしない

#### Acceptance Criteria
1. The Rover Core shall 現在時刻と、時刻が確か（時刻の同期が済んでいる）かどうかを、機能が確かめられるようにする
2. While 時刻が確かでないとき, the Rover Core shall 待機画面に時刻の代わりに「--:--」を表示する
3. When 時刻が確かになったとき, the Rover Core shall そのことを登録されたすべての機能に知らせる

### Requirement 11: 設定
**Objective:** As a 保護者, I want 名前や時刻などの設定を自分のファイルで変えられ、間違いがすぐ分かること, so that 公開リポジトリに個人の情報を入れずに、安心して設定できる

#### Acceptance Criteria
1. The Rover Core shall 設定を、リポジトリの外にある利用者の設定ファイルから読む
2. If 起動時に設定ファイルがないとき, the Rover Core shall リポジトリにある見本から設定ファイルを作り、作った場所をログに記録して、見本の値で起動する
3. If 設定ファイルの書き方が誤っているとき、または値が許される範囲の外にあるとき, the Rover Core shall 誤りの場所と理由をログに記録し、LCD に設定の誤りであることを表示する
4. If ある機能の設定だけが誤っているとき, the Rover Core shall その機能を開始せず、ほかの機能は開始する
5. When 設定に値が書かれていないとき, the Rover Core shall 決められた既定値を使う
6. If 設定に知らない項目があるとき, the Rover Core shall その項目名をログに記録し、起動を続ける
7. The Rover Core shall 各機能が、自分の設定の項目と既定値と許される範囲を、基盤を書き換えずに定められるようにする

### Requirement 12: 状態とキャッシュの保存
**Objective:** As a 保護者, I want 停電や再起動でも記録が壊れず、個人の記録がリポジトリに入らないこと, so that 子どもの星や記録が消えず、公開リポジトリから漏れない

#### Acceptance Criteria
1. The Rover Core shall 機能の状態（記録・1 日の利用量など）とキャッシュを、リポジトリの外にある利用者のディレクトリに保存する
2. The Rover Core shall 機能ごとに状態の保存先を分け、ほかの機能の状態を書き換えられないようにする
3. If 保存の途中で電源が切れたとき, the Rover Core shall 次の起動で、保存の前か後のどちらかの正しい状態を読めるようにする
4. If 起動時に保存された状態が壊れていて読めないとき, the Rover Core shall 壊れたファイルを別の名前で残し、その機能を空の状態で開始し、ログに記録して動き続ける
5. When デプロイで版が入れ替わったとき, the Rover Core shall 保存した状態とキャッシュをそのまま引き継ぐ
6. The Rover Core shall キャッシュの合計の大きさに上限を設け、上限を超えたら古いものから消す

### Requirement 13: ハードなしでの実行と確認
**Objective:** As a 開発者, I want 実機がなくても PC でロボットを動かして確かめたい, so that 子どもが毎日使う実機を開発で止めずに済む

#### Acceptance Criteria
1. Where ハードなしの実行が指定されたとき, the Rover Core shall 実機のデバイスの代わりに模擬のデバイスを使って起動し、画面の表示・話す文章・LED・首と走行の状態を、開発者が見られる形で出力する
2. Where ハードなしの実行が指定されたとき, the Rover Core shall 開発者がボタンの押下を模擬できるようにする
3. The Rover Core shall 現在時刻を差し替えられるようにし、時刻に基づく振る舞いを、実際の時間を待たずに確かめられるようにする
4. The Rover Core shall 実機のデバイスに関わる部分を除き、ハードのない環境（開発者の PC と自動テストの環境）でテストできるようにする
5. When 開発者が文章を指定して発話の確認を求めたとき, the Rover Core shall その文章を話して終了する
