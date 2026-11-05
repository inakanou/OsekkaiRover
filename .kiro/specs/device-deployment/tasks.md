# Implementation Plan

- [ ] 1. 土台: 道具のパッケージ・共通の型・状態の保存・共通の見本
- [x] 1.1 配置の道具のパッケージの骨組みと、テストの仕組みを整える
  - 標準ライブラリだけで動く道具のパッケージと、開発 PC の入口の実行ファイルを作る（入口は引数の解析だけで、サブコマンドは後のタスクで足す）
  - 型の確認とテストの対象に道具のディレクトリを足す（lint は既定で対象）。道具が `osekkai` のパッケージを読み込まないことをテストで縛る
  - 外のコマンドを呼ぶ口（Runner）と、テストで使う偽の Runner（呼ばれたコマンドを記録し、決めた結果を返す）を作る
  - 後の部品が使う systemctl・journalctl・git の薄い包み（動いているか、MainPID・再起動の回数・状態の読み取り、起動・停止・有効化・無効化・読み直し、時刻以降の journal、git describe・手の変更の一覧）もここで作る。後の部品はこの包みを使い、包みのファイルを直さない
  - 両側で使う型を作る: 終了コード（0・10・11・12・13）、版の対象（サンプル・版、タグかブランチ、sha）、入れ替えの結果、動いているサービスの種類（なし・新しい基盤・サンプル・両方）、版の表示の文字列（タグ / ブランチ@短い識別子 / sample）、JSON の読み書き
  - 完了の状態: `ruff`・`mypy`・`pytest` が道具のディレクトリを含めて通り、版の表示の文字列と、道具が `osekkai` を読み込まないことのテストが通る
  - _Requirements: 1.10, 7.3_

- [x] 1.2 実機の置き場所と、道具の状態・記録・ロックを作る
  - 実機の置き場所（新しい基盤の置き場所、版のディレクトリ、current、道具、状態のディレクトリ、XDG の実行時のディレクトリ、サンプルの場所）を 1 か所で決める。XDG の実行時のディレクトリがないときの決まりは基盤と同じにする
  - 道具の状態（普段の版・今と前の版・試しを始めた時刻・知らせた時刻・サンプルの保存・調整値の移し替え・途中の入れ替え・最後の入れ替え）を 0600 で、一時ファイルからの置き換えで保存する。普段の版の既定は、サンプルの作業コピーがあればサンプル
  - 入れ替えのロック（取れなければすぐ断る）と、入れ替えの記録（1 行 1 件、最後の行が結果、最新 50 件を残す）を作る。記録には手順の名前・固定の理由・例外の型の名前だけを書く
  - 完了の状態: 状態の読み書き・権限 0600/0700・ロックの二重取得の拒否・記録の世代の削除のテストが通る
  - _Requirements: 1.7, 4.6, 5.2, 10.2, 10.3_

- [x] 1.3 基盤と道具が共有するファイルの形の見本を作る
  - 状態のファイル（走行中・作業中・使えないデバイス・心拍を含む）、知らせのファイル、self-check の JSON の見本を、設計の Data Models の形で置く
  - 版のログの文言の頭（`版: `）も見本に置き、基盤と道具の両方がそれと同じ文言を使うことを後のタスクのテストで確かめる
  - 見本は基盤のテストと道具のテストの両方から読む（形のずれを防ぐ）。この段階では見本が JSON として読め、必要なキーを持つことだけを確かめる
  - 完了の状態: 見本のファイルがあり、形を確かめるテストが通る
  - _Requirements: 2.1, 2.2, 6.8, 7.1_

- [ ] 2. 基盤（rover-core）への追加
- [x] 2.1 (P) 走行中の判定と、状態のファイルを書く仕組みを作る
  - 走行の操作に、最後に送った速さ（範囲に収めた後）が 0 でないかを返す口を足す。緊急停止が成功したら 0 に戻す
  - 状態の型と、JSON との行き来（書く・読む。形が違えば読めない）を作る。self-check（2.3・2.8）もこの型を使う
  - 状態のファイル（版・pid・心拍・走行中・作業中の資源と持ち主・使えないデバイス・開始した機能）を 1 秒ごとに 0600 で書くスレッドを作る。基盤の内部の持ち主は作業中に含めない
  - 書けないときは最初の 1 回だけ警告して続ける。止めるとファイルを消す
  - 完了の状態: 偽の時計と一時ディレクトリで、書いた中身が共通の見本の形と一致し、止めると消えるテストが通る
  - _Boundary: StatusReporter, MotionService_
  - _Requirements: 2.1, 2.2, 2.3, 6.7_

- [x] 2.2 (P) 外からの知らせを待機画面に出す仕組みを作る
  - 知らせのファイルを読み、期限内なら出す行を返す（ない・期限切れ・形が違う → なし。形が違うときは 1 回だけ警告し、中身はログに出さない）
  - 待機画面は、描くたびに知らせを読み、ある行を待機画面の行の代わりに出す。文字の変換と切り詰めは既存の処理を使う。機能が画面を持っている間は出さない
  - 完了の状態: 期限・形の誤り・片方の行だけの知らせ・待機画面に知らせの行が出るテストが通る
  - _Boundary: NoticeBoard, IdleScreen_
  - _Requirements: 5.5, 7.1_

