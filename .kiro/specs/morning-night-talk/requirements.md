# Requirements Document

## Project Description (Input)
子ども（4 歳前後）は、寝る前と朝に、ロボットから今日やあしたの天気や、自分の好きなもの（恐竜・電車・動物など）の短いお話を聞きたい。保護者は、天気を知って服や持ち物の支度に生かし、寝る前と朝の時間を楽しく区切りたい。

今は、日課（`daily-routine`）が時刻の声かけをし、寝るタスクが終わるとおやすみモードに入り、起床時刻には声を出さずに起きる。天気や話題を話す機能はない。おしゃべり（`conversation`）は子どもが話しかけたときに Claude で答えるだけで、ロボットから話題を出すことはない。

`rover-core` の機能として、朝と夜のお話（morning-night-talk）を作る。夜は、寝るタスクの声かけの前（おやすみモードに入る前）に、あしたの天気と好きなもののお話をする。朝は、起床時刻には黙ったまま（日課の決まりを崩さない）、その日の最初の時刻のタスクの前に、今日の天気と好きなもののお話をする。天気は、保護者が個人の設定（リポジトリの外）に書いた現在地の天気を外部の天気の API から取る。好きなものは保護者が設定に一覧で書き、Claude がその中から 4 歳向けの短い豆知識を作る（`conversation` と同じ子ども向けの決まりに従う）。子どもの名前と現在地の細かい場所は Claude に送らない。日課の声かけを遅らせず、おやすみモード中は話さない。天気の API や Claude が使えないときは、決まった短い言葉で済ませ、ロボット全体は止まらない。時刻の判定と話の組み立ては、時計・天気・LLM を差し替えて PC でテストできるようにする。

## Introduction
朝と夜のお話（Morning Night Talk）は、寝る前と朝に、ロボットから天気と、子どもの好きなものの短い豆知識を話す機能です。`rover-core` の機能を足す口に登録され、声と画面は `rover-core` から借りて使います。

夜は、日課の寝るタスクの予告に続けて、あしたの天気と豆知識を話します。朝は、その日の最初の時刻のタスクの予告に続けて、今日の天気と豆知識を話します。起床時刻にはこれまでどおり黙って起き、予告がおやすみモードの間に当たるときは、最初のタスクが終わった後に話します。そばに子どもがいるかは確かめず、時刻で話します。

天気は、保護者が設定した現在地の天気を外部の天気の情報から取り、天気・いちばん高い気温と低い気温・傘や上着の一言・注意報や警報を、子どもが怖がらない言葉で話し、LCD にも出します。豆知識は、保護者が設定した好きなものの一覧から 1 つを選び、外部の会話の AI に 4 歳向けの短いお話を作らせます。子どもの呼び名と細かい現在地は、外部の会話の AI に送りません。

この文書で使う言葉:
- **夜のお話**: 寝るタスクの予告に続けて話す、あしたの天気と豆知識
- **朝のお話**: その日の最初の時刻のタスクの予告に続けて（または、そのタスクの後に）話す、今日の天気と豆知識
- **予告の時刻**: 日課の時刻のタスクの、時刻から予告の早さ（既定は 10 分）だけ前の時刻。日課の予告の言葉がないタスクでも、この時刻を使う
- **朝の最初のタスク**: その日の時刻のタスクのうち、寝るタスクを除いて最も早いもの
- **天気の話**: 天気・気温・傘や上着の一言・注意報や警報の部分
- **豆知識**: 好きなものの 1 つについての、短い本当のお話
- **好きなもの**: 保護者が設定に書く、子どもが興味を持つものの一覧（例: きょうりゅう、でんしゃ、どうぶつ）
- **現在地**: 天気を調べる場所。保護者が設定に書く

## Boundary Context
- **In scope**:
  - 夜のお話と朝のお話を話す時刻の判定（日課の予告の時刻に続ける。朝は、予告がおやすみモードの間に当たるときに最初のタスクの後へずらす）
  - 天気の話の中身（天気・気温・傘や上着の一言・注意報や警報）と、LCD の表示
  - 好きなものの一覧からの選び方と、豆知識を外部の会話の AI に作らせること
  - 外部に送るもの・送らないもの（呼び名と細かい現在地を送らない）
  - 日課・ほかの機能・おやすみモードとの譲り合い
  - 1 日にそれぞれ 1 回だけ話すこと（再起動しても守る）
  - 天気の情報や外部の会話の AI が使えないときの扱い
  - 設定（現在地、好きなもの、夜と朝のそれぞれを使うかどうか）と検証、self-check の項目
  - 保護者が試しにお話をさせるコマンド
  - 開発 PC で、偽の時計・偽の天気・偽の会話の AI を使って確かめられること
