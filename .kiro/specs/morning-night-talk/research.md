# 調べたこと（morning-night-talk）

## ギャップの分析（2026-11-20）

### 1. 今あるもの

| 領域 | 今あるもの | 場所 |
| --- | --- | --- |
| 機能の登録 | entry point（`osekkai.features`）で登録し、`FeatureSpec(name, priority, resources, config_model, checks)` を申告する。`enabled: false` や設定の誤りなら起動しない | `pyproject.toml:49-55`、`core/registry.py:52-87`、`core/feature.py:36-44` |
| 優先度と調停 | 帯は BACKGROUND 10 / REMOTE 30 / INTERACTIVE 50 / SCHEDULED 80。日課は 85。資源は全部か無しで借りる。高い優先度が来たら `on_preempt` を 1 回呼び、2 秒の猶予の後に取り上げる。`speak` は言い終えるまでロックを持つ | `core/types.py:22-29`、`core/arbiter.py`、`core/voice.py:66,92` |
| 静かな状態 | `ctx.is_quiet`・`QuietChanged` の知らせ・再起動の前の状態 | `core/quiet.py`、`core/events.py:26-29` |
| 保存とキャッシュ | 機能ごとの `ctx.store`（一時ファイルから置き換え）と `ctx.cache_dir`。声は `keep=False` なら残さない | `core/feature.py:159`、`core/voice.py:135-168` |
| 時計 | `ctx.clock`（差し替えられる）、`is_synchronized()` | `core/clock.py:44-47` |
| self-check | `Check("<機能>.<項目>", fn)`。決まった言葉だけを出す | `core/selfcheck.py:48-129`、`features/conversation/checks.py:57-72` |
| 日課の公開の情報 | `RoutineStatus(next_task_at, in_task, asleep, updated_at)` だけ。`in_task` はタスクか予告の最中 | `features/routine/api.py:14-28` |
| 日課の中の判定 | 寝るタスク・起床時刻・予告の窓・予告を言ったかの記録はあるが、中だけで使っている | `features/routine/schedule.py:124-211`、`routine/config.py:67-117` |
| 日課の情報の使い方の例 | だるまさんが `next_task_at` と `in_task` を見て早めに終える | `features/daruma/rules.py:96-109` |
| 会話の AI | `ChatModelPort.reply(system, turns, timeout_s)` は例外を出さず `Replied / Refused / Failed(kind)` を返す。`ClaudeChatModel`（`claude-opus-5-5`、再試行なし）、偽物 `scripted.py` | `features/conversation/ports.py:16-64`、`adapters/claude.py`、`adapters/scripted.py` |
| 子ども向けの決まり | `SYSTEM_PROMPT`、`NameGuard`（呼び名を `{name}` に置き換えて送る）、`shape_reply`（記号と絵文字を消し、2 文・120 文字まで） | `conversation/texts.py:16-38`、`conversation/privacy.py` |
| 鍵 | `~/.config/osekkai/env` の `ANTHROPIC_API_KEY`（持ち主だけが読める権限でないと使わない） | `conversation/envfile.py` |
| 声と画面の言葉 | 声はひらがな中心で言葉の間に空白。LCD は 16 文字の半角カタカナ。1 行は 300 文字まで | `core/voice.py:31-49`、`routine/texts.py` |
| HTTP | 外へ問い合わせる道具は**ない**（ラジコンの Web は受ける側だけ） | `features/remote/web.py` |

### 2. 要件ごとの対応と足りないもの