- [x] 2.3 self-check の契約と実行の仕組みを作る
  - 確認の項目・結果（よい・だめ・飛ばした と固定の理由）・項目に渡す文脈（パス・検証した設定・サービスの報告・ハードを触ってよいか）を定める
  - 項目を別のスレッドで動かし、時間切れなら「だめ（時間切れ）」にして次へ進む。例外は「だめ」にして理由は型の名前だけにする
  - ハードの項目は、触ってよくなければサービスの報告で判定し（使えないデバイスに名前があればだめ）、報告もなければ「飛ばした」にする
  - 機能の契約に確認の項目の並びを足す（既定は空）。表示（1 項目 1 行）と JSON の形、だめがあれば終了コード 1 を作る
  - 完了の状態: 時間切れ・例外・ハードの項目の飛ばし方とサービスの報告・終了コード・JSON が共通の見本の形と一致するテストが通る
  - _Boundary: SelfCheck, FeatureSpec_
  - _Depends: 2.1_
  - _Requirements: 6.2, 6.3, 6.4, 6.6, 6.7_

- [x] 2.4 (P) 安全停止に、失敗を終了コードで返す使い方を足す
  - 厳しい使い方のときは、失敗した段階があれば終了コード 1 を返す。今までの使い方（ExecStopPost）は 0 のまま
  - 完了の状態: 段階の失敗あり・なしで、厳しい使い方と今までの使い方の終了コードを確かめるテストが通る
  - _Boundary: safe_stop_
  - _Requirements: 4.4_

- [x] 2.5 (P) 新しい基盤のユニットと、戻し忘れのタイマーを作る
  - 旧名のユニットを新しい名前に改め、置き場所を版のリンクの下に変える。サンプルのユニットとの排他、再生のサービスの後に起動、停止と再起動の約束は今までどおり
  - 戻し忘れの確認のサービスとタイマー（起動の 10 分後と、その後 1 時間ごと）を作る
  - 完了の状態: ユニットのテストが新しい名前・置き場所・排他・タイマーの間隔を確かめて通り、旧名のファイルがない
  - _Boundary: systemd units_
  - _Requirements: 5.5, 9.4, 9.5_

- [x] 2.6 状態のファイルと知らせを起動と停止の順番に組み込む
  - 機能の開始の後に状態のファイルを書き始め、停止の最初に止める（止まっている途中を作業中でないと見せない）。待機画面に知らせの仕組みを渡す
  - 版のログの文言の頭を定数にし、共通の見本の文言と一致することをテストで確かめる
  - 完了の状態: 偽のデバイスで Runtime を動かし、走行中と作業中の機能が状態のファイルに出て、停止でファイルが消える結合テストが通る。既存のテストがすべて通る
  - _Depends: 2.1, 2.2_
  - _Requirements: 2.1, 2.2, 6.7, 7.1_

- [x] 2.7 基盤の確認の項目と、実機の軽い確かめを作る
  - 動かさない・鳴らさない確かめの口（I2C の応答、GPIO の線を入力として取れるか、再生先があるか、決まった短い文の合成、時刻の同期）を、実機用と偽物の両方で作る
  - 基盤の確認の項目（設定・保存先・時刻・声の合成・再生先・LCD・HAT・ボタン・サーボ）を組み立てる。理由は固定の文言で、値・名前・文章を出さない
  - 完了の状態: 偽の確かめで、各項目のよい・だめと、ハードの項目の印、理由に設定の値が出ないことのテストが通る。実機用の確かめはモーターとサーボに書き込まない
  - _Depends: 2.3_
  - _Requirements: 6.1, 6.5, 6.6_

- [x] 2.8 起動の口に self-check をつなぐ（統合）
  - `--self-check`・`--json`・`--no-hardware` を足す。単一の実行のロックを取ってみて、取れればハードを触ってよい（終わったら放す）。取れなければ状態のファイルを読んでサービスの報告にする
  - 基盤の項目の後に、登録された機能の項目を並べる
  - 完了の状態: `python -m osekkai --self-check --fake-hardware --json` が共通の見本の形で出て、だめがあれば終了コード 1 になる。ロックが取られているときにハードの項目がサービスの報告で判定されるテストが通る
  - _Depends: 2.1, 2.3, 2.7_
  - _Requirements: 6.1, 6.2, 6.3, 6.7, 6.8_