- **Out of scope**:
  - 声で頼まれたときに天気を答えること（「きょうの てんきは？」など。今後、`voice-gateway` の語句として足す）
  - 子どもがそばにいるかを確かめること、顔を見てから話すこと（`presence`）
  - ニュースなど、天気と豆知識のほかの話題
  - 子どもの返事や反応を聞くこと、覚えること。おしゃべりからの好きなものの学習
  - 待機画面に天気を出し続けること
  - 複数の場所の天気
  - 日課の予告・声かけ・おやすみモードの振る舞いを変えること（`daily-routine`）
- **Adjacent expectations**:
  - `daily-routine` は、タスクの名前や言葉を出さずに、寝るタスクの時刻、朝の最初のタスクの時刻、それぞれの予告の早さ、予告を言い終えたかどうか、朝の最初のタスクが終わったかどうか、タスクか予告の最中かどうか、おやすみモードかどうか、今日の種類の日に寝るタスクがあるかどうかを、ほかの機能が確かめられるようにする。今の日課の公開の情報（次のタスクの時刻、最中かどうか、おやすみかどうか）では足りないため、`daily-routine` の公開の情報を足す変更が要る
  - `rover-core` は、声と画面の調停、静かな状態の知らせ、時刻が確かかどうか、再起動をまたぐ状態の保存先、設定の読み込みを受け持つ。日課が資源を求めたら、この機能に中断を知らせ、発話の途中なら言い終えてから渡す
  - `conversation` が守っている子ども向けの話し方の決まり（やさしく、短く、怖い話をしない、うそをつかない、個人のことを聞かない）を、豆知識にも当てはめる。外部の会話の AI を使うための鍵は、`conversation` と同じ置き場所のものを使う
  - おしゃべりの 1 日の回数と時間帯の上限には数えない

## Requirements

### Requirement 1: 設定と検証
**Objective:** As a 保護者, I want 現在地・好きなもの・夜と朝のどちらを使うかを、自分の設定ファイルで決められること, so that 家に合ったお話を、公開リポジトリに個人の情報を入れずに設定できる

#### Acceptance Criteria
1. The Morning Night Talk shall 現在地、好きなものの一覧、夜のお話を使うかどうか、朝のお話を使うかどうかを、リポジトリの外にある利用者の設定ファイルから読む
2. The Morning Night Talk shall 機能そのものを使うかどうかの既定を「使わない」とし、保護者が使うと書いたときだけ動く
3. Where 夜のお話か朝のお話の一方だけを使うと設定されているとき, the Morning Night Talk shall 使うと設定された方だけを話す
4. If 現在地の書き方が誤っているとき、または好きなものの一覧に空の項目があるとき, the Morning Night Talk shall 誤りの場所と理由を記録してこの機能を開始せず、`rover-core` の設定の誤りの表示に任せる
5. Where 現在地が設定されていないとき, the Morning Night Talk shall 天気の話をせず、豆知識だけを話す
6. Where 好きなものの一覧が設定されていないか空のとき, the Morning Night Talk shall 豆知識を話さず、天気の話だけを話す
7. If 現在地と好きなものの両方が設定されていないとき, the Morning Night Talk shall お話をせず、そのことを self-check で知らせる
8. The Morning Night Talk shall 現在地・好きなもの・呼び名などの設定の値を、ログと self-check の表示に出さない

### Requirement 2: 夜のお話を話す時
**Objective:** As a 子ども, I want 寝る前に、あしたの天気と好きなもののお話を聞きたい, so that あしたを楽しみにして寝られる

