# Requirements Document

## Project Description (Input)
子どもは、スマホやタブレットからロボットを走らせて遊びたい。保護者は、家の中だけで安全に使え、使える時間を決められ、日課の声かけを邪魔しないことを求めている。

今は、サンプルの `remote.py`（Flask、ポート 8080）にラジコンがある。家のネットワークからだけ開け、4 けた以上の pin でログインする。矢印ボタンを押している間だけ走り（最後の命令から 0.5 秒で止まる）、首の操作・ブッブー・ダンス・速さの切り替えがある。前の物（超音波）と段差（床センサー）では前に進まない。首のカメラと USB カメラ（C270）の映像を見られ、写真を撮れ、文字を入れると話す。使える時間帯と 1 日の上限があり、日課などが始まると止まる。ただし、後退をセンサーで確かめない、1 日の上限が再起動で戻りページを開いているだけで減る、ログインの失敗の待ちが並列のリクエストで効かない、英数字以外の pin で内部エラーになる、ダンスが停止で止まらない、話す声と LCD が日課と衝突する、写真が無期限に残る、という問題がある。

`rover-core` の機能として remote-control を作る（日課より低い優先度を申告する）。子どもから見た遊び方はサンプルと同じにし、走行は常に安全側に倒す（命令が途切れたら・危険を検知したら・例外が起きたら・優先度の高い機能が始まったら止まる）。家の外からは使えず、ログインは総当たりに強くする。使える時間帯を守る（1 日の上限は、要件づくりのときに保護者の判断でなくした）。写真は一定期間で整理できる。首のカメラは `presence` の口から使い、USB カメラはこのスペックで扱う。走行の安全判定・時間の計算・ログインの規則は、偽のセンサーと時計で PC でテストできるようにする。詳しくは `.kiro/specs/remote-control/brief.md` を参照。

## Introduction
スマホのラジコン（remote-control）は、家の Wi-Fi の中のスマホやタブレットのブラウザから、ロボットを走らせて遊ぶ機能です。ボタンを押している間だけ走り、首を動かし、ブッブーやダンスをし、2 台のカメラの映像を見て、写真を撮り、入れた文字をロボットに話させます。

子どもから見た遊び方は、サンプルの振る舞いを踏襲します。サンプルの問題は、次のとおりに決めて直します。
- 後ろはセンサーで確かめられないので、後退はゆっくり、短くだけにする
- センサーが読めないときは、前に進まない
- 1 日の上限はなくし、使える時間帯だけで使う時間を決める（保護者の判断）
- ログインは、まちがいが続くと一定時間すべての端末で止める
- 走らせられるのは 1 台の端末だけにする
- ダンスは首だけで踊り、止める操作と優先度の高い機能で止まる
- 声と LCD は、ほかの機能と調停して使う
- 写真は 30 日で消す

用語:
- **端末**: ラジコンの画面を開いたスマホやタブレットのブラウザ
- **操作**: 走る・首・ブッブー・ダンス・速さの切り替え・写真・話させる、のどれかを端末から行うこと。映像を見るだけは操作に入らない
- **操作する端末**: いまロボットを操作できる 1 台の端末
- **前に進む**: まっすぐ前・左前・右前に走ること
- **後退**: まっすぐ後ろ・左後ろ・右後ろに走ること
- **危険**: 前の物が決めた距離（既定は 20 cm）より近いこと、または前の床に段差があること
- **使える時間帯**: 保護者が決めた、ラジコンを使ってよい時刻の範囲（既定は 07:00〜20:00）

