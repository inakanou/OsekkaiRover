# Requirements Document

## Project Description (Input)
ロボットの機能は今後増えていく（仕事や生活の補佐）。使う人（子どもと保護者）は、ボタンを押して話しかけるだけで、どの機能にもたどり着けるようにしたい。

今は、サンプルの `ears.py` と `chat.py` が、ボタンを押すと「なあに？」と聞いて 5 秒録音し、実機の中で faster-whisper で文字にしている。聞いた言葉は、まずゲームへの振り分けに渡し、当てはまらなければ Claude との会話に送る。入口と会話が混ざっていて、子どもの言葉をログにそのまま出し、おしゃべりが無効だとボタンの押下が誰にも消費されない。

`rover-core` の機能として音声の入口（voice-gateway）を作る。ボタンを押すとロボットが聞く姿勢になり（声・LCD・LED）、話した言葉を実機の中で文字にし、各機能が自分で登録した言葉（意図）に当てはまる機能へ渡す。どれにも当てはまらない言葉は、受け手として登録された機能（`conversation`）へ渡し、受け手がなければやさしく分からなかったと伝える。入口は機能の中身を知らず、子どもの言葉をログや記録に残さない。振り分けは偽の聞き取りで PC でテストできるようにする。詳しくは `.kiro/specs/voice-gateway/brief.md` を参照。

## Introduction
音声の入口（Voice Gateway）は、ロボットに話しかけるための共通の入口です。子どもがボタンを押すと、ロボットは「なあに？」と聞き、話した言葉を実機の中で文字にします。その言葉を、言葉を登録した機能（あそび・今後の補佐機能など）へ渡します。どれにも当てはまらない言葉は、おしゃべりの機能（`conversation`）へ渡します。

音声の入口は、ボタンが押されたときに声・画面・LED・ボタンを `rover-core` から借ります。機能の中身は知らず、機能を足しても入口を書き換えません。

この文書で使う言葉:
- **聞き取り**: マイクで録音した声を文字にすること。実機の中だけで行う
- **言葉の登録**: 機能が「この言葉が来たら自分に渡してほしい」と入口に申告すること（例: あそびの機能が「だるまさん」を登録する）
- **受け手**: どの登録にも当てはまらない言葉を受け取る機能。1 つだけ登録できる（`conversation` を想定）
- **続けて聞く**: 言葉を受け取った機能が、返事の後にもう一度だけ子どもの言葉を聞くよう求めること。そのときは振り分けをせず、同じ機能に渡す
- **聞ける時間帯**: ボタンを押したときに聞き取りを始めてよい時間帯。保護者が設定する

## Boundary Context
- **In scope**:
  - ボタンを押してから聞き取りまでの流れ（聞く姿勢の声・LCD・LED、録音、文字にする）
  - 言葉の登録の口と、登録に基づく振り分け
  - 当てはまらない言葉の受け手の登録と、受け手がないときの返事
  - 続けて聞く口
  - 聞ける時間帯と、おやすみモードの間の扱い
  - 聞き取れなかったとき・聞き取りの準備ができていないときの返事
  - マイクと聞き取りの設定、self-check の項目
  - 子どもの言葉と声を残さない・外に出さないこと
  - ハードなしで、聞き取りの代わりに文字を入れて試せること
- **Out of scope**:
  - Claude との会話、会話の 1 日の利用上限（`conversation`）
  - 各機能が登録する言葉の中身と、言葉を受け取った後の振る舞い（各機能）
  - 呼びかけの言葉（ウェイクワード）での起動（将来の検討）
  - USB カメラの映像（`presence`、`remote-control`）。マイクは同じ機械（C270）だが、映像の扱いは持たない
  - ボタンの押下を誰に届けるかの調停（`rover-core`）
- **Adjacent expectations**:
  - `rover-core` は、優先度の高い機能（日課）がボタンを借りている間は、押下をその機能に届け、音声の入口には届けない
  - `rover-core` は、日課が資源を求めたら、聞いている途中の音声の入口に中断を知らせる
  - `daily-routine` は、おやすみモードを `rover-core` の静かな状態で知らせる
  - `conversation` は、受け手として登録し、必要なら続けて聞くを求める。聞ける時間帯の外とおやすみモードの扱いは音声の入口に任せ、1 日の利用上限は自分で持つ
  - `daruma-game` などの機能は、自分の言葉を登録し、言葉を受け取ったら自分で資源を借りて動く

## Requirements

### Requirement 1: ボタンを押して聞く
**Objective:** As a 子ども, I want ボタンを押すとロボットが聞いてくれること, so that 話しかけるだけでロボットと関われる

