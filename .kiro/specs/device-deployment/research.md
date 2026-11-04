# Research & Design Decisions

## Summary
- **Feature**: `device-deployment`
- **Discovery Scope**: Extension（`rover-core` の上に、配置の道具を足す。`rover-core` にも小さな追加がある）
- **Key Findings**:
  - 実機のサンプルは、新しい基盤のユニットが前提にしていた場所（`~/works/OsekkaiRover`）とユニットの名前（`osekkai-rover.service`）を使っている。新しい基盤は、別の場所と別の名前に置く必要がある
  - 「走行中か・作業中か」を外から知る口が `rover-core` にない。サービスが状態のファイルを書く形で足す
  - サンプルは単一の実行のロックを使わず、状態も出さない。サンプルが動いている間は、安全かを確かめられない

## Research Log

### 実機の今の構成（2026-11-04、読み取りだけで確認）
- **Context**: 新しい版を、サンプルを壊さずに置く場所を決めるため
- **Sources Consulted**: 実機での `ls`、`systemctl --user`、`/etc/os-release`、`apt-cache policy`、`dpkg -l`、`/boot/firmware/config.txt`（個人のデータの中身は出していない）
- **Findings**:
  - OS は Debian 13（trixie）、aarch64、Python 3.13.5、git 2.47.3。空き容量は 17 GB
  - サンプルのユニット `~/.config/systemd/user/osekkai-rover.service`:
    - `WorkingDirectory=%h/works/OsekkaiRover`、`ExecStart=%h/.venvs/osekkai/bin/python -m osekkai.main`
    - `EnvironmentFile=-%h/.config/osekkai/env`、`After=`・`Wants=pipewire-pulse.service`
    - `default.target.wants` で有効。`ExecStopPost` はない（停止の後の安全停止がない）
  - サンプルの作業コピーには、実機にしかないブランチと `data/`（記録・顔の特徴・音声のキャッシュ・`picar-x.conf`）がある
  - `data/picar-x.conf` には、注釈の 1 行しかない（調整値は書かれておらず、既定値で動いている）
  - `~/.config/osekkai/` には `env`（0600）だけがある。新しい基盤の `config.yaml` はまだない
  - `~/.local/share/osekkai/` には `jtalk`（open_jtalk の手元の写し）、`models`（YuNet・SFace）、`voices` がある
  - apt: `open-jtalk` は入っていない（候補 1.11-5+b1 はある）。`python3-lgpio`・`python3-numpy`・`python3-opencv`・`python3-picamera2`・`python3-smbus2`・`python3-yaml`・`i2c-tools`・`pipewire-pulse`・`pulseaudio-utils` は入っている
  - `config.txt`: `dtparam=i2c_arm=on`、`dtoverlay=hifiberry-dac`（スピーカー）がある。`/etc/asound.conf` がある
  - グループ: `gpio`・`i2c`・`spi`・`audio` に入っている。`Linger=no`（tty1 の自動ログインでユーザーの systemd が動いている）
- **Implications**:
  - 新しい基盤は `~/osekkai-rover/`（版ごとのディレクトリ）に置き、ユニットは `osekkai.service` にする
  - サンプルと新しい基盤は、ユニットの `Conflicts=` で同時に動かないようにする
  - サンプルにはロックも状態のファイルもないので、サンプルが動いている間の入れ替えは中止し、先に止めてもらう（2.3）
  - 調整値の移し替えは、今の実機では「移すものがない」で終わる。それでも、値があるときの変換は用意する
  - open_jtalk は apt で入れる。サンプルは手元の写しを `env` の環境変数で使っているので、apt を入れてもサンプルは変わらない（実装のときに `env` の中の変数の名前だけを確かめる）

### 調整値の形式（SunFounder の picar-x.conf）
- **Context**: 9.3（首の調整値の移し替え）
- **Sources Consulted**: robot_hat 2.5.x の `fileDB`、picarx の `Picarx.__init__`（rover-core の調査で参照したもの）、`config/osekkai.example.yaml` の注釈
- **Findings**:
  - 形式は `キー = 値` の行。キーは `picarx_dir_servo`（かじ取り）、`picarx_cam_pan_servo`、`picarx_cam_tilt_servo`、`picarx_dir_motor`（`[1, 1]` の形）
  - 新しい基盤の `core.motion.calibration` は、pan と tilt の符号が SunFounder と逆（rover-core の設計）。かじ取りとモーターの向きは同じ
  - 新しい基盤の範囲は −20〜20 度
- **Implications**: 変換は「pan・tilt は符号を反転、steering はそのまま、motor_direction はそのまま」。範囲の外の値は移さず、知らせる