- [ ] 3. 実機の道具の部品
- [x] 3.1 (P) 動いているサービスの判定と、入れ替えてよいかの判断を作る
  - どのサービスが動いているか（なし・新しい基盤・サンプル・両方）と MainPID を、systemctl から求める
  - 状態のファイルを読み、ない・形が違う・pid の不一致・心拍が 5 秒より古い → 確かめられない、走行中、作業中（機能の名前）、サンプルが動いている、を判断する。時刻は見ない
  - 完了の状態: 共通の見本を使い、止まっている・古い心拍・pid の不一致・走行中・作業中の名前・サンプル・時刻によらない、のテストが通る
  - _Boundary: ServiceStatus_
  - _Requirements: 2.1, 2.2, 2.3, 2.5_

- [x] 3.2 (P) 版のディレクトリの準備・切り替え・片付けを作る
  - bare の写しの取得、sha の確認、worktree、版ごとの仮想環境と依存関係の導入。同じ sha でできていれば使い回し、途中で失敗したら作りかけだけを消す
  - current のリンクを一時名から置き換えて切り替え、前の先を返す。版のユニットを写して読み直す。手の変更の件数を数える（ファイルの名前は出さない）
  - 古い版の削除（今・前・普段の版と、新しい順に 3 つを残す）。サンプルの場所と XDG のディレクトリには何も書かない
  - 完了の状態: 偽の Runner と一時ディレクトリで、準備・使い回し・失敗のときの片付け・切り替え・削除で残すもの・サンプルの場所に触れないことのテストが通る
  - _Boundary: Releases_
  - _Requirements: 1.1, 1.2, 1.8, 1.9, 3.6, 4.2, 9.1_

- [x] 3.3 (P) バックアップの作成・世代の削除・一覧・復元を作る
  - 設定・記録・データ（取り直せる素材を除く）と、サンプルのデータ（声のキャッシュを除く）を tar.gz に、0600（ディレクトリは 0700）で作る。作れなければ専用の例外
  - 初回用のサンプルのバックアップ（作業コピー全体、ユニットのファイル、仮想環境の pip freeze）。サンプルがなければ作らない
  - 5 世代を残して古いものを消す。一覧。中身を元の場所に戻す（サンプルのデータは指定のときだけ）
  - 完了の状態: 一時ディレクトリで、中身の範囲・権限・世代の削除・復元とサンプルのデータの扱いのテストが通る
  - _Boundary: Backups_
  - _Requirements: 3.1, 3.2, 3.4, 3.5, 3.7, 9.2_

- [x] 3.4 (P) 首の調整値の移し替えを作る
  - サンプルの調整値のファイルから 4 つのキーを読み、首の左右と上下は符号を反転、向きの値はそのまま移す
  - キーがない → 移すものがない、範囲の外 → 何も書かない、新しい設定の調整値が既定値でない → 上書きしない。設定がなければ版の見本から作り、調整値の節の行だけを置き換えて注釈を残す。値はログに出さない
  - 完了の状態: 符号の反転・範囲の外・キーがない・既定値でない設定を書かない・注釈が残る、のテストが通る
  - _Boundary: Calibration_
  - _Requirements: 9.3_

- [x] 3.5 (P) LCD の知らせと、戻し忘れの確認を作る
  - 版の知らせ（1 行目に版の表示を 16 セルに切り詰め、2 行目に普段の版か、試しと普段の版の短い名前。20 秒の期限）を書く・消す
  - 戻し忘れ: 試しの版が動いていて、試しを始めてから 3 時間を過ぎていれば、期限なしの 2 行目の知らせを書き、journal に警告を 1 行出し、知らせた時刻を記録する
  - 完了の状態: 共通の見本の形と一致する知らせ、文言と切り詰め、戻し忘れの時間の境目のテストが通る
  - _Boundary: Notices_
  - _Depends: 1.3_
  - _Requirements: 5.4, 5.5, 7.1, 7.3_

- [x] 3.6 (P) 入れた後の確認を作る
  - 版: 起動を待ち（最大 30 秒）、版の self-check の JSON から失敗の項目を集め、30 秒の間 MainPID と再起動の回数が変わらないこと、journal に版のログ（その版の git describe）があることを確かめる
  - 版のログの文言の頭は、道具の側にも定数で持ち、共通の見本の文言と一致することをテストで確かめる（道具は `osekkai` を読み込まない）
  - サンプル: 30 秒動き続けることだけ
  - 完了の状態: 偽の Runner と偽の時計で、self-check の失敗の集め方・再起動の回数の変化・版のログがない・起動しない、のテストが通る
  - _Boundary: Verify_
  - _Depends: 1.3_
  - _Requirements: 4.1, 4.3_

- [x] 3.7 初期設定の手順を作る
  - 手順の並び（OS の確認、apt、起動の設定、グループ、リンガリング、置き場所、声のファイルと顔のモデル（sha256 を固定）、戻し忘れのタイマー、最初の版のディレクトリ、秘密の置き場所の案内、self-check）と、各手順の「済んでいるか」「行う」を作る
  - 済んでいる手順は飛ばし、失敗で止めてやり直し方を出し、再起動が要れば最後に知らせる（自分では再起動しない）。対応していない OS では何も変えない
  - すでにある個人の設定・記録・秘密を上書きせず、秘密を作らない。sha256 が違う同じ名前のファイルは上書きせず警告する
  - 完了の状態: 偽の Runner で、済んでいる手順を飛ばす・失敗で止まる・再起動の知らせ・対応していない OS・既存のファイルを上書きしない、のテストが通る
  - _Depends: 2.5, 2.8, 3.2_
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8_

