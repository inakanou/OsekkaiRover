# Research & Design Decisions

## Summary
- **Feature**: `voice-gateway`
- **Discovery Scope**: Extension（`rover-core` の機能を足す口に、外部のライブラリ faster-whisper を使う機能を足す）
- **Key Findings**:
  - faster-whisper 1.2.1（2025-10-31、MIT）は純粋な wheel。依存（ctranslate2 4.8.5・onnxruntime 1.31.1・av 19.0.2・tokenizers 0.23.3）は、どれも manylinux aarch64 の cp313 向けの wheel がある。Trixie（glibc 2.41）でソースからのビルドは要らない見込み。numpy は apt の 2.x を `--system-site-packages` で使える
  - モデルは CTranslate2 の形（`Systran/faster-whisper-base`、MIT）。base は約 148 MB（`model.bin` 145 MB）、small は約 486 MB。ディレクトリを `WhisperModel(<path>, device="cpu", compute_type="int8")` に渡せば、Hub に繋がずに読める。VAD（Silero）の重みはパッケージに同梱
  - 基盤には音声入力の資源もポートもない。マイクを使うのは音声の入口だけなので、入口の中に閉じる（基盤を変えない）。ハードなしの実行では、端末の 1 行の入力が USR ボタンになっているので、その行の文字を聞き取った言葉として使うのが自然。そのために基盤の偽物（端末の版）に小さな口を足す

## Research Log

### サンプルの聞き取りと振り分け
- **Context**: 子どもから見た振る舞いをサンプルにそろえるため
- **Sources Consulted**: 開発 PC の控え（サンプルの tar の `ears.py`・`chat.py`・`routine.py`・`main.py`・`routine.example.yaml`）。個人の設定と記録は読んでいない
- **Findings**:
  - 押下 → 時間帯の確認（`chat.hours` 既定 07:00〜21:30。外なら「ｵﾔｽﾐ ｼﾞｶﾝ」と「いまは ねる じかんだよ。また あした おはなし しようね」） → うなずき → LCD「ｷｲﾃﾙﾖ」と呼びかけの顔 → 「なあに？おはなし しよう！」 → LED を点けて `arecord -D plughw:CARD=U0x46d0x825,DEV=0 -f S16_LE -r 16000 -c 1 -d 5 -t raw` → 音量を最大 20 倍まで持ち上げる → `WhisperModel("base", cpu, int8, cpu_threads=4)` で `language="ja", beam_size=1, initial_prompt=ヒント, vad_filter=True, condition_on_previous_text=False`
  - 空なら「ごめんね、きこえなかった。もういちど ボタンを おして はなしてね」。ゲームへの振り分けは「だるま」「ダルマ」「転んだ」の部分一致。当てはまらなければ Claude
  - 返事の後 30 秒の押下で続ける（2 回目からは「うん、なあに？」）
  - 聞いた言葉を `print` でそのまま journal に出していた（直す）。おやすみの後でも 21:30 までは話しかけに応じていた
- **Implications**: 言葉・LCD の文字・時間・録音の方法はサンプルにそろえる。ヒント（initial_prompt）には、機能が登録した語句も足す

### rover-core のつなぎ目
- **Sources Consulted**: `src/osekkai/core/{buttons,feature,arbiter,quiet,events,types,ports}.py`、`src/osekkai/adapters/fake/console.py`、`src/osekkai/__main__.py`
- **Findings**:
  - 押下は、BUTTON を借りている機能 → 待ち手（`want_button_presses`）のうち最も優先度の高いもの → 捨てる、の順に届く。日課はタスクの間だけ BUTTON を借りるので、それ以外の押下は音声の入口に届く
  - 資源は VOICE・DISPLAY・LED・BUTTON・NECK・DRIVE だけ。マイクはない
  - 機能は、自分がハードなしで動いているかを知る口を持たない。端末の偽物（`ConsoleButtons`）は、1 行の入力を USR の押下として知らせ、行の文字は捨てている
  - 静かな状態は `ctx.is_quiet` と `QuietChanged` で分かる
- **Implications**: マイクと聞き取りは入口の中のアダプタにする。ハードなしの判定は「faster-whisper がなく、端末の偽物が動いている」で行い、端末の偽物に「最後に入れた行の文字」を渡す口を足す（rover-core の偽物への小さな追加）

### faster-whisper と依存（2026-11-07 に確かめた）
- **Sources Consulted**: https://pypi.org/pypi/faster-whisper/json、ctranslate2・onnxruntime・av・tokenizers の PyPI、https://huggingface.co/Systran/faster-whisper-base、faster-whisper 1.2.1 のソース
- **Findings**:
  - `requires_dist`: `ctranslate2>=4.0,<5`、`huggingface-hub>=0.21`、`tokenizers>=0.13,<1`、`onnxruntime>=1.14,<2`、`av>=11`、`tqdm`。`import faster_whisper` で av・ctranslate2・tokenizers・huggingface_hub を読み込む（CI と PC では入れない）
  - `transcribe(audio: np.ndarray float32 16 kHz mono, language="ja", beam_size=1, vad_filter=True, initial_prompt=..., condition_on_previous_text=False, without_timestamps=True)`。戻りの segments は遅延なので、回して初めて計算が走る。`hotwords` もある
  - Pi 5 での日本語の公表の計測はない。近い報告から、base int8 で 3〜5 秒の発話に 1〜3 秒、メモリは 0.5〜1 GB の見込み。冷却が要る
- **Implications**: 依存は extras の `voice` に分け、版の準備（`pip install -e .[pi,voice]`）で入れる。モデルは初期設定で取得元とハッシュを固定して置く。速さは実機確認で測る（要件 1.5）

### 録音の方法
- **Findings**: `arecord`（alsa-utils、Pi OS に入っている）は Python の依存を足さない。`plughw:CARD=U0x46d0x825,DEV=0` は USB の差し替えで番号が変わっても同じ名前で残る。`pw-record` はユーザーの PipeWire が要る。どちらもユーザーサービスで動く
- **Implications**: サンプルと同じ `arecord` を使う。止めるときは子のプロセスを止める

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| 入口の中にマイクを閉じる（採用） | 録音と聞き取りを入口のアダプタにする | 基盤を変えない。マイクを使う機能は入口だけ | 将来ほかの機能がマイクを使うときは、基盤の資源にし直す | ロードマップでは音声の入力は入口の担当 |
| 基盤に MIC の資源とポートを足す | 録音を基盤の資源にする | 将来の共有に強い | 今は使う機能が 1 つで、早すぎる一般化。rover-core の変更が大きい | 採らない |
| サンプルの形（会話と入口を 1 つに） | 聞いた言葉をその場で Claude に送る | 速く写せる | 入口と会話が混ざる。言葉をログに出していた | 採らない |

## Design Decisions

### Decision: 言葉の登録は入口のパッケージの `api.py` の、プロセスの中で 1 つの登録表で受ける
- **Context**: 要件 2.1〜2.7、3.1〜3.3、4.1。基盤には機能どうしの口がない。structure.md は「機能どうしは相手の `api.py` だけを使う」
- **Selected Approach**: `osekkai.voice.api` に登録の口（`register_words`・`register_fallback`・`listen_again`）を置く。登録はプロセスの中の 1 つの表に入り、入口の機能が読む。登録には「まだ動いているか」を返す口（機能の `stop` の旗）を渡し、止まった機能の登録は使わない
- **Rationale**: 機能と入口の起動の順番に頼らない（どちらが先に始まってもよい）。基盤を変えない
- **Trade-offs**: 登録する機能は `osekkai.voice.api` を読み込む（ロードマップで宣言した依存）

### Decision: 機能に言葉を渡す前に、入口の資源を返す
- **Context**: 要件 2.5。渡した先の機能（会話・あそび）は、自分で声・画面・ボタンを借りて動く
- **Selected Approach**: 文字にし終えたらリースを返し、それから受け手の口を呼ぶ。受け手の口はすぐに戻る（自分のスレッドに仕事を渡す）決まりにする
- **Trade-offs**: 渡すまでの一瞬、待機画面に戻ることがある

### Decision: ハードなしの実行では、端末の 1 行の文字を聞き取った言葉にする
- **Context**: 要件 9.1。端末の偽物は標準入力を読んでいて、入口が別に読むと取り合いになる
- **Selected Approach**: 端末の偽物（`ConsoleButtons`）が、押下を知らせる前に、その行の文字を「最後に入れた行」として取っておく。入口の「打った文字の聞き取り」がそれを取り出す。選び方は設定 `recognizer: auto`（faster-whisper が読み込めれば実機の聞き取り、なければ端末の偽物が動いているときだけ打った文字、どちらもなければ使えない）
- **Trade-offs**: rover-core の偽物（`adapters/fake/console.py`）に小さな口を足す。基盤の本体（core）は変えない

### Decision: モデルは初期設定で置き、実行中は取りに行かない
- **Context**: 要件 6.3〜6.5、7.1（外部に送らない）。サンプルは初回の実行で Hub から取っていた
- **Selected Approach**: 配置の道具の初期設定（models の段階）に、`Systran/faster-whisper-base` の 4 つのファイル（`config.json`・`model.bin`・`tokenizer.json`・`vocabulary.txt`）を、版（コミット）と sha256 を固定して足す。置き場所は `~/.local/share/osekkai/models/faster-whisper-base/`。入口はそのディレクトリだけを読む
- **Trade-offs**: device-deployment の道具に、サブディレクトリに置く小さな変更が要る（越境する変更として、このスペックの PR で行い、道具のテストを足す）

### Decision: 優先度は `INTERACTIVE + 5`（55）
- **Context**: 押下の待ち手の順番。会話（INTERACTIVE）より先に押下を受け、日課（85）より下
- **Trade-offs**: 会話の機能は自分では押下を待たず、続けて聞くを入口に頼む

## Synthesis
- **一般化**: 「語句の一致」と「将来の LLM の判定」を、同じ `IntentMatcher` の口で差し替えられるようにする（2.7）。実装は語句の一致だけ
- **作るか使うか**: 聞き取りは faster-whisper を使う。録音は `arecord` を使う（Python の依存を足さない）。意図の判定は作る（数十行の一致の判定で足りる）
- **簡単にする**: マイクを基盤の資源にしない。会話の 1 日の上限は持たない（会話の担当）

## Risks & Mitigations
- Pi 5 で日本語の base の精度が足りない — 設定でモデルを small にできるようにする。実機確認で測る
- 依存（onnxruntime など）の版が新しすぎて実機で動かない — `ctranslate2>=4.4,<5` などで幅を決め、実機確認で版を記録する
- C270 のマイクの音が小さい・ひずむ — サンプルと同じく音量を持ち上げる（上限 20 倍）。実機確認で聞き取りの結果を見る
- モデルの読み込みで起動が遅れる — 入口のスレッドで読み込み、終わるまでは「じゅんび しているよ」と答える。ほかの機能（日課）は待たない

## References
- [faster-whisper（PyPI）](https://pypi.org/project/faster-whisper/) — 1.2.1、MIT
- [Systran/faster-whisper-base](https://huggingface.co/Systran/faster-whisper-base) — CTranslate2 の形のモデル、MIT
- [pw-cat の man](https://docs.pipewire.org/page_man_pw-cat_1.html) — 録音のほかのやり方（採らない）