#### Acceptance Criteria
1. When 今日の寝るタスクの予告の時刻になり、日課がその予告を言い終えたとき, the Morning Night Talk shall 続けて夜のお話を話す
2. Where 寝るタスクに日課の予告の言葉がないとき, the Morning Night Talk shall 寝るタスクの予告の時刻になったときに夜のお話を話す
3. While 日課がタスクか予告の最中のとき, the Morning Night Talk shall 夜のお話を始めず、日課の最中が終わるのを待つ
4. If 寝るタスクを始める時刻までに夜のお話を始められないとき, the Morning Night Talk shall その日の夜のお話を話さない
5. If 寝るタスクがすでに始まったか、おやすみモードに入っているとき, the Morning Night Talk shall 夜のお話を話さない
6. Where 今日の種類の日に寝るタスクがないとき, the Morning Night Talk shall 夜のお話を話さない

### Requirement 3: 朝のお話を話す時
**Objective:** As a 保護者, I want 朝のお話が、起床時刻の静けさを崩さずに、朝の支度の前に聞けること, so that 子どもを無理に起こさず、天気に合わせた支度ができる

#### Acceptance Criteria
1. When 朝の最初のタスクの予告の時刻がおやすみモードの後にあり、その時刻になって日課がその予告を言い終えたとき, the Morning Night Talk shall 続けて朝のお話を話す
2. Where 朝の最初のタスクに日課の予告の言葉がなく、予告の時刻がおやすみモードの後にあるとき, the Morning Night Talk shall その予告の時刻になったときに朝のお話を話す
3. When 朝の最初のタスクの予告の時刻がおやすみモードの間にあるとき, the Morning Night Talk shall 予告の時刻には話さず、朝の最初のタスクが終わった（結果が「できた」「スキップ」「中断」のどれでも）後に朝のお話を話す
4. While 日課がタスクか予告の最中のとき, the Morning Night Talk shall 朝のお話を始めず、日課の最中が終わるのを待つ
5. If 予告の時刻に続けて話す場合に、朝の最初のタスクを始める時刻までに朝のお話を始められないとき, the Morning Night Talk shall 朝の最初のタスクが終わった後に朝のお話を話す
6. If 朝の最初のタスクが終わってから 60 分たっても朝のお話を始められないとき, the Morning Night Talk shall その日の朝のお話を話さない
7. Where 今日の種類の日に寝るタスクを除く時刻のタスクがないとき, the Morning Night Talk shall 朝のお話を話さない
8. The Morning Night Talk shall 起床時刻（おやすみモードが終わる時）に声を出さない

### Requirement 4: 1 日に 1 回
**Objective:** As a 保護者, I want 夜と朝のお話が、それぞれ 1 日に 1 回だけであること, so that 再起動などで同じお話が何度も流れない

#### Acceptance Criteria
1. The Morning Night Talk shall 夜のお話と朝のお話を、それぞれ 1 日に 1 回まで話す
2. When ロボットを起動し直したとき, the Morning Night Talk shall その日にすでに話したお話を、もう一度話さない
3. If 話したかどうかの記録が読めないとき, the Morning Night Talk shall その日はすでに話したものとして扱う
4. When お話の途中で日課に資源を渡して中断したとき, the Morning Night Talk shall その回のお話を話したものとして扱い、続きを話さない

### Requirement 5: 天気の話
**Objective:** As a 保護者, I want 天気と気温と、支度に役立つ一言を、子どもが分かる言葉で話してほしい, so that 服や傘の支度を子どもと一緒に考えられる

#### Acceptance Criteria
1. When 夜のお話をするとき, the Morning Night Talk shall 現在地のあしたの天気を話す
2. When 朝のお話をするとき, the Morning Night Talk shall 現在地の今日の天気を話す
3. The Morning Night Talk shall 天気の話で、天気（はれ・くもり・あめ・ゆき など）と、その日のいちばん高い気温といちばん低い気温を話す
4. When その日の雨か雪の降る確率が決めた値（既定は 50%）以上のとき, the Morning Night Talk shall 傘を持っていくことを勧める一言を話す
5. When その日のいちばん低い気温が寒いと決めた温度（既定は 10 度）より低いとき, the Morning Night Talk shall 上着を着ることを勧める一言を話す
6. When その日のいちばん高い気温が暑いと決めた温度（既定は 30 度）以上のとき, the Morning Night Talk shall 水を飲むことを勧める一言を話す
7. When 現在地に注意報か警報が出ているとき, the Morning Night Talk shall 怖がらせない言葉で知らせ、「おうちの ひとと いっしょに きを つけようね」のように家の大人に任せる一言を添える
8. The Morning Night Talk shall 天気の話を、ひらがな中心のやさしい言葉で、ロボットの中で決めた言い方から組み立てる
9. While 天気の話をしている間, the Morning Night Talk shall LCD に、あしたか今日かの別、天気、いちばん高い気温といちばん低い気温を半角カタカナと数字で出す（例: 「ｱｼﾀ ﾊﾚ 18/9」）
10. If 天気の情報が取れないとき、または取れた情報が古すぎる（お話の日のものでない）とき, the Morning Night Talk shall 天気の話をせず、豆知識だけを話す
11. If 注意報や警報の情報だけが取れないとき, the Morning Night Talk shall 注意報や警報には触れずに、ほかの天気の話をする