- [ ] 4. 実機の道具の流れ
- [x] 4.1 適用の流れ（切り替え・確認・切り戻し・停止）を作る
  - 準備 → 停止の直前の安全の確認 → 切り替え（初回の調整値の移し替え、動いているサービスの停止、リンクとユニット、知らせ、起動、普段の版だけを有効に）→ 確認 → 失敗なら前の版で同じ手順と確認 → それも失敗なら両方のユニットを止めて厳しい安全停止
  - 始めに途中の入れ替えを状態に書き、終わったら消す。経過と結果を記録に書き、状態の今と前の版・最後の入れ替え・試しを始めた時刻を更新する
  - 対象がサンプルのときはサンプルのユニットを起動して、30 秒の確認だけを行う。起動の前に相手のユニットが止まっていることを確かめる
  - 完了の状態: 偽の Runner で、成功・確認の失敗で切り戻し・切り戻しも失敗で停止・準備の失敗で何も変えない・試しの版は有効にしない・普段の版は有効にしてサンプルを無効にする・サンプルに戻す、のテストが通る
  - _Depends: 2.4, 3.1, 3.2, 3.4, 3.5, 3.6_
  - _Requirements: 1.1, 1.2, 1.10, 3.6, 4.2, 4.3, 4.4, 4.6, 5.1, 5.3, 9.4, 9.5_

- [x] 4.2 実機の入れ替えのサブコマンドを作る
  - 事前の確認（動いているサービスと版、安全の判断、空き容量、手の変更の件数、ロック、途中で終わった入れ替え）を、実機を変えずに JSON で返す
  - バックアップ（ロックを取り、初回はサンプルのバックアップも作る）、サンプルを受け取れたことの記録、適用を systemd-run で切り離して始める（同じ名前のユニットがあれば断る）、切り離された側の適用、記録を結果の行まで流す追跡
  - 完了の状態: 偽の Runner で、事前の確認の各中止の理由、切り離しのコマンドと二重の断り、追跡が結果の行で終わる、のテストが通る
  - _Depends: 1.2, 3.3, 4.1_
  - _Requirements: 1.5, 1.7, 1.9, 2.4, 3.1, 3.2, 4.5, 9.2_

- [x] 4.3 実機のそのほかのサブコマンドを作る
  - 版の確認（動いている版・普段の版・最後の入れ替え・試しの経過）、普段の版の記録、self-check、バックアップの一覧と復元（安全の確認 → 今のバックアップ → 停止 → 戻す → 起動）、停止（両方のユニットを止めて厳しい安全停止）、戻し忘れ、初期設定、入れ替えの記録の表示（id がなければ最新の 1 件。一覧も返せる）
  - 動いている版は、記録ではなく実際に動いているユニットとリンクの先から求める
  - 完了の状態: 偽の Runner で、各サブコマンドが JSON を 1 つ返し、復元が止めてから戻して起動する順番、記録の表示（id あり・なし）のテストが通る
  - _Depends: 3.7, 4.2_
  - _Requirements: 3.7, 4.6, 5.2, 5.5, 6.8, 7.2, 8.1, 11.2_

- [ ] 5. 開発 PC の道具
- [x] 5.1 (P) 利用者の設定を読む
  - 環境変数の指定か既定の置き場所から、接続先の別名・バックアップの置き場所・リポジトリの URL を読む。URL がなければ origin から https の形を作る
  - 接続先がなければ設定の仕方を出して中止する。バックアップの置き場所が git の作業コピーの中なら拒む
  - 完了の状態: 接続先がないときの案内、作業コピーの中を拒む、既定値のテストが通る
  - _Boundary: DevConfig_
  - _Requirements: 3.3, 10.1, 10.2_

- [x] 5.2 (P) タグとブランチを解決する
  - リモートのタグとブランチを探し、注釈付きのタグは先のコミットを使う。両方にあればタグを選んでその旨を返す
  - 手元のブランチにリモートにないコミットがあれば push が要る、手元が遅れているだけならリモートの最新、手元だけのタグは見つからない（push が要ると添える）
  - 完了の状態: 偽の Runner で、注釈付きのタグ・両方にある名前・手元だけのタグ・push していないコミット・手元が遅れているブランチ・見つからない、のテストが通る
  - _Boundary: RefResolver_
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 5.3 (P) ssh の操作をまとめる
  - 道具を tar で送って実機に展開する、実機のサブコマンドを呼んで JSON を読む、JSON 行を流す、ファイルを受け取る（0700 のディレクトリに 0600 で）、送る
  - 端末を割り当てない呼び方と接続の時間切れを付け、初期設定だけ端末を割り当てる。つながらなければ専用の例外
  - 完了の状態: 偽の Runner で、ssh の引数・つながらないときの例外・受け取ったファイルの権限のテストが通る
  - _Boundary: Remote_
  - _Requirements: 1.6, 3.3, 3.4_