| 要件 | 使えるもの | 足りないもの |
| --- | --- | --- |
| 1 設定 | `FeatureConfig`（`enabled` の既定は True） | **Constraint**: 既定を「使わない」にするには、この機能の設定で `enabled` の既定を False に上書きする必要がある（registry がそれを見るかは要確認）。現在地の型（緯度・経度、または地域のコード）と丸め方 |
| 2・3 話す時 | `RoutineStatus.in_task`・`asleep`・`next_task_at` | **Missing**: 寝るタスクの時刻、朝の最初のタスクの時刻、予告の早さ、予告を言い終えたか、朝の最初のタスクが終わったか、今日の種類に寝るタスクがあるか。どれも日課の中にあるが、公開されていない。日課の `api.py` を足す変更が要る（`daily-routine` のスペックの更新） |
| 4 1 日に 1 回 | `ctx.store`、会話の `DailyUsage` の形（読めなければ上限に達したとみなす） | 小さな記録（日付ごとに夜・朝を話したか、直前に選んだ好きなもの）の新しい形 |
| 5 天気の話 | なし | **Missing**: 天気の情報のポートとアダプタ、偽物。HTTP の道具（標準ライブラリの `urllib` で足りるか、依存を足すか）。天気の種類（WMO の天気のコードなど）→ ひらがなと半角カタカナへの対応表。**Unknown**: 注意報・警報の出どころ |
| 6 豆知識 | `ChatModelPort` と Claude のアダプタ、`shape_reply`、`SYSTEM_PROMPT` | **Constraint**: 会話には `api.py` がなく、ほかの機能は使えない（`structure.md` の決まり）。共有するには、会話に `api.py` を足すか、基盤へ移すか、写しを持つかを決める |
| 7 流れと長さ | `lease.voice.say`、LCD の表示、待機画面への戻り | 先に用意しておく仕組み（5 秒以内に話し始めるため、予告の時刻の前に天気と豆知識を取っておく）。用意したものは話し終えたら捨てる |
| 8 譲り合い | 調停（`on_preempt`）、`is_quiet`、`ResourceBusy` | 優先度の帯の選び方（日課より低く、会話やあそびとは「待つ」）。待つ期限の判定 |
| 9 外部に送るもの | `NameGuard`、ログに値を出さない決まり | 現在地を丸める決まり。Claude に送る言葉の組み立て（好きなもの・夜か朝か・日付と季節だけ） |
| 10 失敗 | `Failed(kind)` の形、機能だけが止まる基盤の作り | 天気の失敗の種類（つながらない・時間切れ・形が違う・古い） |
| 11 self-check と試し | `Check`、日課の試しの実行のコマンド（`--try` 系） | 試しのお話のコマンドの入口（日課の試しのコマンドの作りを確かめて合わせる） |
| 12 PC での確かめ | 偽の時計、偽の会話の AI | 偽の天気 |

### 3. 進め方の候補

#### 案 A: 日課の中に足す
- 日課の予告の後に、天気と豆知識を話す段を足す
- ✅ 予告に続けて話すのが簡単。日課の公開の情報を足さずに済む
- ❌ 日課が外部のネットワークと Claude に依存する。最も大事な機能が、通信の失敗の影響を受ける場所になる（「毎日の習慣を壊さない」に反する）
- ❌ `daily-routine` の要件の「日課の振る舞いを変えない」にも、このスペックの Out of scope にも反する

#### 案 B: 新しい機能として作り、日課と会話の公開の口を足す
- `features/talk/`（名前は設計で決める）を新しく作り、日課の `api.py` に、名前を出さない時刻と真偽値を足す
- 会話の AI は、会話の `api.py` を足して、ポート・Claude のアダプタ・子ども向けの決まりの一部（`shape_reply` など）を公開する
- ✅ 依存の向きが「機能 → 日課と会話の公開の口」だけになる。日課は通信を知らない
- ✅ ロードマップの依存（rover-core, daily-routine, conversation）と合う
- ❌ 日課と会話の 2 つの既存のスペックに、小さな更新が要る

#### 案 C: 新しい機能として作り、会話の AI の部品は写しを持つ
- 日課の `api.py` は案 B と同じく足す。会話の AI のアダプタと決まりは、この機能の中に写しを持つ（会話の設定で時刻の読み方を写した前例がある）
- ✅ 会話のスペックを触らない
- ❌ 子ども向けの決まりと鍵の読み方が 2 か所になり、ずれる危険がある（安全と個人データの決まりなので、ずれると困る）

### 4. 大きさと危険

- **大きさ: M**（3〜7 日）。基盤の作りに沿った新しい機能だが、外部の天気の問い合わせ（新しい種類のつながり）と、日課の公開の口を足す変更がある
- **危険: 中**。天気の出どころ（特に注意報・警報）が未確定。気象庁の警報・注意報は 2026 年度の出水期から形式が変わる予定と案内されており、今の形に合わせると壊れるおそれがある

### 5. 設計で調べること（Research Needed）

1. **天気の出どころ**: キーの要らない候補として Open-Meteo（日ごとの `weather_code`（WMO のコード）、最高と最低の気温、`precipitation_probability_max`）。利用条件（個人・非商用）、気象庁のモデルを選べるか、日本で降水確率が空になる場合がないかを確かめる
2. **注意報・警報の出どころ**: 気象庁の防災情報の JSON か XML。地域のコード（市区町村）の決め方、利用規約と出典の表示、2026 年度からの警報の形の変更に合わせられるか。取れないときは触れない（要件 5.10）ので、最初の版で外す選択も残す
3. **HTTP の道具**: 標準ライブラリの `urllib.request` で足りるか（時間切れ・TLS）。依存を足すなら extras に分ける
4. **会話の AI の共有の仕方**: 会話に `api.py` を足して何を出すか（ポート・アダプタを選ぶ関数・`shape_reply`・鍵の確かめ）。豆知識の頼み方（1 往復、温度、長さ）は claude-api のスキルで確かめる
5. **日課に足す公開の情報**: 寝るタスクの時刻、朝の最初のタスクの時刻、それぞれの予告の時刻、予告を言い終えたか、最初のタスクが終わったか。名前と言葉を出さない形にする。日課の 1 秒ごとの更新で足りるか
6. **`enabled` の既定**: この機能だけ既定を False にできるか（`FeatureConfig` と registry）
7. **試しのお話のコマンド**: 日課の試しの実行の入口に合わせる
8. **現在地の丸め**: 緯度・経度を小数 2 桁（約 1 km）か 1 桁（約 10 km）にするか。注意報・警報の地域のコードとの関係