#### Acceptance Criteria
1. When ほかの機能がボタンを使っていないときにボタンが押されたとき, the Voice Gateway shall 首で 1 回うなずき、LCD に「ｷｲﾃﾙﾖ」と呼びかけの顔を出し、「なあに？」と話してから聞き取りを始める
2. While 録音している間, the Voice Gateway shall LED を点け、録音が終わったら LED を消す
3. The Voice Gateway shall 録音を、設定した長さ（既定は 5 秒）で終える
4. When 録音が終わったとき, the Voice Gateway shall LCD に「ｶﾝｶﾞｴﾁｭｳ」を出して、文字にし終えるまで表示を続ける
5. The Voice Gateway shall 5 秒の録音を文字にするのを、実機で 3 秒以内に終えることを目標とし、実機確認で測った時間を記録する
6. While 聞く姿勢から文字にし終えるまでの間, the Voice Gateway shall 次のボタンの押下で聞き取りを重ねて始めない
7. When 聞いている途中で優先度の高い機能（日課など）が資源を求めたとき, the Voice Gateway shall 録音と文字にする処理をやめ、その言葉をどの機能にも渡さずに捨て、資源を手放す

### Requirement 2: 言葉の登録と振り分け
**Objective:** As a 開発者, I want 機能が自分の受け付ける言葉を入口に登録できること, so that 機能を足しても入口を書き換えずに、声からその機能を呼べる

#### Acceptance Criteria
1. The Voice Gateway shall 機能が、自分の名前と、受け付ける言葉（1 つ以上の語句）と、言葉が来たときに呼ぶ口を登録できるようにする
2. When 文字にした言葉が、登録された語句を含むとき, the Voice Gateway shall その言葉をその機能に渡す
3. The Voice Gateway shall 語句の一致を、ひらがなとカタカナの違い、空白、句読点を無視して判定する（例: 「ダルマさん」は「だるまさん」に一致する）
4. When 1 つの言葉が 2 つ以上の機能の語句に一致したとき, the Voice Gateway shall 優先度の高い機能に渡し、同じ優先度なら先に登録した機能に渡す
5. When 機能に言葉を渡すとき, the Voice Gateway shall 自分が借りていた資源を返してから渡し、渡した先の機能が資源を借りて動けるようにする
6. When 機能が止まったとき, the Voice Gateway shall その機能の登録を外す
7. The Voice Gateway shall 振り分けの判定のしかたを、語句の一致から別の判定（例: LLM による意図の判定）へ、登録の口を変えずに差し替えられるようにする

### Requirement 3: 当てはまらない言葉の受け手
**Objective:** As a 子ども, I want 機能の言葉に当てはまらないことを話しても、ロボットが応えてくれること, so that 何を話しても無視されない

#### Acceptance Criteria
1. The Voice Gateway shall 機能が 1 つだけ、当てはまらない言葉の受け手として登録できるようにする
2. If 受け手がすでに登録されているときに別の機能が受け手として登録しようとしたとき, the Voice Gateway shall 後の登録を断り、そのことを記録する
3. When 文字にした言葉がどの語句にも一致しなかったとき, the Voice Gateway shall その言葉を受け手に渡す
4. If 受け手が登録されていないときに、言葉がどの語句にも一致しなかったとき, the Voice Gateway shall 「ごめんね、まだ わからないんだ」と話して終える

### Requirement 4: 続けて聞く
**Objective:** As a 子ども, I want ロボットの返事の後に、もう一度ボタンを押して続きを話せること, so that おしゃべりを続けられる

#### Acceptance Criteria
1. The Voice Gateway shall 言葉を受け取った機能が、続けて聞くを求められるようにする
2. When 続けて聞くが求められたとき, the Voice Gateway shall 決めた時間（既定は 30 秒）の間ボタンの押下を待ち、押されたら「うん、なあに？」と話して聞き取りを始める
3. When 続けて聞いた言葉を文字にしたとき, the Voice Gateway shall 振り分けをせず、続けて聞くを求めた機能に渡す
4. If 続けて聞くの待ちの間にボタンが押されなかったとき, the Voice Gateway shall 何も話さずに待ちを終える
5. If 続けて聞くを求めた機能が止まったとき, the Voice Gateway shall 続けて聞くの待ちを終える

### Requirement 5: 聞ける時間帯とおやすみモード
**Objective:** As a 保護者, I want 夜はロボットが話しかけに応じないこと, so that 子どもが夜更かしせず、寝ている家族を起こさない

