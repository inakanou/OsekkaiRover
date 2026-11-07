# Research & Design Decisions

## Summary
- **Feature**: `daily-routine`
- **Discovery Scope**: Extension（`rover-core` の機能を足す口に、最初の機能を足す）
- **Key Findings**:
  - `rover-core` の口（`FeatureSpec`・`FeatureContext`・`Lease` の口・`StateStore`・`QuietMode`・`IdleScreen.set_info`）だけで、日課の振る舞いはほぼ全部作れる。基盤を書き換えるのは `pyproject.toml` の登録と設定の見本だけ
  - ボタン待ち（`ButtonRouter.wait_press`）は本物の時間で待つ（`Condition.wait`）。偽の時計を進めても待ちは縮まない。タスクの進み方は、資源の口を細い型（`RoutineIO`）で包んで、テストでは偽の時計と台本の入出力で動かす
  - 試しのコマンドは、基盤に機能ごとのサブコマンドの仕組みがない。機能の側に入口（`python -m osekkai.features.routine`）を作り、基盤の `Runtime` を「試しの機能 1 つだけ」の登録で動かす

## Research Log

### サンプルの日課の振る舞い
- **Context**: 子どもから見た振る舞いをサンプルと同じにするため
- **Sources Consulted**: 開発 PC の控え（`~/.local/share/osekkai-deploy/backups/` のサンプルの tar の `routine.py`・`routine.example.yaml`・`main.py`・`body.py`）。個人の設定と記録は読んでいない
- **Findings**:
  - 設定のキー: `id`・`days`（`weekday`／`holiday`／省略で毎日）・`at`・`after`・`firm`・`notice`・`notice_min`（10）・`ready`・`call`・`praise`・`lcd`・`remind_min`（5）・`max_reminders`（2）・`sleep`。子どもの呼び名は `child.call_name`、追加の休日は `calendar.extra_holidays`
  - 時刻のタスクは [時刻, 時刻 + 60 分) の間だけ始まる。続きのタスクは、前のタスクが `done` か `skipped` なら始まり、`missed` なら始まらない（時間の窓はない）
  - 進み方: 準備（時間の上限なし。どの時刻のタスクでも割り込める）→ 声かけ（見回し・LED）→ 催促（5 分ごと、2 回まで）→「あとで できたら ボタンを おしてね」と 30 分の受付（割り込めるタスクだけが割り込める）→ できたら星 + ダンス + ほめる → 寝るタスクならおやすみ、そうでなければ 2 回うなずく
  - 結果は `done`／`skipped`／`missed`。記録は全体を書き直し（原子的でない）、増え続ける
  - 待機画面の 1 行目は `MM/DD HH:MM ★N`
  - 起動の判定: その日の寝るタスクが `done` か、その日の最初の `at` より前なら、暗いまま始める
  - 試しの `--try` は記録を書き、星を増やす
- **Implications**: 設定のキーはサンプルと同じ名前にして、保護者が手で写しやすくする（子どもの呼び名と追加の休日は `routine:` の節の下に移す）。結果の `missed` は「中断」（`interrupted`）と呼び直す

### rover-core の口
- **Context**: 日課が使える基盤の機能と、その制約を知るため
- **Sources Consulted**: `src/osekkai/core/{feature,arbiter,handles,buttons,idle,quiet,storage,config,clock,selfcheck,registry,runtime,supervisor,types,paths}.py`、`src/osekkai/__main__.py`、`.kiro/specs/rover-core/requirements.md`
- **Findings**:
  - 優先度の帯: `SCHEDULED = 80`（機能は 80〜89）。上位の機能が求めると、下位のリースには中断の知らせが 1 回届き、2 秒で返されなければ取り上げられる。発話は途中で止めない
  - リースは「全部か無しか」。日課より上の機能はないので、日課は待たずに借りられる（下位がいれば中断させて借りる）
  - `lease.voice.say()` は話し終わるまで戻らない。`lease.button.wait_press(timeout)` は本物の時間で待つ。`lease.neck.dance(drive)` は、走行の口を渡したときだけタイヤを動かし、さらに `core.motion.allow_drive_in_dance` の許可も要る（基盤の側で確かめる）
  - `ctx.set_idle_info(text)` は 1 行目の右に 4 セルまで出る（「★999」がちょうど入る）
  - `ctx.request_quiet()`／`end_quiet()` は待機画面とバックライトを切り替え、`QuietChanged` を出す。最後の状態は基盤が保存し、`was_quiet_before_restart` で読める。起動は必ず「静かでない」から始まる。静かな状態でも基盤は声を止めない（機能の側で守る）
  - `ctx.store` は機能ごとの保存先。`save` は原子的（一時ファイル + fsync + `os.replace`）。壊れたファイルは `<key>.corrupt-<日時>.json` に退避され、`load` は `None` を返す
  - 設定は `~/.config/osekkai/config.yaml` の、機能の名前の節。`FeatureConfig` を継いだ pydantic のモデルで検証する。誤りは「キーのパス: 理由」で出る（pydantic の `msg`。値は含まない。`ValueError` の文言に値を入れないこと）
  - self-check の項目は `FeatureSpec.checks` に `Check("routine.<項目>", ...)` で申告する。機能のインスタンスは作られない（`CheckContext.paths` から保存先を求める）
  - 機能の `run()` が例外で終わると、その機能は止まったままになる（作り直されない）
  - 機能ごとのサブコマンドの仕組みはない。`Runtime` は `start()` と `shutdown()` を公開している。`FeatureRegistry(discovered=[...])` で登録する機能を直接渡せる