### Requirement 6: 好きなものの豆知識
**Objective:** As a 子ども, I want 自分の好きなものの、短くておもしろく、本当のお話を聞きたい, so that 毎日のお話が楽しみになり、保護者も安心して聞かせられる

#### Acceptance Criteria
1. When お話をするとき, the Morning Night Talk shall 好きなものの一覧から 1 つを選び、その豆知識を話す
2. When 好きなものが 2 つ以上あるとき, the Morning Night Talk shall 直前のお話と同じ好きなものを続けて選ばない
3. The Morning Night Talk shall 豆知識を、外部の会話の AI に、4 歳向けに、2 文以内で、よく知られた本当のことだけを、やさしく話すよう求めて作らせる
4. The Morning Night Talk shall 豆知識に、`conversation` と同じ子ども向けの決まり（怖い話・暴力・大人向けの話をしない、うそをつかない、危ないことを勧めない）を当てはめる
5. When 夜のお話をするとき, the Morning Night Talk shall 寝る前に合う、落ち着いた豆知識を求める
6. The Morning Night Talk shall 豆知識の長さを、決めた文字数（既定は 120 文字）までにし、絵文字や記号を話さない
7. If 外部の会話の AI が豆知識を作れなかったとき、返事を拒んだとき、または決めた時間までに返事がないとき, the Morning Night Talk shall 豆知識を話さず、天気の話だけを話す
8. The Morning Night Talk shall 作った豆知識の言葉と声を、話し終えた後に残さない

### Requirement 7: お話の流れと長さ
**Objective:** As a 子ども, I want お話が短く、すぐに始まること, so that 待たされずに聞ける

#### Acceptance Criteria
1. When お話をするとき, the Morning Night Talk shall 夜は「おやすみの まえの おはなし だよ」、朝は「おはよう。きょうの おはなし だよ」のような始まりの言葉、天気の話、豆知識、終わりの言葉（夜は「あしたも たのしみだね」、朝は「きょうも いちにち がんばろうね」のような言葉）の順に話す
2. Where 始まりや終わりの言葉に呼び名の差し込み位置（`{name}`）があるとき, the Morning Night Talk shall そこに設定の呼び名（なければ「きみ」）を入れて話す
3. The Morning Night Talk shall 1 回のお話を、始まりから終わりまでおおむね 60 秒以内にする
4. When お話を始める時になったとき, the Morning Night Talk shall 天気と豆知識を用意するために子どもを待たせず、5 秒以内に話し始める
5. If お話を始める時までに天気か豆知識の用意ができていないとき, the Morning Night Talk shall 用意ができた部分だけを話す
6. If 天気の話と豆知識の両方が用意できないとき, the Morning Night Talk shall 始まりの言葉を言わず、夜は「きょうも たのしかったね。おやすみ なさい」、朝は「おはよう。きょうも いちにち たのしもうね」のような決まった短い言葉だけを話す
7. While お話をしている間, the Morning Night Talk shall LCD に、天気の話の間は天気の表示を、そのほかの間はお話をしている顔を出す
8. When お話を終えたとき, the Morning Night Talk shall 声と画面を返し、待機画面に戻す

### Requirement 8: 日課・ほかの機能・おやすみモードとの譲り合い
**Objective:** As a 保護者, I want お話が日課の声かけを遅らせず、夜中に声を出さないこと, so that 毎日の習慣と夜の静けさが崩れない