#### Acceptance Criteria
1. The Voice Gateway shall 聞ける時間帯を設定で受け付け、既定を 7 時から 21 時 30 分までとする
2. When 聞ける時間帯の外にボタンが押されたとき, the Voice Gateway shall 聞き取りを始めず、LCD に「ｵﾔｽﾐ ｼﾞｶﾝ」と眠った顔を出し、「いまは ねる じかんだよ。また あした おはなし しようね」と話す
3. While おやすみモード（`rover-core` の静かな状態）の間, the Voice Gateway shall ボタンが押されても声を出さず、画面を点けず、聞き取りを始めない
4. While 時刻が確かでない間, the Voice Gateway shall 聞ける時間帯の外として扱う
5. While 続けて聞くの待ちの間に聞ける時間帯が終わったとき、またはおやすみモードになったとき, the Voice Gateway shall 待ちを終える

### Requirement 6: 聞き取れなかったとき・聞けないとき
**Objective:** As a 子ども, I want うまく聞き取れなかったときも、ロボットがやさしく教えてくれること, so that もう一度話せばいいと分かる

#### Acceptance Criteria
1. If 文字にした言葉が空だったとき（無音・雑音だけ）, the Voice Gateway shall 「ごめんね、きこえなかった。もういちど ボタンを おして はなしてね」と話し、どの機能にも渡さない
2. If 録音や文字にする処理が失敗したとき, the Voice Gateway shall 失敗の種類だけを記録し、6.1 と同じ言葉を話して終える
3. If 起動の後、聞き取りの準備（モデルの読み込み）が終わる前にボタンが押されたとき, the Voice Gateway shall 「ちょっと まってね。じゅんび しているよ」と話し、聞き取りを始めない
4. If マイクが見つからない、または聞き取りの準備に失敗したとき, the Voice Gateway shall ボタンが押されたら「いまは おはなし できないんだ」と話し、そのことを self-check で知らせる
5. The Voice Gateway shall 聞き取りの準備を、ロボットの起動のときに始め、ボタンが押されてから始めない

### Requirement 7: 子どもの言葉と声を守る
**Objective:** As a 保護者, I want 子どもの話した言葉や声が残ったり外に出たりしないこと, so that 安心して話しかけさせられる

#### Acceptance Criteria
1. The Voice Gateway shall 録音した声を、実機の中だけで文字にし、外部に送らない
2. The Voice Gateway shall 録音した声を、文字にし終えたら捨て、ファイルとして残さない
3. The Voice Gateway shall 文字にした言葉を、ログ・記録・self-check の表示に出さない
4. The Voice Gateway shall ログには、決まった文言（聞き取りを始めた・振り分けた先の機能の名前・聞き取れなかった・失敗の種類・かかった時間）だけを出す
5. The Voice Gateway shall 言葉を渡した先の機能の名前は記録してよいが、どの語句に一致したかは記録しない

### Requirement 8: 設定と self-check
**Objective:** As a 保護者, I want マイクや聞き取りの設定を自分の設定ファイルで変えられ、使えるかどうかを確かめられること, so that 実機の機械に合わせて調整できる

#### Acceptance Criteria
1. The Voice Gateway shall マイクの機械、録音の長さ、聞き取りのモデルの大きさ、聞ける時間帯、続けて聞くの待ちの長さを、リポジトリの外の利用者の設定ファイルから読む
2. When 設定に値が書かれていないとき, the Voice Gateway shall 決められた既定値（録音 5 秒、続けて聞くの待ち 30 秒、聞ける時間帯 7 時〜21 時 30 分）を使う
3. If 設定の値が誤っているとき（範囲の外、時間帯の書き方の誤りなど）, the Voice Gateway shall 誤りの場所と理由を、値を出さずに知らせ、音声の入口を開始しない
4. When self-check が実行されたとき, the Voice Gateway shall マイクが見つかるか、聞き取りのモデルが置かれているかを、決まった理由の文言で知らせる
5. Where 設定で音声の入口が無効にされているとき, the Voice Gateway shall ボタンの押下を受け取らず、ほかの機能に任せる

### Requirement 9: PC での試しとテスト
**Objective:** As a 開発者, I want 実機やマイクがなくても、振り分けと流れを PC で確かめられること, so that 子どもが使う実機で試す前に、大半を確かめられる

#### Acceptance Criteria
1. Where ハードなしの実行が指定されたとき, the Voice Gateway shall ボタンを押した後の聞き取りの代わりに、端末から入れた文字を、聞き取った言葉として扱う
2. The Voice Gateway shall 振り分け（語句の一致・優先度・受け手・続けて聞く）を、聞き取りを偽物に差し替えて、実機のデバイスなしで確かめられるようにする
3. When 開発用の聞き取りの確認のコマンドを実行したとき, the Voice Gateway shall 1 回だけ録音して文字にし、文字にした言葉とかかった時間を端末にだけ出し、ログには出さない
4. If 開発用の聞き取りの確認のコマンドを、ロボットが動いているときに実行したとき, the Voice Gateway shall 録音を始めずに、その理由を示して終える