## Boundary Context
- **In scope**: 端末向けの画面と、端末からの操作の受け付け、アクセスの制限（家のネットワーク）とログイン、走行（押している間だけ・速さ・後退の制限）、前の安全の判定（超音波と床センサー）、首の操作、ブッブーとダンス、2 台のカメラの映像、写真の保存と整理、話させる、使える時間帯、操作する端末を 1 台にすること、ほかの機能に譲ること、設定と self-check の項目、センサーを確かめる道具、開発 PC で偽のセンサーと時計を使って確かめられること
- **Out of scope**: 家の外からの操作、通信の暗号化（家のネットワークに限ることで代える）、1 日の上限（保護者の判断でなくした）、首のカメラの操作（`presence`）、資源の調停の仕組みと静かな状態（`rover-core`）、日課のタスクの判定（`daily-routine`）、映像の保存、写真の一覧と端末からの削除、話させた言葉の記録
- **Adjacent expectations**:
  - `rover-core` は、走行・首・声・LCD の資源の調停、静かな状態（おやすみ）、時刻が確かかどうか、記録の保存先、self-check の口を提供する。優先度の高い機能が資源を借りに来たら、この機能に中断を知らせ、走行をすぐ止める。資源を返したときは、モーターを止めて首を正面に戻す
  - `presence` は、首のカメラの画像を貸し出す。貸している間、顔を探さない
  - `daily-routine`・`voice-gateway`・`conversation`・`daruma-game` は、この機能より優先度が高く、始まるときにこの機能から資源を取り上げる

## Requirements

### Requirement 1: 家のネットワークだけで使う
**Objective:** As a 保護者, I want 家の Wi-Fi の中の端末だけがラジコンを開けること, so that 家の外からロボットを動かされない

#### Acceptance Criteria
1. The Remote Control shall 家のネットワークの範囲のアドレス（IPv4 のプライベートアドレス、ループバック、IPv6 のユニークローカルとリンクローカル）から来た要求だけを受け付ける
2. If 家のネットワークの範囲の外のアドレス、または読み取れないアドレスから要求が来たとき, the Remote Control shall 画面も映像も写真も返さずに断る
3. Where ラジコンが有効で pin が正しく設定されているとき, the Remote Control shall 設定したポート（既定は 8080）で端末からの要求を待つ
4. If pin が設定されていないか、4〜8 けたの数字でないとき, the Remote Control shall 端末からの要求を待たず、理由（pin の値は含めない）をログと self-check に出す
5. If 設定したポートを使えないとき, the Remote Control shall 端末からの要求を待たず、理由をログと self-check に出し、ほかの機能は動き続ける

### Requirement 2: ログイン
**Objective:** As a 保護者, I want pin を知っている人だけが使えて、総当たりで当てられないこと, so that 子どもが安心して遊べる

#### Acceptance Criteria
1. While ログインしていない端末のとき, the Remote Control shall pin を入れる画面だけを出し、操作・映像・写真・状態を返さない
2. When 正しい pin が入れられたとき, the Remote Control shall その端末をログインした状態にし、操作の画面を出す
3. When まちがった pin、または数字以外を含む入力が入れられたとき, the Remote Control shall 「ばんごうが ちがうよ」と出し、内部のエラーにしない
4. If まちがいが 5 回続いたとき, the Remote Control shall それから 5 分間、どの端末からの pin も確かめずに「しばらく まってね」と出す（同時に何度送られても、まちがいを 1 回ずつ数える）
5. When 正しい pin でログインできたとき, the Remote Control shall 続いたまちがいの数を 0 に戻す
6. When ロボットが起動し直したとき、またはログインから 12 時間たったとき, the Remote Control shall それまでのログインを無効にし、pin を入れ直させる
7. When 端末で「おわる」が押されたとき, the Remote Control shall その端末のログインを無効にし、pin を入れる画面に戻す
8. The Remote Control shall pin・入れられた pin・端末のアドレスをログに出さない

### Requirement 3: 操作する端末は 1 台
**Objective:** As a 保護者, I want 走らせられる端末が 1 台だけであること, so that 2 台の命令がぶつかってロボットが思わぬ動きをしない

#### Acceptance Criteria
1. When 操作する端末がないときに、ログインした端末が操作をしたとき, the Remote Control shall その端末を操作する端末にする
2. While ほかの端末が操作する端末のとき, the Remote Control shall この端末の操作を受け付けず、画面に「いまは ほかの ひとが そうさ ちゅう」と出す。映像は見られるようにする
3. When 操作する端末の最後の操作から 10 秒たったとき, the Remote Control shall 操作する端末をなくし、ほかの端末が操作できるようにする
4. When 操作する端末がなくなったとき、または操作する端末のログインが無効になったとき, the Remote Control shall 走行を止める

### Requirement 4: 押している間だけ走る
**Objective:** As a 子ども, I want 矢印のボタンを押している間だけロボットが走ること, so that 思いどおりに走らせて遊べる