#### Acceptance Criteria
1. When お話の途中で日課が声や画面を求めたとき, the Morning Night Talk shall 話している文を言い終えたところでお話をやめ、資源を日課に渡す
2. While おやすみモード（静かな状態）の間, the Morning Night Talk shall 声を出さず、画面を点けない
3. When お話の途中で静かな状態に入ったとき, the Morning Night Talk shall 話している文を言い終えたところでお話をやめる
4. While ほかの機能（おしゃべり・あそび・ラジコンなど）が声を使っているとき, the Morning Night Talk shall その機能が終わるのを待ってから話し、2.4・3.5・3.6 の時刻を過ぎたらその回は話さない
5. While 時刻が確かでないとき, the Morning Night Talk shall お話を始めない
6. While お話をしている間, the Morning Night Talk shall モーターを動かさない
7. The Morning Night Talk shall お話を、おしゃべりの 1 日の回数と時間帯の上限に数えない

### Requirement 9: 外部に送るもの
**Objective:** As a 保護者, I want 子どもの呼び名と家の細かい場所が外部に送られないこと, so that 子どもの個人情報と家の場所を守れる

#### Acceptance Criteria
1. The Morning Night Talk shall 子どもの呼び名を、天気の情報の問い合わせと外部の会話の AI のどちらにも送らない
2. The Morning Night Talk shall 天気の情報の問い合わせに、現在地を市区町村ほどの粗さに丸めた場所だけを送る
3. The Morning Night Talk shall 外部の会話の AI に、選んだ好きなもの、夜か朝かの別、日付と季節だけを送り、現在地も天気の情報も送らない
4. The Morning Night Talk shall 現在地・天気の情報・豆知識の言葉を、ログに出さない
5. The Morning Night Talk shall 外部の会話の AI を使うための鍵を、`conversation` と同じリポジトリの外の置き場所から読み、ログと self-check の表示に出さない

### Requirement 10: 失敗に強いこと
**Objective:** As a 保護者, I want 通信や外部のサービスが失敗しても、ロボット全体が止まらないこと, so that 日課や遊びがいつもどおり使える

#### Acceptance Criteria
1. If インターネットにつながらないとき, the Morning Night Talk shall 7.5・7.6 に従って話せる部分だけを話し、ほかの機能とロボット全体を動かし続ける
2. If 天気の情報や外部の会話の AI の問い合わせが決めた時間（既定は 20 秒）を過ぎたとき, the Morning Night Talk shall その問い合わせをやめ、使えなかったものとして扱う
3. If この機能の中で予期しない失敗が起きたとき, the Morning Night Talk shall 借りている資源を返してこの機能だけを止め、失敗の種類だけを記録する
4. If 外部の会話の AI を使うための鍵がないか、使えない状態のとき, the Morning Night Talk shall 豆知識を話さずに天気の話だけを話し、そのことを self-check で知らせる

### Requirement 11: self-check と試しの実行
**Objective:** As a 保護者, I want 設定と外部のつながりを確かめ、お話を試しに聞けること, so that 子どもが使う前に、実機で正しく動くかが分かる

#### Acceptance Criteria
1. The Morning Night Talk shall self-check で、機能を使うかどうか、現在地と好きなものが設定されているか、天気の情報を取れるか、外部の会話の AI の鍵が使える状態かを、決まった言葉で知らせる
2. The Morning Night Talk shall self-check の表示に、現在地・好きなもの・鍵・天気の値を出さない
3. When 保護者が試しに夜のお話か朝のお話をさせるコマンドを実行したとき, the Morning Night Talk shall 時刻にかかわらず、そのお話を 1 回話す
4. When 試しのお話を話したとき, the Morning Night Talk shall その日に話したことの記録と、直前に選んだ好きなものの記録を変えない
5. While おやすみモードの間, the Morning Night Talk shall 試しのお話のコマンドを受けても話さず、話さなかったことを表示する

### Requirement 12: PC での確かめ
**Objective:** As a 開発者, I want 話す時刻の判定とお話の組み立てを、実機なしで確かめられること, so that 子どもが毎日使う実機に入れる前に、大半を PC と CI で確かめられる

#### Acceptance Criteria
1. The Morning Night Talk shall 時計・天気の情報・外部の会話の AI を偽物に差し替えて、PC で動かせる
2. Where 偽物の天気の情報と偽物の会話の AI を使うとき, the Morning Night Talk shall 外部に問い合わせずに、決めた天気と豆知識でお話を組み立てる
3. The Morning Night Talk shall 夜と朝のお話の時刻の判定、天気の話の組み立て、好きなものの選び方、失敗のときの扱いを、偽物を使って CI で確かめられるようにする