### 走行中・作業中を外から確かめる方法
- **Context**: 2.1〜2.3。`rover-core` の要件にない口
- **Findings**:
  - `Arbiter.holder(resource)` で、資源ごとの持ち主（機能の名前か、基盤の内部の `core.*`）が分かる
  - `MotionService` は最後に送った速さを覚えていない。走行中の判定には、速さを覚える小さな追加が要る
  - `Clock.monotonic()` は `time.monotonic()`（Linux では CLOCK_MONOTONIC）で、同じ起動の間は別のプロセスとも比べられる
- **Implications**: 基盤が 1 秒ごとに状態のファイル（`$XDG_RUNTIME_DIR/osekkai/status.json`）を書く。古ければ「確かめられない」とする

### 接続が切れても入れ替えを最後まで行う方法
- **Context**: 4.5
- **Findings**:
  - `systemd-run --user --unit=<名前> --collect` で、ユーザーの systemd の一時的なユニットとして動かすと、ssh のセッションが終わっても続く（ユーザーの systemd が動いている限り）
  - ユーザーの systemd は、リンガリングか自動ログインで保たれる。初期設定でリンガリングを有効にする
  - 同じ名前の一時的なユニットは 2 つ作れない。ファイルのロック（`flock`）と合わせて二重の入れ替えを防げる
- **Implications**: 開発 PC は、事前の確認とバックアップの後、実機の「適用」を一時的なユニットで始め、その記録を追うだけにする

### 依存関係と切り戻し
- **Context**: 1.8、4.2（前の版と、その版の依存関係に戻す）
- **Findings**:
  - 仮想環境は場所を移せない。1 つの作業コピーで版を切り替えると、戻すたびに依存関係を入れ直すことになり、ネットワークがないと戻せない
  - 版ごとのディレクトリ（git の worktree ＋ 版ごとの仮想環境）と、今の版を指すリンクの切り替えにすると、戻すのはリンクを戻すだけで済む
  - pip のキャッシュ（`~/.cache/pip`）があれば、2 回目以降の仮想環境づくりは速い。基盤の依存は PyYAML・pydantic・smbus2 だけで小さい（numpy・lgpio は apt）
- **Implications**: 版ごとのディレクトリを採る（Capistrano などで広く使われる形）。古い版は世代の数で消す

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| 版ごとのディレクトリ＋リンク | `releases/<sha>/` を作り、`current` を切り替える | 戻すのが速く確実。依存関係も版と一緒に戻る。準備の失敗で今の版に触れない | ディスクを版の数だけ使う | 採用 |
| 1 つの作業コピーで checkout | 今のサンプルの形 | 単純 | 切り戻しで依存関係を入れ直す。手の変更と混ざる | 不採用 |
| Ansible などの構成管理 | 初期設定と配置を宣言で書く | 冪等の仕組みがそろっている | 開発 PC に道具が要る。1 台には大きい。CI で試しにくい | 不採用 |
| コンテナ | イメージで配る | 再現性 | Pi のハード（I2C・GPIO・PipeWire）を渡す手間。1 台には過大 | 不採用 |

## Design Decisions

### Decision: 道具は標準ライブラリだけの Python にし、開発 PC から毎回実機に写す
- **Context**: 開発 PC（WSL）と実機の両方で動き、CI でテストしたい。入れ替えの最中に、入れ替える版のコードに頼りたくない
- **Alternatives Considered**:
  1. シェルのスクリプト — テストしにくい。分岐（切り戻し、サンプルの扱い）が多い
  2. 道具を `osekkai` のパッケージに入れる — 入れ替える版と道具の版が混ざる。壊れた版の道具では戻せない
  3. 標準ライブラリだけの別のパッケージ（`deploy/rover_deploy/`）を、開発 PC から実機の `~/osekkai-rover/tool/` に写して動かす
- **Selected Approach**: 3
- **Rationale**: 道具の版は開発 PC の作業コピーと同じになり、入れる版とは独立する。実機のシステムの `python3` だけで動く
- **Trade-offs**: `osekkai` のコード（パスの決まり、状態のファイルの形）を道具が写しで持つ。形の変更は両方を直す（Revalidation Triggers に書く）
- **Follow-up**: 形の写しがずれないよう、両方のテストで同じ見本のファイルを読む

### Decision: 安全の確認は、基盤が書く状態のファイルで行う
- **Context**: 2.1〜2.3、6.7
- **Alternatives Considered**:
  1. Unix ソケットで問い合わせる — 双方向で正確だが、基盤にサーバーのスレッドが要る
  2. ログを読んで推し量る — 不確か
  3. 1 秒ごとに状態のファイルを書く — 単純。古さで「確かめられない」が分かる