- [ ] 5.4 開発 PC の入れ替えと普段の版に戻す流れを作る
  - 設定 → 版の解決 → 道具を送る → 事前の確認で今の版とこれからの版を表示 → バックアップを作って受け取る（初回はサンプルのバックアップも受け取り、受け取れたら記録）→ 切り離して適用 → 追跡 → 結果を表示して終了コードで終える
  - どれかが実機を変える前に失敗したら 10、追跡の途中で切れたら 13 で、版の確認を案内する。サンプルが動いているときは、使っていないことを確かめてから止めるよう案内する
  - 表示に設定の値・記録の中身・秘密を出さず、バックアップは場所と大きさだけを出す
  - 完了の状態: 偽の Remote で、成功・中止の各理由・切り戻し・停止・接続の切断（13）の表示と終了コードのテストが通る
  - _Depends: 4.2, 5.1, 5.2, 5.3_
  - _Requirements: 1.5, 1.6, 1.10, 2.1, 2.2, 2.3, 2.4, 3.2, 3.3, 4.3, 4.5, 5.1, 9.2, 10.3_

- [ ] 5.5 開発 PC のそのほかのサブコマンドを作る
  - 版の確認・普段の版の記録・self-check・バックアップの一覧と復元（開発 PC のファイルからの復元を含む）・停止・記録の表示・初期設定
  - どのサブコマンドでも、戻し忘れが知らされていれば最初に警告を出す
  - 完了の状態: 偽の Remote で、各サブコマンドの表示と終了コード、戻し忘れの警告のテストが通る
  - _Depends: 4.3, 5.4_
  - _Requirements: 3.7, 5.2, 5.4, 5.5, 6.8, 7.2, 8.3, 8.4, 11.2_

- [ ] 6. 手順書と全体の確認
- [ ] 6.1 手順書を書き、README とステアリングを更新する
  - 初期設定・入れ替え・普段の版に戻す・バックアップから戻す・self-check・失敗したときに人がすること・モーターを確実に止める方法・接続先の設定の仕方（実際の値を書かない）を書く
  - README から手順書へ案内し、ステアリングに道具の構成と決まり（標準ライブラリだけ）を書き足す
  - 完了の状態: 手順書があり、禁止ファイルの検査（接続先・個人の値がない）が通る
  - _Requirements: 11.1, 11.2, 11.3, 10.1_

- [ ] 6.2 開発 PC から実機の道具までをつなぐ結合テストを書く
  - 偽の Remote が実機の道具を同じプロセスで呼ぶ形で、入れ替え・切り戻し・普段の版に戻す・接続の切断を通しで動かす
  - 基盤が書く状態のファイル・知らせ・self-check の JSON と、道具の読み取りを、同じ見本で確かめる
  - 完了の状態: 結合テストと全体のテスト・lint・型が CI の両方の Python の版で通る
  - _Depends: 2.6, 2.8, 4.3, 5.5_
  - _Requirements: 1.10, 4.5, 6.8, 10.2_

- [ ] 6.3 実機で入れ替えを確かめる
  - 子どもが使っていない時間に: 初期設定を 2 回実行して 2 回目は何も変えない、サンプルが動いている状態の入れ替えは中止、停止の後に作業ブランチを入れて LCD に版が出る、走行中の入れ替えは中止、壊した版で自動の切り戻し、途中で ssh を切っても最後まで進む、普段の版（サンプル）に戻る、self-check（サービスが動いている・止まっている）、再起動でサンプルで立ち上がる
  - 終わったらサンプルに戻す
  - 完了の状態: PR 本文に実機確認の結果が書かれている
  - _Depends: 6.2_
  - _Requirements: 1.2, 2.1, 2.3, 4.2, 4.5, 5.3, 6.1, 6.7, 7.1, 8.2_

## Implementation Notes
- 1.1: 外のコマンドは `rover_deploy.system` の Runner（`run(argv, *, input, timeout, check, cwd, env) -> RunResult`）で呼ぶ。systemctl・journalctl・git の包み（`is_active`・`show_unit`・`start_unit` など・`journal_since`・`git_describe`・`git_dirty_count`）も system.py にあり、後のタスクは system.py を直さない。例外は `CommandError` の仲間で、標準エラーを持たない
- 1.1: テストの偽物は `tests/deploy/fake_runner.py` の `FakeRunner`（`from fake_runner import FakeRunner`、fixture は `fake_runner`）。前方一致で最長のものが勝ち、台本にないコマンドは AssertionError。tests に __init__.py がないので、`tests/deploy/conftest.py` は mypy の対象から外している（conftest の名前が重なる）。conftest には fixture だけを置く
- 1.1: `Target` は作るときに形を確かめる（版は 40 桁の小文字の sha が要る）。`write_json_file` は 0600 で原子的に書くが、親のディレクトリは作らない
- 1.2: 置き場所はすべて `rover_deploy.layout.Layout(home, env, uid)` から取る（`release_dir(sha)`・`current_link`・`state_dir`・`backups_dir`・`core_config_dir`・`core_state_dir`・`runtime_dir`・`sample_dir`・`sample_venv`・`sample_exists()` など）。`runtime_dir` は rover-core の `Paths` と同じく、XDG_RUNTIME_DIR が空か相対なら `/tmp/osekkai-<uid>`（設計の `/run/user` の段は誤りだったので直した）
- 1.2: 状態は `StateStore(layout).load()/save()`、ロックは `DeployLock`、記録は `DeployLog(log_dir, deploy_id, clock=)` の `.event()`/`.result()`。読む側は `list_log_ids`・`latest_log_id`・`read_log_events`・`read_log_result`。`read_log_result` は結果の行が壊れていても None を返すので、follow では「まだ結果がない」と区別に気をつける。`state_from_dict` は deploy_id の形を確かめていない
- 1.3: 共有の見本は `tests/fixtures/*.json`（status_driving_busy・status_idle・notice_version・notice_reminder・selfcheck_failed・selfcheck_ok・version_log）。読み込みは `from shared_fixtures import load_fixture, version_log_prefix`（tests/ が import の道にある）。見本のテストはキーの集合をちょうどで確かめるので、基盤と道具の書き出しは見本と同じキーにする
- 1.3: tests の下は同じ名前のテストのファイルを置けない（tests/core と tests/deploy で名前を重ねない）
- 2.1: 状態の型は `osekkai.core.status.ServiceSnapshot`（`from_dict` は形に厳しく、違えば `SnapshotUnreadable`）。`read_snapshot(path)` は読めなければ None。2.6 は `build_snapshot(arbiter=, motion=, version=, pid=, clock=, unavailable=, features=)` と `StatusReporter(Paths.runtime_dir / STATUS_FILE_NAME, snapshot)` でつなぐ
- 2.1: `StatusReporter.stop()` は、Arbiter のロックを持ったまま呼ばない（報告のスレッドが `Arbiter.holder()` を待って止まる）
- 2.1: `MotionService.driving` は「たぶん走行中」の側に倒す（非常停止が書き込みと重なっても True のまま）
- 2.2: 2.6 は `IdleScreen(arbiter, clock, notices=NoticeBoard(Paths.runtime_dir / NOTICE_FILE_NAME, clock))` でつなぐ。`current()` はない・期限切れ・形の誤りで None（警告は続く間 1 回）。知らせは静かな状態の間も出さない
- 2.2: Clock には壁時計のエポック秒の口がないので、`clock.now().timestamp()` を使う
- 2.3: self-check は `osekkai.core.selfcheck`（`Check(name, run, timeout_s, hardware, device)`・`CheckResult`・`CheckContext(paths, report, service, hardware_allowed)`・`SelfCheckRunner`・`collect_checks(base, specs)`・`render_lines`・`results_to_dict`/`results_to_json`・`exit_code`）。`core` の `__init__` からは出していない
- 2.3: ハードの項目は `device=` に ServiceSnapshot.unavailable の名前（LCD は "display"、ほかは "hat"・"buttons"・"servos"）を入れる。入れないと、サービスの報告があっても「飛ばした」になる
- 2.4: `python -m osekkai.core.safe_stop --strict` は、段階の失敗に加えて、ハードに触らずに終えたとき（ロックをほかが持つ・EXIT_STATUS=75・アダプタなし）も終了コード 1。PC では必ず 1 になる。引数なしは今までどおり必ず 0
- 2.5: 新しい基盤のユニットは `deploy/systemd/osekkai.service`（旧名のファイルは消した）。戻し忘れは `osekkai-trial-reminder.service`（`/usr/bin/python3 -m rover_deploy.agent remind`、作業ディレクトリは `~/osekkai-rover/tool`）と `.timer`（起動の 10 分後と 1 時間ごと）。道具は `tool/rover_deploy/` に展開する前提
- 2.5: `Conflicts=` は新しい基盤の側だけに書き、systemd が両方向に効かせる。サンプルのユニットは直さない
- 2.6: 基盤は機能の開始の後に状態のファイルを書き始め、停止の最初に消す（停止の途中は「ファイルがない」= 道具からは確かめられない）。`features` と `unavailable` は起動のときの値で固定、`driving` と `busy` は毎回の値。版のログの頭は `osekkai.core.runtime.VERSION_LOG_PREFIX`
- 2.7: 基盤の確認の項目は `osekkai.checks.base_checks(probes)`。実機は `osekkai.adapters.pi.probes.PiProbes`、偽物は `osekkai.adapters.fake.probes.FakeProbes`。I2C の確かめはデータのない quick write、ボタンの線は入力で取ってすぐ放す。声の合成の項目の時間切れは 40 秒
- 2.7: サンプルは単一の実行のロックを使わないので、サンプルが動いている間の self-check はハードの項目も走る（害はないが、初期設定ではサンプルが動いていれば `--no-hardware` を付ける）
- 2.8: `python -m osekkai --self-check [--json] [--no-hardware] [--fake-hardware]` は、ロックを取れればハードの項目も行い、終わるまでロックを持つ（`--no-hardware` でも持つ。声の合成で最大 40 秒）。取れなければ status.json の報告で判定し、なければ「飛ばした」
- 2.8: 重要: self-check がロックを持っている間に osekkai.service が起動すると、サービスは 75 で終わって再起動しない。入れた後の確認（3.6）は、self-check の前に「status.json があり、pid が MainPID と同じで、心拍が新しい」まで待つ（START_TIMEOUT_S の中で）。初期設定でサービスを起動するのは self-check の後にする。手順書（6.1）にも、self-check の最中にサービスを起動しないよう書く
- 3.1: 安全の判断は `rover_deploy.service_status.check_safety(run, layout)` → `SafetyVerdict(ok, running, reason, busy)`。理由は ""・driving・busy・unknown・sample_running。`running=BOTH` で `reason=unknown` は「systemctl が答えなかった」の意味なので、動いているサービスとして表示しない
- 3.1: 入れた後の確認がサービスを待つときは `service_reporting(read_status(status_path(layout)), core_main_pid(run), now_monotonic=)` を使う。service_status.py には壁時計の語（time.time・datetime など）を書かない（テストが文字で探す）
- 3.2: 版は `rover_deploy.releases.Releases(layout, run)` の `prepare(target, repo_url)`・`current()`・`switch(dir)`・`install_unit(dir)`・`dirty_count(dir)`・`prune(keep)`。準備の失敗は `PrepareFailed(step)`（fetch・commit・worktree・venv・pip）、ほかは `ReleaseError`。取得は `fetch --prune --tags --force`（リモートで付け直したタグを受け入れる）。仕上げの印は `.venv/.osekkai-ready`
- 3.3: バックアップは `rover_deploy.backups`（`create_backup(layout, id, env=)`・`create_sample_backup(layout, id, run)`・`list_backups`・`prune`・`restore_backup(layout, path, include_sample=, env=)`・`recover_interrupted_restore(layout, env=)`・`estimate_backup_bytes(layout, include_sample=, env=)`）。ファイルは `backups_dir/backup-<id>.tar.gz` と `sample-<id>.tar.gz`。サンプルのバックアップは一覧・世代の削除・復元の対象外
- 3.3: Layout は XDG_DATA_HOME を持たないので、backups の関数には Layout を作ったのと同じ env を必ず渡す。サービスを起動する前（preflight や stop の中、ロックを持って）に `recover_interrupted_restore` を呼び、途中で止まった復元を片付ける
- 3.4: 調整値の移し替えは `rover_deploy.calibration.migrate(sample_conf, config_file, example)` → status（migrated・nothing_to_migrate・already_set・out_of_range・unreadable）。どの status でも入れ替えは続ける（失敗にしない）。結果は状態の `calibration_migrated` に記録して 2 回目以降は行わない（4.1）。見本は版の `src/osekkai/core/example_config.yaml`
- 3.5: 知らせは `rover_deploy.notices`（`version_notice(target, everyday, now)`・`write_notice(runtime_dir, notice)`・`clear_notice(runtime_dir)`・`remind(state, running, now) -> bool`・`send_reminder(runtime_dir, state, now) -> DeployState`）。send_reminder が返した状態は呼ぶ側が保存する。普段の版に戻したら `clear_notice` で戻し忘れの知らせを消す。試しの版かどうかは `current != everyday`（Target のすべての欄で比べる）
- 3.6: 入れた後の確認は `rover_deploy.verify.verify_release(release_dir, run, clock, layout, *, since_epoch, expected_describe=None)` と `verify_sample(run, clock)`。`since_epoch` はユニットを起動する直前の壁時計の時刻を呼ぶ側が渡す。失敗の名前は start・status・self_check・self_check:<項目>・alive・version_log
- 3.6: 実機（2026-11-05 に読み取りだけで確認）には利用者ごとの journal のファイルがなく、`journalctl --user` は何も読めない。`journal_since` は `--user` を付けずに `--user-unit=` で読むよう直した。利用者が journal を読めるグループ（systemd-journal か adm）に入っている要があるので、初期設定（3.7）のグループの手順で確かめる
- 3.6: 起動と報告の待ちは合わせて 30 秒（START_TIMEOUT_S）。実機確認（6.3）で起動の時間を測り、足りなければ報告の待ちに別の予算を持たせる
- 3.7: 初期設定は `rover_deploy.provision`（`provision_steps(ctx)`・`run_provision(steps, log) -> ProvisionReport`、`ProvisionContext(run, layout, releases, user, repo_url, target, data_dir, say, os_release, boot_config, now, fetch, assets)`）。`release` の手順には、開発 PC で解決した Target を渡す（--ref の既定は最新のタグ、なければ main）。`data_dir` は `backups.core_data_dir(layout, env)`。ロックは呼ぶ側が持つ
- 3.7: 手順の順番は設計から 1 つ変えた: reminder は release の後（タイマーのファイルを今の版から写すため）。グループには systemd-journal も足す（adm に入っていれば足さない）。self-check のだめの項目は手順の失敗にせず、`ProvisionReport.self_check_ok` に入れる
- 3.7: 声のファイルと顔のモデルは、取得元と sha256 を固定した `ASSETS`（実機のファイルと一致することを 2026-11-05 に確かめた）。open_jtalk は apt の open-jtalk を使うので、jtalk のディレクトリは取らない
- 4.1: 入れ替えは `rover_deploy.transaction.Transaction`（`run(target, deploy_id, *, everyday) -> DeployResult`・`halt() -> list[str]`、作るときに `repo_url` が要る）。有効にするのは確認が通った後（設計の Switching から移した）。有効にできなければ outcome は succeeded のまま、failed_checks に "enable" が入る（5.4 の表示で扱う）
- 4.1: 前の版がリンクの先と合わなければ前の版は None になり、切り戻さずに停止する。停止では current=None。予期しない例外は in_progress を残して上に投げる（確認の最中の例外だけは "verify" の失敗として切り戻す）
- 4.1: 設計の穴（設計の Migration Strategy に記録）: 普段の版が新しい基盤の版になった後は、試しの版を入れたまま再起動すると試しの版で立ち上がる。daily-routine で普段の版を切り替える前に直す
- 4.2: 実機のコマンドは `python3 -m rover_deploy.agent <sub>`（作業ディレクトリは `~/osekkai-rover/tool`）。標準出力に JSON を 1 つ（follow は JSON の行）、終了コードは 0（JSON の ok/reason を見る）・2（使い方の誤り。JSON がないこともある）・1（思わぬ失敗）。4.3 は `agent._handlers()` の表に足す。動いている版は `running_target(ctx, state, running)`
- 4.2: preflight の reason は lock_busy・state_broken・interrupted・安全の理由（driving・busy・unknown・sample_running）・disk_low・dirty・dirty_unknown。apply は apply_running・apply_unknown・lock_busy・start_failed。follow の終わりは結果の行か、error（no_log・broken_result・ended_without_result・timeout）。ended_without_result は開発 PC で 13 と同じに扱う
- 4.2: ssh の側と systemd-run の側で XDG_* の環境が違うと、ロックと状態の場所がずれる。手順書（6.1）に「XDG_* を変えない」と書く
- 4.3: 実機のそのほかのコマンド: status・set-everyday・self-check・backups・restore・stop・remind・provision・log。新しい reason: no_backup・bad_backup_file・backups_unreadable・restore_failed・restore_leftover・stop_failed・start_failed_after・halt_failed・no_release・self_check_error・self_check_unreadable・running_unknown・notice_failed・provision_failed
- 4.3: 開発 PC（5.x）への引き継ぎ: provision は ssh -t で動かすので、標準エラーと sudo の問いが同じ流れに混ざり、行の終わりが \r\n になる。最後の行の \r を取ってから JSON として読む。stop の結果が failed=["safe_stop"] だけなら「サービスは止めたが、モーターの停止は確かめられない（版がない）」と表示する。サンプルを止めてから restore した後は、サービスを起動し直すよう（または back を）案内する
- 5.1: 開発 PC の設定は `rover_deploy.devconfig.load_dev_config(env, repo_root, run, home=None) -> DevConfig(host, backup_dir, repo_url)`。誤りは `ConfigError`（決まった文言、`exit_code`=10）。devcli はこれを終了コード 10 に写す。repo_url は https だけ（origin の ssh の別名からは作れない）
- 5.2: 版の解決は `rover_deploy.refs.resolve_ref(name, repo_root, run) -> Resolution(target, also_branch)`（設計の `-> Target` から変えた。also_branch は「タグとブランチの両方にあり、タグを選んだ」）。例外は `RefError` の仲間（BadRefName・RefNotFound(local_only)・UnpushedCommits・RemoteUnreachable）で、どれも exit_code=10。`refs.RemoteUnreachable` は origin に届かないの意味で、ssh の接続の失敗とは別
- 5.3: ssh は `rover_deploy.remote.Remote(host, run, *, spawn, echo)`（`push_tool`・`agent(*args, tty=False) -> AgentReply(exit_code, data)`・`stream(*args)`・`fetch`・`send`）。例外は `RemoteError` の仲間で `exit_code` をそのまま終了コードにする（DeviceUnreachable 10、DeviceDisconnected 13、StreamProtocolError 13、AgentProtocolError・RemoteTimedOut・TransferFailed 10）。follow の `{"error": ...}` の行は普通の行として返るので、devcli で 13 に写す
- 5.3: tty（provision）のときは、実機の進み具合と sudo の問いを手元に表示し、`{` で始まる行は隠す。実機のログインシェルは POSIX の sh 系（既定の bash）を前提にする（手順書に書く）