#### Acceptance Criteria
1. The Remote Control shall 端末の画面に、前・左前・右前・後ろ・左後ろ・右後ろの 6 つの走るボタンと、「とめる」のボタンを出す
2. While 走るボタンが押されている間, the Remote Control shall その向きに走る（左と右はステアリングを 30 度切る）
3. When 走るボタンが離されたとき、「とめる」のボタンが押されたとき、端末の画面が隠れたとき、または写真のボタンが押されたとき, the Remote Control shall 走行を止め、ステアリングをまっすぐに戻す
4. If 最後の走る命令から 0.5 秒たっても次の命令が届かないとき, the Remote Control shall 走行を止める
5. The Remote Control shall 速さを「ゆっくり」（既定は 20）と「はやい」（既定は 35）の 2 つから選べるようにし、始めは「ゆっくり」にする
6. When 速さが切り替えられたとき, the Remote Control shall 走っている途中でも、次の命令からその速さで走る
7. While 走っている間, the Remote Control shall LCD に、前に進むときは「ﾌﾞｰﾝ!」、後退のときは「ﾊﾞｯｸ」、止まっているときは「ﾗｼﾞｺﾝ」と笑顔を出す
8. If 走行の途中で予期しないエラーが起きたとき, the Remote Control shall 走行を止め、それからの走る命令を受け付けられる状態に戻す

### Requirement 5: 前の安全
**Objective:** As a 保護者, I want 前に物や段差があったら必ず止まること, so that ロボットがぶつかったり落ちたりしない

#### Acceptance Criteria
1. While 前に進もうとしている間, the Remote Control shall 前の物までの距離と前の床の段差を、0.1 秒ごとに確かめる
2. When 前に進もうとしているときに危険を見つけたとき, the Remote Control shall 1 回の読み取りで、すぐに走行を止める
3. While 危険が続いている間, the Remote Control shall 前に進む命令を受け付けない。後退の命令は受け付ける
4. If 前の物の距離か床の段差を読み取れないとき（センサーの故障・読み取りの失敗・ありえない値）, the Remote Control shall 危険があるとして、前に進まない
5. When 危険で止まったとき, the Remote Control shall LCD に「ｽﾄｯﾌﾟ!」と呼びかけの顔を出し、物なら「おっと、なにか あるよ」、段差なら「あぶない！とまるよ」と話し、画面に「まえに なにか あるよ」か「だんさが あるよ！」と出す
6. The Remote Control shall 危険で止まったときの声を、10 秒に 1 回までにする（画面の知らせは毎回出す）
7. Where 床の段差の判定を設定で無効にしたとき（既定は有効）, the Remote Control shall 前の物の距離だけで危険を判定する

### Requirement 6: 後退の制限
**Objective:** As a 保護者, I want 確かめられない後ろには、ゆっくり短くしか下がらないこと, so that 後ろの物や段差で事故にならない

#### Acceptance Criteria
1. While 後退している間, the Remote Control shall 選んだ速さにかかわらず、「ゆっくり」の速さで走る
2. When 後ろのボタンを押し続けて 2 秒たったとき（既定。設定で変えられる）, the Remote Control shall 走行を止め、ボタンが離されるまで後退しない
3. When 後退で止まったボタンが離されて、もう一度押されたとき, the Remote Control shall また後退を始める
4. The Remote Control shall 後退のボタンの近くに「うしろは みえないよ。ゆっくり さがるよ」と出す

### Requirement 7: 首・ブッブー・ダンス
**Objective:** As a 子ども, I want 首を動かしたり、ブッブーやダンスをさせたりできること, so that 走る以外でも遊べる

#### Acceptance Criteria
1. When 首の上・下・左・右のボタンが押されたとき, the Remote Control shall 首をその向きに 15 度動かす。動かせる範囲の端では、それ以上動かさない
2. When 首の「まんなか」のボタンが押されたとき, the Remote Control shall 首を正面に戻す
3. When ブッブーのボタンが押されたとき, the Remote Control shall 「ブッブー！」「プップー！」「どいて どいてー！」「しゅっぱつ しんこう！」から 1 つをランダムに選んで話す。ほかの言葉を話している途中なら、話さない
4. When ダンスのボタンが押されたとき, the Remote Control shall 走行を止め、LCD に「ﾀﾞﾝｽ!」と笑顔を出し、首だけで踊る（タイヤは動かさない）。終わったら首を踊る前の向きに戻す
5. While 踊っている間, the Remote Control shall 走る命令とダンスの命令を受け付けない
6. When 踊っている間に、止める操作（走るボタンが離されること、または「とめる」のボタン）、操作する端末がなくなること、または優先度の高い機能の開始があったとき, the Remote Control shall 次の動きに移る前にダンスを止める

### Requirement 8: 映像
**Objective:** As a 子ども, I want ロボットから見える景色をスマホで見られること, so that ロボットになったつもりで走らせられる

#### Acceptance Criteria
1. While ログインした端末が画面を開いている間, the Remote Control shall 首のカメラと USB カメラの映像を、それぞれ 1 秒に最大 10 枚で出す
2. The Remote Control shall 同じカメラの映像を、同時に 3 つの端末まで見られるようにし、それより多い端末には「いまは みられないよ」と出す
3. If どちらかのカメラが使えないとき（ない・`presence` が動いていない・ほかの機能が借りている・読み取りの失敗）, the Remote Control shall そのカメラの枠に「カメラが つかえないよ」と出し、もう一方のカメラと操作は使えるようにする
4. When カメラの映像を見る端末がなくなって 3 秒たったとき, the Remote Control shall そのカメラを返す
5. The Remote Control shall 映像を保存しない
6. When 使える時間帯が終わったとき、おやすみになったとき、またはログインが無効になったとき, the Remote Control shall 映像を止める

### Requirement 9: 写真
**Objective:** As a 子ども, I want ボタンで写真を撮ってスマホに残せること, so that ロボットの目から見た景色を残せる

#### Acceptance Criteria
1. When 写真のボタンが押されたとき, the Remote Control shall 走行を止め、LCD に「ﾊｲ ﾁｰｽﾞ!」と出し、「はい、チーズ！」と話してから、使えるカメラそれぞれで 1 枚ずつ撮る
2. When 1 枚以上撮れたとき, the Remote Control shall 「とれたよ！」と話し、端末に撮った写真を出して、端末に保存できるようにする
3. If 1 枚も撮れなかったとき, the Remote Control shall 「ごめんね、うまく とれなかった」と話し、端末に撮れなかったことを出す
4. While 写真を撮っている途中のとき, the Remote Control shall 次の写真のボタンを受け付けない
5. The Remote Control shall 写真をリポジトリの外の、保護者だけが読める場所に、撮った時刻の分かる重ならない名前で保存する
6. The Remote Control shall 撮ってから決めた日数（既定は 30 日）たった写真を、1 日に 1 回とロボットの起動のときに消す
7. The Remote Control shall この機能で撮った写真だけを端末に返し、それ以外のファイルを返さない

### Requirement 10: 話させる
**Objective:** As a 子ども, I want 入れた文字をロボットに話させられること, so that ロボットを通して家族に話しかけられる

#### Acceptance Criteria
1. When 端末から文字が送られたとき, the Remote Control shall 制御文字を空白に変え、前後の空白を除き、120 文字までに切ってから話す
2. When 話すとき, the Remote Control shall LCD に「ｵﾊﾅｼ ﾁｭｳ」と呼びかけの顔を出し、話し終えたら走行の表示に戻す
3. If 送られた文字が空のとき, the Remote Control shall 話さずに、端末に「もじを いれてね」と出す
4. If ロボットがほかの言葉を話している途中のとき, the Remote Control shall 話さずに、端末に「いま おはなし ちゅう。ちょっと まってね」と出す
5. The Remote Control shall 送られた文字と、その声を、ログにも記録にも残さない

### Requirement 11: 使える時間帯とおやすみ
**Objective:** As a 保護者, I want 決めた時間帯だけラジコンを使えること, so that 夜や朝の支度の時間に遊ばない

#### Acceptance Criteria
1. While 使える時間帯の外のとき, the Remote Control shall 操作を受け付けず、端末に「いまは ラジコンの じかんじゃないよ」と出す
2. While おやすみのとき, the Remote Control shall 操作を受け付けず、端末に「いまは ラジコンの じかんじゃないよ」と出す
3. If ロボットの時刻が確かでないとき, the Remote Control shall 操作を受け付けず、端末に「いまは つかえないよ」と出す
4. When 操作している途中で使える時間帯が終わったとき、またはおやすみになったとき, the Remote Control shall すぐに走行と首の動きを止め、端末に 11.1 の知らせを出す
5. The Remote Control shall 使える時間帯を、始まりと終わりの時刻（HH:MM）で設定できるようにし、始まりの時刻を含み、終わりの時刻を含まない
6. The Remote Control shall 1 日に使える時間の上限を持たない