### 6. 設計への勧め

- 案 B を勧める。日課と会話に、名前を出さない小さな公開の口を足し、新しい機能はそれだけを使う
- 天気は「先に取っておく」形にする（予告の時刻の少し前に問い合わせる）。話す時に待たせない（要件 7.4）
- 注意報・警報は、天気の情報と別のポートにして、取れなくても天気の話ができるようにする

### 出典
- [Open-Meteo の降水確率の説明](https://openmeteo.substack.com/p/precipitation-probability-uv-index)
- [Open-Meteo の利用条件](https://open-meteo.com/en/features#terms)
- [気象庁防災情報 XML の資料](https://xml.kishou.go.jp/seminar/pdf20140319/01.pdf)
- [気象庁のお知らせ（警報・注意報の電文の再編）](https://www.data.jma.go.jp/suishin/oshirase/pdf/20241226)

---

## 設計の調べ（2026-11-20）

### Summary
- **Feature**: `morning-night-talk`
- **Discovery Scope**: Extension（既存の基盤の上の新しい機能。外部の天気の情報との新しいつながりを含む）
- **Key Findings**:
  - 気象庁の旧い注意報・警報の JSON（`bosai/warning/data/warning/{office}.json`）は 2026-05-28 から更新が止まったまま 200 を返し続けている。今の画面は `bosai/warning/data/r8/{office}.json`（形もコードも新しい）を読む
  - Open-Meteo の `models=jma_seamless` では降水確率が null になる。既定（`best_match`、日本では JMA MSM を含む）なら降水確率が取れる
  - 日課の公開の口には、寝るタスク・朝の最初のタスク・予告を言い終えたか・起床時刻がない。日課の中（`Scheduler`、記録の `notice_said`・`ended_at`）には全部ある

### Research Log

#### 天気の予報の出どころ
- **Sources**: https://open-meteo.com/en/docs 、https://open-meteo.com/en/docs/jma-api 、https://open-meteo.com/en/terms 、https://open-meteo.com/en/licence （2026-11-20 に実際に問い合わせて確かめた）
- **Findings**:
  - `daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Asia/Tokyo&forecast_days=2` で今日とあしたが取れる。値は `daily.time` と同じ並びの配列
  - 非商用は無料で鍵なし。1 日 1 万回まで。データは CC BY 4.0、出典の表記が要る
  - 誤りは HTTP 400 と `{"error": true, "reason": ...}`
- **Implications**: 予報は Open-Meteo の既定のモデルで取る。降水確率の null を許す

#### 気象庁の予報 JSON（代わりの候補）
- **Findings**: `bosai/forecast/data/forecast/{office}.json` は生きている。17 時の報であしたの最高・最低・降水確率が取れる。朝の報の気温の並び（今日の最低が欠ける場合がある）は確かめられなかった
- **Implications**: 朝の今日の値が不確かなので、予報には使わない。将来の代わりの候補として残す

#### 気象庁の注意報・警報（r8）
- **Sources**: https://www.jma.go.jp/bosai/warning/ （画面の JS のコード表）、https://www.jma.go.jp/bosai/common/const/area.json 、https://www.jma.go.jp/jma/press/2604/14b/20260414_taikeiseiri.html 、https://www.jma.go.jp/jma/kishou/info/coment.html
- **Findings**:
  - 応答は電文の種類（`dataTypeCode`）ごとの報の配列。`warning.class20Items[].areaCode` と `kinds[].code`・`status`
  - 2026-05-29 から新しい体系（大雨・土砂災害・高潮はレベル 2〜5、洪水の警報はなくなり氾濫の情報へ）
  - 公式の API ではなく、URL は予告なく変わる（利用規約）。公共データ利用規約で、出典の記載が要る
- **Implications**: 注意報は別のポートにし、取れなくても天気の話はする。報の時刻が 24 時間より古ければ使わない

#### 既存のコードの口
- 日課: `RoutineStatus` は時刻と真偽値だけ。`_publish` は 1 秒ごとと、タスク・予告の始まり。記録は日付ごとで、`notice_said`・`ended_at` を持つ
- おしゃべり: `api.py` がない。ほかの機能は中のモジュールを読めない（読み込みの向きのテスト）。`ChatModelPort` は例外を出さず結果の型を返す
- 基盤: `FeatureConfig.enabled` は機能ごとに既定を変えられる（ラジコンが `False`）。優先度が同じか上の機能が借りていれば `ResourceBusy`
- 試し: 日課の試しは別のプロセスで、単一起動のロックを取れなければ 75 で終わる

### Design Decisions

#### Decision: 新しい機能にし、日課とおしゃべりに公開の口を足す（ギャップの分析の案 B）
- **Alternatives**: 日課の中に足す（案 A）、会話の部品を写す（案 C）
- **Selected**: `features/talk/` を作る。日課の `RoutineStatus` に `wake_at`・`bedtime`・`first_task`（`TaskMoment`）を足し、おしゃべりに `api.py` を足す
- **Rationale**: 日課をネットワークから切り離す。子ども向けの決まりと鍵の読み方を 1 か所に保つ
- **Trade-offs**: 既存の 2 つのスペックに小さな変更が要る。`SYSTEM_PROMPT` を 1 文字も変えないことをテストで縛る

#### Decision: 優先度は `BACKGROUND + 5`
- **Context**: 8.4（ほかの機能が声を使っていれば待つ）と 8.1（日課には譲る）
- **Alternatives**: `INTERACTIVE` の帯（おしゃべりやラジコンを止めてしまう）、`SCHEDULED` の帯（日課と競う）
- **Selected**: 見守り（10）より上、ラジコン（30）より下。借りられなければ次の秒に判定し直す
- **Trade-offs**: 子どもがボタンを押すと、音声の入口に譲ってお話は終わる（その回は話した扱い）。子どもの関わりを優先するので受け入れる

#### Decision: 前もって用意し、話す時は 5 秒だけ待つ
- **Context**: 7.4（5 秒以内に話し始める）と、外の問い合わせが 20 秒かかりうること
- **Selected**: 予告の時刻の 5 分前（後の道ではタスクの時刻の 5 分前）から、3 つを並べて取る。話す時は 5 秒待って、そろった分を話す

#### Decision: HTTP は標準ライブラリ
- **Context**: GET が 2 つだけ。依存を増やしたくない
- **Selected**: `urllib.request.urlopen(timeout=...)`。`adapters/` の中だけで使い、テストでは差し替える
- **Rejected**: `httpx`・`requests`（新しい依存と extras が要る）

### Synthesis
- **Generalization**: 天気・注意報・豆知識は「時間切れ付きで取り、取れなければ省く部品」で同じ形にした（`Preparer` が 3 つを同じに扱う）。将来の「声で天気を聞く」も、同じ `WeatherPort` と `weather_talk` を使える
- **Build vs Adopt**: 天気は Open-Meteo（鍵なし、日本で使える）と気象庁を使う。会話の AI はおしゃべりのアダプタを使い、作らない
- **Simplification**: 話す時の判定は 1 つの純粋な関数（`plan.decide`）にし、状態機械のクラスを作らない。状態は記録（1 日 1 回）と日課の状況から毎秒求め直す

### Risks & Mitigations
- 気象庁の URL や形が予告なく変わる — 注意報は省ける部品にし、報の時刻で古さを見る。self-check で知らせる。見本の応答でテストする
- Open-Meteo の利用条件の変更 — 手順書に出典と条件を書き、Revalidation Triggers に入れる
- 予報の天気のコードと気象庁の注意報が食い違う（はれなのに大雨注意報など） — 注意報は「今出ているもの」として、別の文で話す
- 子どもがボタンを押すとお話が途中で終わる — 受け入れる（上の優先度の決定）

### 使ったスキルと案内
- `kiro-spec-design` の rules（design-principles、design-discovery-light、design-synthesis、design-review-gate）
- Claude の呼び方は、おしゃべりの設計で claude-api のスキルで確かめた決まり（`claude-opus-5-5`、effort low、server-side fallback）をそのまま使う。新しい呼び方は足さない

## 設計の見直し（2026-11-20、/kiro-validate-design）
- 判定: GO
- 直したこと:
  - `talk.weather` を `hardware=True` の self-check にした。CI（`--no-hardware`）と配置の後の確かめ（サービスがロックを持つ）で外へ問い合わせないため
  - 日課が無効のときに、`talk.setup` で「日課がないので話さない」と知らせる。日課の `config` は読まず、基盤の `FeatureConfig.enabled` だけを見る
- 受け入れた危険: 子どもがボタンを押すと、音声の入口（優先度が上）に譲ってお話が途中で終わる。その回は話した扱い