- **Selected Approach**: 3
- **Rationale**: 書くのは基盤の 1 か所、読むのは道具だけ。基盤は道具を知らない
- **Trade-offs**: 最大 1 秒の遅れ。確認から停止までの間に走り出す可能性は、適用の直前にもう一度確かめて小さくする

### Decision: LCD の知らせは、待機画面が読む「知らせのファイル」で出す
- **Context**: 7.1（入れ替えの後に版を数秒）、5.5（戻し忘れ）
- **Alternatives Considered**:
  1. 道具が LCD に直に書く — 動いているサービスとバスを取り合う
  2. 版を毎回の起動で出す — 試しの版かどうかを基盤は知らない
  3. 待機画面が `$XDG_RUNTIME_DIR/osekkai/notice.json` を読み、ある間はその行を出す
- **Selected Approach**: 3（汎用の「外からの知らせ」。中身は道具が決める）
- **Rationale**: 基盤は「知らせを出す」だけを持ち、版や試しの意味を知らない。7.1 と 5.5 を 1 つの口で満たす
- **Trade-offs**: 機能が DISPLAY を持っている間は出ない（待機画面に戻ったときに出る）

### Decision: 試しの版は有効（enable）にしない
- **Context**: 5.1〜5.5。普段の版はサンプル（daily-routine まで）
- **Selected Approach**: 普段の版のユニットだけを有効にする。試しの版は起動だけする
- **Rationale**: 戻し忘れたまま実機が再起動しても、普段の版で立ち上がる
- **Trade-offs**: 普段の版が新しい基盤になった後は、試しの版も同じユニット（`current` のリンク）なので、再起動でも試しの版のまま。そのため戻し忘れの知らせは 1 時間ごとのタイマーで行い、再起動の後も続ける

### Decision: self-check は基盤のコマンドにし、動いているサービスがあるときはその報告を使う
- **Context**: 6.1〜6.8
- **Selected Approach**: `python -m osekkai --self-check`。単一の実行のロックが取れれば、ハードを軽く確かめる（I2C の応答・GPIO の線・再生先の有無）。取れなければ、状態のファイルにあるサービスの報告（使えないデバイス）を使う。サンプルが動いているときは、道具が `--no-hardware` を付ける
- **Rationale**: 動かさず、鳴らさず、取り合わない（6.5、6.7）。機能は `FeatureSpec.checks` で項目を足す（6.2）
- **Trade-offs**: サービスが動いている間のハードの項目は、起動のときの報告で、今の応答ではない

## Synthesis Outcomes
- **Generalization**:
  - 「入れ替え」「普段の版に戻す」「切り戻し」は、どれも「ある版（サンプルか、版のディレクトリ）を動かして確かめる」に帰着する。版の指定を `Target`（サンプルか、版）にまとめ、1 つの適用の流れで扱う
  - 版の表示（7.1）と戻し忘れ（5.5）を、「外からの知らせ」1 つにまとめた
- **Build vs. Adopt**:
  - 採用: systemd（`Conflicts=`、`systemd-run`、ユーザーのタイマー）、git の worktree、`flock`、pip の仮想環境、`tar`
  - 自作: 道具の流れ（標準ライブラリ）。構成管理の道具は、1 台・CI でのテスト・開発 PC の負担から採らない
- **Simplification**:
  - 開発 PC の設定は接続先の別名などの数項目だけ（TOML）
  - バックアップは `tar.gz` 1 つ。差分や暗号化はしない（権限で守る）
  - 実機の情報を開発 PC に保存しない（毎回実機に問う）

## Risks & Mitigations
- サンプルが動いている間は入れ替えられない — 手順書と中止のメッセージで、`rover-deploy stop` を案内する
- 状態のファイルの形が道具と基盤でずれる — 同じ見本のファイルで両方をテストする。形には `schema` の番号を付ける
- 初期設定の `sudo` がパスワードを求める — 端末を割り当てた ssh（`-t`）で実行する
- `apt` の `open-jtalk` がサンプルの声に影響する — サンプルの `env` で使う変数の名前を確かめ、影響がないことを実装のときに確認する
- 状態のファイルを書く処理が基盤を止める — 書けないときは警告を 1 回出すだけにし、基盤の異常にしない

## References
- systemd.unit(5) `Conflicts=`、systemd-run(1) `--user --unit --collect`、systemd.timer(5) `OnBootSec=`・`OnUnitActiveSec=`
- git-worktree(1)、git-ls-remote(1)、git-bundle(1)
- `.kiro/specs/rover-core/design.md`（ユニット、安全停止、単一の実行、版のログ）