### Requirement 12: ほかの機能に譲る
**Objective:** As a 保護者, I want 日課の声かけや話しかけが始まったらラジコンが止まること, so that 毎日の習慣を壊さない

#### Acceptance Criteria
1. The Remote Control shall 日課・音声の入口・おしゃべり・だるまさんより低く、見つけてあいさつより高い優先度で資源を借りる
2. When 優先度の高い機能がこの機能の資源を借りに来たとき, the Remote Control shall すぐに走行・ダンス・首の動きを止めて資源を返し、端末に「ローバーは いま ほかの ことを しているよ。ちょっと まってね」と出す
3. When 優先度の高い機能が資源を返した後に、端末で操作がされたとき, the Remote Control shall また資源を借りて操作を受け付ける（止まる前の操作を続けて行わない）
4. When 操作する端末がなくなったとき, the Remote Control shall 首を正面に戻して、すべての資源を返す
5. While この機能が首を借りている間, the Remote Control shall 見つけてあいさつが首を動かさないようにする
6. If この機能の中で予期しないエラーが起きたとき, the Remote Control shall 走行を止め、首を正面に戻し、ほかの機能は動き続ける

### Requirement 13: 設定と self-check
**Objective:** As a 保護者, I want ラジコンの設定を決めて、実機で使える状態か確かめられること, so that 安心して子どもに使わせられる

#### Acceptance Criteria
1. The Remote Control shall 次の設定を持つ: 有効かどうか（既定は無効）、pin、ポート、使える時間帯、2 つの速さ、危険とする前の物の距離、床の段差の判定の有効、後退を続けられる長さ、写真を残す日数、USB カメラの指定（空なら C270 を自動で探す）
2. If 設定の値が範囲の外のとき, the Remote Control shall 端末からの要求を待たず、誤りの場所（キーのパス）と理由をログに出し、値は出さない。ほかの機能は動き続ける
3. The Remote Control shall pin をリポジトリと設定の見本に入れず、実機の設定ファイルだけに置くように手順書で案内する
4. The Remote Control shall self-check に、pin とポートの設定が使えるか（`remote.setup`）、前の物の距離と床の段差を読み取れるか（`remote.sensors`）、USB カメラがあるか（`remote.usb_camera`）を出す
5. While ロボットが動いているとき, the Remote Control shall センサーとカメラに触る self-check の項目を「飛ばした」にする
6. The Remote Control shall self-check の理由とログに、pin・端末のアドレス・話させた文字を出さない

### Requirement 14: センサーを確かめる道具
**Objective:** As a 保護者, I want 実機で前の物の距離と床の段差の読み取りを確かめられること, so that 危険で止まる距離が部屋に合っているかが分かる

#### Acceptance Criteria
1. When 保護者がセンサーを確かめる道具を動かしたとき, the Remote Control shall 決めた秒数（既定は 30 秒）の間、0.5 秒ごとに、前の物の距離、床の段差の判定、設定で危険になるかどうかを出す
2. If ロボットのサービスが動いているとき, the Remote Control shall 道具を始めずに「ロボットが動いています。止めてから実行してください」と出す
3. The Remote Control shall 道具の中でタイヤを動かさない

### Requirement 15: 開発 PC で確かめられること
**Objective:** As a 開発者, I want 安全の判定・時間帯・ログインの規則を偽のセンサーと時計で確かめられること, so that 子どもが使う実機に入れる前に PC で確かめられる

#### Acceptance Criteria
1. The Remote Control shall 前の安全の判定、後退の制限、命令が途切れたときの停止、使える時間帯、ログインのまちがいの数え方、操作する端末の切り替え、写真の整理を、偽のセンサー・偽の時計・偽のカメラで、実機なしに確かめられるようにする
2. The Remote Control shall 開発 PC で偽のハードを使って起動したとき、端末向けの画面を開いて操作できるようにする
