# Research & Design Decisions

## Summary
- **Feature**: `conversation`
- **Discovery Scope**: Extension（`rover-core` の機能を足す口と `voice-gateway` の受け手の口を使う。外部の API が 1 つ加わる）
- **Key Findings**:
  - Claude API は `claude-opus-5-5` を使う。思考は止められない（`thinking` を渡さない。`{"type": "disabled"}` は 400）。深さは `output_config={"effort": "low"}` で下げる（Opus 5.5 の既定は `medium`）。`temperature` などは渡さない
  - 拒否の代わりの実行は、beta の `server-side-fallback-2026-07-01` と `fallbacks="default"` の組み合わせが今も有効。Python SDK 2.6.0 の `client.beta.messages.create` は `fallbacks` と `betas` と `output_config` を引数として受け付ける（`extra_body` は要らない）。すべてが断ったときだけ `stop_reason == "refusal"` が返る
  - Opus 5.5 は「preserved thinking」を行う。思考のブロックを送り返す場合は、前のやりとりを書き換えると 400 になる（2026-08-31 以降に作ったアカウント）。おしゃべりは古いやりとりを先頭から捨てる（要件 2.4）ので、**思考のブロックを送り返さず、返事の文字だけを残す**

## Research Log

### Claude API の呼び出し方（claude-api のスキル、2026-11-07 に確認）
- **Context**: brief と技術構成で、モデル名・effort・fallback の組み合わせを設計の段階で確かめることにしていた
- **Sources Consulted**: claude-api のスキル（`SKILL.md`、`python/claude-api/README.md`、`shared/model-migration.md` の Migrating to Claude Opus 5 / 5.5 / Fable 5.1 の節、`shared/error-codes.md`、`shared/prompt-caching.md`）、PyPI の `anthropic`（最新 2.6.0、Python 3.10 以上）、作業用の仮想環境に入れた SDK 2.6.0 の引数と例外の型
- **Findings**:
  - モデル: `claude-opus-5-5`（入力 $4 / 出力 $20 / 100 万トークン、キャッシュの読み出し $0.20）
  - 思考: 常に有効。`thinking` は渡さない。`effort` の既定は `medium` なので `low` を明示する
  - fallback: ヘッダー `server-side-fallback-2026-07-01` と `fallbacks: "default"`。拒否の種類ごとに Anthropic が代わりのモデルを選ぶ。配列の形（`-2026-06-01`）と混ぜると 400
  - 拒否: HTTP 200、`stop_reason == "refusal"`、`stop_details.category`。中身を読む前に `stop_reason` を確かめる
  - 例外（SDK 2.6.0）: `APITimeoutError` は `APIConnectionError` の子なので先に捕まえる。`OverloadedError`（529）は `InternalServerError` の子。`RateLimitError`（429）。`APIStatusError` がそのほかの状態コード
  - クライアント: `timeout` は秒、`max_retries` の既定は 2（429・5xx・接続の失敗を自動でやり直す）。やり直すと待つ時間が `timeout ×（max_retries + 1）` まで延びる
  - キャッシュ: 最新のモデルは 512 トークン以上の前置きをキャッシュする。上の階層に `cache_control={"type": "ephemeral"}` を付ければ、決まりの文（system）がキャッシュに入る
  - preserved thinking: 思考のブロックは、それより前の system・tools・messages が同じであるときだけ有効。先頭のやりとりを消すと、その後の思考のブロックが 400 になる
- **Implications**: アダプタは `client.beta.messages.create` を 1 回呼ぶ。`max_retries=0` と `timeout=返事を待つ時間` にし、待つ長さを要件 6.4 の 20 秒に収める。会話のやりとりには返事の文字だけを残す

### voice-gateway の受け手の口
- **Context**: 要件 1.6・2.x は `voice-gateway` の続けて聞く口に頼る
- **Sources Consulted**: `src/osekkai/voice/api.py`、`intents.py`、`flow.py`、`feature.py`
- **Findings**:
  - `register_fallback(owner, handler, alive=...)`。受け手は 1 つだけで、ほかに生きている受け手があれば `FallbackTaken`
  - 入口は資源を返してから `handler(Heard)` を呼ぶ。`handler` はすぐに戻り、重い仕事は自分のスレッドで行う
  - `listen_again(owner)` は、返事を話し終えて資源を返した後に呼ぶ。入口は `follow_up_seconds`（既定 30 秒、5〜120 秒）の間ボタンを待つ。押されなかったことは受け手に知らせない
  - 入口は、自分の時間帯（07:00〜21:30）の外・時刻が確かでないとき・静かな状態のときは聞かない
  - 振る舞いの間はボタンも借りておく。借りていないと、押下が入口に届いて聞き取りが始まる