- **Implications**: 日課の中のどの例外も 1 回の確かめの中で捕まえて記録し、`run()` を終わらせない。試しのコマンドは `Runtime` を組み立て直さず、登録だけを差し替える

### 祝日の判定（jpholiday）
- **Context**: 要件 2.2（日本の祝日・振替休日）
- **Sources Consulted**: PyPI の jpholiday（https://pypi.org/project/jpholiday/）
- **Findings**: 最新は 1.0.3（2025-12-26）。MIT。依存なし。Python 3.9 以上。1.0.0 は取り下げ。`jpholiday.is_holiday(date)` で判定する。祝日法の改正には新しい版で追随する
- **Implications**: 依存に `jpholiday>=1.0.2,<2` を足す（版の venv に pip で入る）。読み込めないときは土日と追加の休日だけで判定し、self-check で知らせる（2.6）。読み込みは日課の祝日の部品の中だけで、遅れて行う

### 配置への影響
- **Context**: 新しい依存とエントリーポイントが実機でどう入るか
- **Sources Consulted**: `deploy/rover_deploy/releases.py`（版ごとに `pip install -e <版>[pi]`）
- **Findings**: 版を準備するときに依存とエントリーポイントが入る。開発 PC の venv は、エントリーポイントを足した後に `pip install -e .` をやり直さないと機能が見つからない
- **Implications**: 実装のタスクに「開発 PC の venv を入れ直す」を手順として書く。実機は配置の道具がそのまま入れる

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| 純粋な判定 + 薄い実行（採用） | 判定（今日の種類・始めるもの・割り込み・おやすみ）を時刻と記録だけを受ける関数にし、実行は 1 本のスレッドで資源の口を呼ぶ | 時計を差し替えるだけで PC で全部の時刻を試せる。スレッドが 1 本で取り合いがない | 実行の部分（`TaskRunner`）は入出力の型で包む必要がある | steering の「判定と操作を分ける」に合う |
| サンプルの形（1 秒ごとの tick に全部を書く） | 判定と操作を 1 つのクラスに混ぜる | 速く写せる | 日付をまたぐ計算などを PC で試しにくい。サンプルの不具合を持ち込みやすい | 採らない |
| タスクごとにスレッド | 予告や割り込みを別のスレッドで走らせる | 予告を独立に出せる | 声とボタンの取り合いが日課の中で起きる。止め方が難しい | 採らない |

## Design Decisions

### Decision: タスクの進み方は 1 本のスレッドで、待ちを小刻みにして割り込みと予告を確かめる
- **Context**: 3.7（タスクのボタン待ちの間の予告）、5.1〜5.4（割り込み）、6.1（寝るタスクの割り込み）
- **Alternatives Considered**:
  1. 予告と割り込みを別のスレッドで見張る
  2. ボタン待ちを 1 秒ごとに区切り、区切りごとに判定の関数を呼ぶ
- **Selected Approach**: 2。`TaskRunner` はボタンを最大 1 秒ずつ待ち、そのたびに `Scheduler` に「割り込むタスクはあるか」「今言う予告はあるか」を聞く
- **Rationale**: 声・画面・ボタンを 1 本のスレッドだけが触るので、日課の中で取り合いが起きない。判定は純粋な関数のまま
- **Trade-offs**: 割り込みと予告の反応は最大 1 秒遅れる（要件の 1 分に十分収まる）
- **Follow-up**: 発話の途中は判定しない（言い終えてから）。ほめる間は割り込みを待たせる（5.3）

### Decision: 試しのコマンドは機能の側の入口から、基盤の `Runtime` を試しの機能 1 つだけで動かす
- **Context**: 10.1〜10.7。基盤には機能ごとのサブコマンドがない。基盤に機能の名前を書かない決まりがある
- **Alternatives Considered**:
  1. 基盤の `python -m osekkai` に `--try` を足す（基盤が日課を知ることになる）
  2. 基盤に「機能のコマンド」の汎用の仕組みを足す（今は使う機能が 1 つで、早すぎる一般化）
  3. 日課のパッケージに `__main__.py` を置き、ロック・デバイス・`Runtime` の組み立てを基盤の部品で行い、登録には試しの機能だけを渡す
- **Selected Approach**: 3。`python -m osekkai.features.routine --try <タスク> [--fake-hardware] [--fast] [--config PATH]`
- **Rationale**: 基盤を書き換えない。単一の実行のロック（10.6）・模擬のデバイス（10.7）・資源の口を、本番と同じ部品で使える
- **Trade-offs**: 日課の入口が `osekkai.__main__.build_devices` を読み込む（組み立ての口どうしの依存）。基盤の起動の声（8〜20 時）が試しでも流れる
- **Follow-up**: 2 つ目の機能が同じ形を求めたら、基盤に汎用の仕組みを作ることを検討する（そのときは `rover-core` の変更として扱う）