- **Implications**: やりとりを捨てる時（2.2）は、おしゃべりの側で決める。入口の待ちの上限（120 秒）より長い 130 秒の間、続けての言葉が来なければ捨てる

### 基盤の口（rover-core）
- **Sources Consulted**: `core/feature.py`、`core/handles.py`、`core/voice.py`、`core/storage.py`、`core/selfcheck.py`、`features/routine/records.py`、`features/presence/*`
- **Findings**:
  - `VoiceHandle.say(text, keep=False)` は、合成した声を再生の後に消す（キャッシュに入れない）。要件 8.2 にそのまま使える
  - `StateStore.load` は、壊れたファイルを退避して `None` を返す。読み出しの権限の誤りなどは例外になる。日課は、読む前にファイルがあるかを見て「壊れていた」を見分けている（`records.py`）
  - self-check の結果は OK・FAILED・SKIPPED。見つけてあいさつは、ライブラリがないときを SKIPPED にしている
  - `typed_input_active()`（`adapters/fake/console.py`）で、端末の偽物が動いているかが分かる。音声の入口も、これで打った文字の聞き取りを選んでいる
- **Implications**: 回数の記録の「読めない」（5.6）は、日課と同じ見分け方を使う。PC の偽の返事（10.2）は `typed_input_active()` で選ぶ

### 鍵の置き場所
- **Sources Consulted**: `deploy/rover_deploy/provision.py`（secrets の手順）、`deploy/systemd/osekkai.service`
- **Findings**: 初期設定は `~/.config/osekkai/env` に「名前=値」の行で書くよう案内し、ファイルの中は読まない。サービスはこのファイルを環境変数として読み込んでいない
- **Implications**: おしゃべりは、このファイルを自分で読む。持ち主が本人で、ほかの人が読めないときだけ使う。SDK の環境変数やプロファイルには頼らず、`api_key` を明示して渡す

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| ポートとアダプタ（採用） | 会話の AI を `ChatModelPort` にし、実機は Anthropic SDK、PC は台本の偽物 | 既存の機能と同じ形。CI で SDK を読み込まずにテストできる | アダプタと SDK の対応は CI で確かめにくい（偽の SDK の型で確かめる） | 実機確認で実際の API を通す |
| SDK を機能の中で直接呼ぶ | ポートなし | 部品が少ない | テストで SDK が要る。技術構成の決まり（ハードのライブラリはアダプタの中だけ）に反する | 不採用 |

## Design Decisions

### Decision: 会話のやりとりには返事の文字だけを残す
- **Context**: 要件 2.4（古いやりとりから使わない）と、Opus 5.5 の preserved thinking（先頭を消すと後の思考のブロックが 400）がぶつかる
- **Alternatives Considered**:
  1. 返事の中身（思考のブロックを含む）をそのまま残し、10 往復を超えたら会話を作り直す
  2. `thinking-binding-controls-2026-08-01` と `prefix_mismatch_behavior: "drop_block"` で、合わないブロックを API に捨てさせる
  3. 返事の文字だけを残す（思考のブロックを送り返さない）
- **Selected Approach**: 3
- **Rationale**: 思考のブロックがなければ、先頭を捨てても確かめる対象がない。返事は 1〜2 文で、前の思考の中身がなくても話の流れは保てる。effort は low で、`display` の既定は「omitted」なので、思考のブロックの文は空。捨てても失うものがほとんどない。2 は beta が 1 つ増え、1 は 2.4 の振る舞いを変える
- **Trade-offs**: 前の回の思考を次の回で使えない（返事の質への影響は小さい）
- **Follow-up**: 実機確認で、続けて 3 回以上話して 400 にならないことを確かめる

### Decision: 返事を待つ長さは、こちらの締め切りで守る
- **Context**: 要件 6.4（20 秒で待つのをやめる）。SDK の `timeout` は通信の段階ごとの長さで、合計の上限ではない
- **Selected Approach**: SDK は `timeout=reply_timeout_seconds`、`max_retries=0` にする。そのうえで、問い合わせを別のスレッドで行い、流れの側は締め切りまでしか待たない。締め切りを過ぎた問い合わせの結果は捨てる
- **Rationale**: 遅いネットワークでも、子どもを 20 秒より長く待たせない。自動のやり直しは待つ時間を 3 倍に延ばすので使わない（子どもがもう一度押せばよい）