### Decision: 記録は 2 つの鍵に分けて保存し、星の数は控えからも戻せるようにする
- **Context**: 8.4〜8.7
- **Selected Approach**: `records`（日ごとの結果と予告と星の数）と `stars`（星の数の控え）の 2 つを、基盤の原子的な保存で書く。`records` が壊れていたら、`stars` から星の数を続ける
- **Rationale**: 基盤の保存は 1 つのファイルごとに原子的。2 つに分ければ、片方が壊れても星の数は残る
- **Trade-offs**: 2 回書く。順番は `records` → `stars`。途中で電源が切れても、どちらかは前か後の値
- **Follow-up**: 古い日の記録の整理は、日付が変わったときと起動のときに行う

### Decision: 続きのタスクにも 60 分の時間の窓を付ける
- **Context**: 3.3、7.3、7.7。サンプルの続きのタスクには時間の窓がなく、昼に再起動すると朝のタスクの続きが始まりうる
- **Selected Approach**: 続きのタスクは、前のタスクが終わってから 60 分の間だけ始める。そのために結果と一緒に終わった時刻を記録する
- **Rationale**: 時刻のタスクの 60 分（3.2）と同じ考え方で、再起動した後の思いがけない声かけを防ぐ
- **Trade-offs**: 要件に書いていない細部の決めごと。普段の流れ（すぐ次に進む）は変わらない

### Decision: 寝るタスクは時刻のタスクに限り、起床時刻より後にする
- **Context**: 1.8、6.1、6.7、7.1、7.2
- **Selected Approach**: 寝るタスクに `after` は使えない（誤り）。寝るタスクの時刻は、その種類の日の起床時刻より後でなければならない（誤り）。おやすみの時間は「その日の寝るタスクの後」から「次の日の起床時刻」まで
- **Rationale**: 夜中の 0 時をまたぐ寝る時刻があると、「どの日の起床時刻まで寝るか」が決まらない。家庭の寝る時刻は 0 時より前なので、制約にしてよい
- **Trade-offs**: 0 時より後に寝る設定はできない。寝るタスクが 1 つでもあれば、平日と休日の両方の起床時刻が決まらなければならない（前の夜が平日で次の日が休日でも、起床できるように）
- **Follow-up**: 起床時刻より前の時刻のタスク（おやすみの間に時刻が来たもの）は、起床の後に窓の中でも始めない（6.5）

### Decision: 優先度は `SCHEDULED + 5`（85）
- **Context**: brief の「日課の優先度は最も高く」。structure.md の帯の決まり
- **Selected Approach**: 85。今の機能の中では最も高い。同じ帯の上に余りを残す
- **Trade-offs**: 将来、日課より上に置きたい決まった時刻の機能（例: 薬）が出たら、86〜89 を使える

### Decision: ほかの機能に知らせる情報は、日課のパッケージの `api.py` の読み取り専用の口で出す
- **Context**: 9.4、9.5。`daruma-game` は次のタスクの時刻、`presence` はタスクの最中かどうかを知りたい
- **Alternatives Considered**:
  1. 基盤に機能どうしの掲示板を足す（基盤の変更）
  2. 日課の `api.py` に、プロセスの中で 1 つの読み取り専用の状況（`RoutineStatus`）を置く
- **Selected Approach**: 2。日課が確かめるたびに状況を新しくし、ほかの機能は `current_status()` で読む
- **Rationale**: structure.md の「機能どうしは相手の `api.py` だけを使う」に合う。基盤を変えない
- **Trade-offs**: 日課が動いていなければ `None`。読む側は `None` を「日課の予定なし」として扱う

## Risks & Mitigations
- 基盤の起動の声は静かな状態を見ないので、寝る時刻が起動の声の時間帯（既定は 20 時まで）の中だと、夜の再起動で声が出る — self-check の `routine.quiet_hours` と手順書で、保護者に時間帯を直してもらう（設計の見直しで追加）
- 長いタスク（あとでの 30 分）の間、日課が資源を持ち続け、ほかの機能（会話・あそび）が動けない — サンプルと同じ振る舞い（日課を優先する原則）。ほかの機能のスペックで「日課の最中は始めない」を前提にする
- 実機で時刻の同期が遅れると、朝のタスクが遅れる — 同期するまで始めない（3.9）。窓の 60 分の中で同期すれば始まる
- jpholiday が新しい祝日に追随していない — self-check で版を出さず「判定できる」だけを確かめる。毎年、版を確かめる（tech.md の決まり）
- 開発 PC の venv にエントリーポイントがないと、機能が見つからずテストの一部が通らない — タスクに「`pip install -e .` をやり直す」を書く

## References
- [jpholiday（PyPI）](https://pypi.org/project/jpholiday/) — 祝日の判定。MIT、依存なし
- `.kiro/specs/rover-core/design.md` — 機能の契約・資源の調停・静かな状態・保存の設計
- `.kiro/specs/daily-routine/brief.md` — サンプルの振る舞いと見つかっている問題