### Decision: 回数の記録が読めないときは、その日を上限に達したことにして書き残す
- **Context**: 要件 5.6。基盤の `StateStore.load` は壊れたファイルを退避して `None` を返すので、そのままでは次の起動で 0 から数えてしまう
- **Selected Approach**: 読む前にファイルがあるかを見る（日課と同じ）。あったのに `None` か、読み出しで例外のときは「読めない」とし、その日の記録を「上限に達した」で書き直す。書き直しにも失敗したら、その日の間は記憶の中だけで止める
- **Trade-offs**: 記録が壊れた日は、その日いっぱい話せない（要件で決めたとおり）

### Decision: やりとりを捨てる時をおしゃべりの側で決める
- **Context**: 要件 2.2。入口は、続けて聞く待ちが押されずに終わったことを知らせない
- **Selected Approach**: 最後に話し終えた時から 130 秒（入口の待ちの上限 120 秒より長い）たったら、やりとりを捨てる。続けての言葉でない言葉が来たときも捨てる（2.3）
- **Rationale**: 入口の口を変えずに済む。入口が受け付けない時間を過ぎてから捨てるので、子どもから見た振る舞いは「押さなければ終わる」と同じになる
- **Follow-up**: 入口が「待ちの終わり」を知らせる口を持ったら、そちらに替える（Revalidation Triggers）

### Decision: モデルは設定にしない
- **Context**: 要件 9.5 は、時間帯・上限・往復の数・待つ時間・呼び名だけを設定にする
- **Selected Approach**: モデル名と effort はアダプタの定数にする（`claude-opus-5-5`、`low`）
- **Rationale**: 項目を増やさない。変えるときは版を上げる。費用の見込みは、1 回に入力が約 1,000 トークン・出力（思考を含む）が約 300 トークンで約 1 円。60 回で 1 日に約 60 円が上限の目安（決まりの文はキャッシュに入るので、実際はこれより少ない）

## Synthesis Outcomes
- **Generalization**: 「決まった言葉で答えて会話を終える」場面（時間帯の外・時刻が確かでない・上限・鍵がない・回数の記録が読めない）は、1 つの「門」の判定にまとめる。判定は純粋な関数にして、理由ごとの言葉を対応表で引く
- **Build vs Adopt**: 会話の AI は Anthropic の Python SDK を使う（自前の HTTP は書かない）。回数の記録は基盤の `StateStore` を使う。時間帯の判定は、音声の入口の同じ考え方（始めを含み終わりを含まない、日をまたげる）を小さく持つ（入口の内部の関数は、機能から読み込めないため）
- **Simplification**: 応答の流れ（受け取り → 門 → 問い合わせ → 話す → 数える → 続けて聞く）は 1 つの部品にする。セッションの管理は、やりとりの列と最後に使った時刻だけを持つ小さな型にする。モデルの切り替えや会話の保存の口は作らない

## Risks & Mitigations
- 思考のブロックを送り返さない形が、将来の API の変更で断られる — 例外は「そのほかの誤り」として決まった言葉で答え、ロボットは止まらない。実機確認で続けての会話を確かめる
- SDK の版が上がって例外の型が変わる — 依存を `anthropic>=2.6,<3` に絞る。アダプタのテストは、SDK と同じ親子関係の偽の型で確かめる
- gitleaks の誤検知（鍵の名前と値の組み合わせ） — 鍵の変数名・テストの偽の値に `key`・`token`・`secret` を入れない。テストの値は 10 文字未満にする。文書の例の値は `<ここに鍵>` の形にする
- 聞き取りが呼び名を漢字で書く（例: 名前の漢字）— かなの違いだけでは置き換えられず、要件 4.1 に反して送られてしまう。設定に「呼び名の別の書き方」（`call_name_variants`）を持ち、呼び名と同じく目印に置き換える。文書で、子どもの名前の漢字を書いておくよう案内する

## References
- claude-api のスキル（2.1.317 に同梱）: `shared/model-migration.md` の Migrating to Claude Opus 5.5、Migrating to Claude Opus 5（New API features）、Migrating to Claude Fable 5.1 from Claude Fable 5（Breaking change 3）
- PyPI: https://pypi.org/project/anthropic/ （2.6.0）
- 拒否と fallback: https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback
