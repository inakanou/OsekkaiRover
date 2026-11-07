# Requirements Document

## Project Description (Input)
**誰が困っているか**: OsekkaiRover の保護者（開発者を兼ねる）。作ったものを、実機の Raspberry Pi 5 に安全に入れたい。実機は 1 台で、子どもが毎日使っている。入れ替えで壊れたり、走行中に止まらなくなったりしてはいけない。

**今の状況**:
- 実機では、参照実装（サンプル）が動いている。`~/works/OsekkaiRover`（git の作業コピー）から、別の場所の仮想環境で、systemd のユーザーサービスとして動く
- 依存関係・声のファイル・顔のモデルの準備は、README の手作業。open_jtalk は手元に置いた版で、共有ライブラリの場所を環境変数で渡す必要がある
- `rover-core` で、新しい基盤のユニットのファイル（`deploy/systemd/osekkai-rover.service`）・停止の後の安全停止・単一の実行のロック・版のログができた。ただし、実機にはまだ入っていない。ユニットは作業コピーの場所を前提にしている
- 実機は家の LAN にあり、GitHub の CI からは届かない。リポジトリは公開なので、セルフホストランナーは使わない
- `git-workflow.md` で、実機に入れるのはタグの版だけ、作業ブランチを試したら最新のタグに戻す、と決めてある。試すたびに手で切り替えて元に戻している

**何を変えるか**: 実機への配置（device-deployment）を整える。
- 開発 PC から 1 つのコマンドで、指定したタグ（または作業ブランチ）を実機に入れられる
- 入れる前に、走行中でないこと・作業中の機能がないことを確かめ、記録と設定をバックアップする
- 入れた後に動作確認（self-check と起動後の確認）を行い、失敗したら前の版に自動で戻す
- 今どの版が入っているかが分かる（ログと LCD）
- 新しい SD カードに OS を入れた状態から、スクリプトで実機の環境を作り直せる

範囲・境界・制約の詳細は `brief.md` を参照する。

## Introduction
Device Deployment は、開発 PC から実機（Raspberry Pi 5、1 台）に OsekkaiRover の版を入れ、確かめ、だめなら戻すための仕組みです。主な役割は次のとおりです。
- 指定した版を、安全なときだけ実機に入れる
- 入れる前に、設定と記録を守る
- 入れた後に確かめ、失敗したら前の版に戻す
- 新しい SD カードから、実機の環境を作り直す

この文書の「版」は、リポジトリのタグか作業ブランチの、ある時点のコミットを指します。
「普段の版」は、子どもが毎日使うために実機に入れておく版を指します。`daily-routine` ができるまでは、今実機で動いている参照実装（以下「サンプル」）が普段の版です。新しい基盤（`rover-core`）の版は、それまでは試しに入れるだけにします。
「作業中」は、機能が声・走行・首などの資源を使って何かをしている間（話している、走っている、遊んでいるなど）を指します。

## Boundary Context
- **In scope**:
  - 開発 PC からの入れ替え（版の指定、事前の確認、バックアップ、切り替え、依存関係、再起動、入れた後の確認、自動の切り戻し）
  - 試しに入れた版から普段の版に戻すこと
  - self-check のコマンドと、機能が確認の項目を足せる口
  - 入っている版の表示
  - 実機の初期設定（新しい OS の状態から環境を作る）
  - サンプルからの移行の準備（サンプルを残す、バックアップ、首の調整値の移し替え）
  - 入れ替えと初期設定の手順書
- **Out of scope**:
  - CI からの自動の入れ替え、複数台への配置、OS のイメージの作成
  - 個々の機能の確認の中身（各機能が self-check の口に足す）
  - サービスの停止の時の安全な処理（`rover-core`）
  - サンプルの顔の特徴を新しい保存先に移すこと（`presence`）。サンプルの星の記録は移さない（`daily-routine` で 0 から始めると決めた。2026-11-07）
  - 子どもが使う時間帯かどうかの判断（人が判断する）
- **Adjacent expectations**:
  - `rover-core` は、版の情報、起動と停止の振る舞い、停止の後の安全停止、単一の実行を提供する。Device Deployment は、走行中か・作業中かを外から確かめられることも期待する
  - 各機能は、自分の確認の項目を self-check の口に足す
  - 実機への接続先（ホスト名など）は、開発 PC の利用者の設定にあり、リポジトリには書かない

## Requirements

### Requirement 1: 版を指定した入れ替え
**Objective:** As a 開発者, I want 開発 PC から 1 つのコマンドで、指定した版を実機に入れたい, so that 手作業の切り替えで間違えない

#### Acceptance Criteria
1. When 開発者がタグを指定して入れ替えを実行したとき, the Device Deployment shall そのタグの版を実機に入れ、サービスをその版で動かす
2. When 開発者が作業ブランチを指定して入れ替えを実行したとき, the Device Deployment shall そのブランチの、リポジトリに push 済みの最新のコミットを、試しの版として実機に入れる
3. If 指定したタグまたはブランチがリポジトリにないとき, the Device Deployment shall 実機を変えずに中止し、見つからないことを知らせる
4. If 指定したブランチに、push していない手元のコミットがあるとき, the Device Deployment shall 実機を変えずに中止し、push が要ることを知らせる
5. When 入れ替えを実行したとき, the Device Deployment shall 実機を変える前に、今入っている版と、これから入れる版を表示する
6. If 実機に接続できないとき, the Device Deployment shall 何も変えずに中止し、接続できないことを知らせる
7. While 入れ替えが進んでいる間, the Device Deployment shall 同じ実機への 2 つ目の入れ替えを始めずに断る
8. If 依存関係が前の版から変わったとき, the Device Deployment shall 新しい版の依存関係を入れてからサービスを起動する
9. If 実機の作業コピーに手で加えた変更があるとき, the Device Deployment shall その変更を消さずに中止し、変更があることを知らせる
10. When 入れ替えが終わったとき, the Device Deployment shall 結果（成功、切り戻した、失敗）と、実機で動いている版を表示し、終了コードで区別できるようにする

### Requirement 2: 入れる前の安全の確認
**Objective:** As a 保護者, I want ロボットが走っている間や、子どもと何かをしている間は入れ替えないでほしい, so that 子どもを驚かせたり、ロボットが止まらなくなったりしない

#### Acceptance Criteria
1. If 実機が走行中のとき, the Device Deployment shall 入れ替えを始めずに中止し、走行中であることを知らせる
2. If 機能が作業中のとき, the Device Deployment shall 入れ替えを始めずに中止し、どの機能が作業中かを知らせる
3. If 走行中か作業中かを確かめられないとき, the Device Deployment shall 入れ替えを始めずに中止する。ただし、サービスが止まっていると確かめられたときは、そのまま進める
4. If 実機の空き容量が、入れ替えとバックアップに足りないとき, the Device Deployment shall 入れ替えを始めずに中止し、足りないことを知らせる
5. The Device Deployment shall 時間帯によって入れ替えを断らない（子どもが使う時間帯かどうかは、開発者が判断する）

### Requirement 3: バックアップ
**Objective:** As a 保護者, I want 入れ替えの前に、設定と記録を保存しておきたい, so that 入れ替えで失敗しても、子どもの記録や調整をなくさない

#### Acceptance Criteria
1. When 入れ替えを実行したとき, the Device Deployment shall 版を切り替える前に、実機の設定・記録・首の調整値のバックアップを作る
2. If バックアップを作れないとき, the Device Deployment shall 入れ替えを始めずに中止する
3. The Device Deployment shall バックアップを、実機の中と開発 PC の両方に置く。開発 PC では、リポジトリの作業コピーの外に置く
4. The Device Deployment shall バックアップを、本人だけが読める権限で保存する（名前・顔の特徴・API キーなどを含むため）
5. The Device Deployment shall 実機の中のバックアップを、決めた世代の数だけ残し、古いものから消す
6. The Device Deployment shall 入れ替えで、リポジトリの外にある設定・記録・キャッシュを消さない
7. When 開発者がバックアップを指定して戻すとき, the Device Deployment shall サービスを止めてから設定と記録をその時点に戻し、サービスを起動する

### Requirement 4: 入れた後の確認と自動の切り戻し
**Objective:** As a 開発者, I want 入れた版が動かなければ、自動で前の版に戻ってほしい, so that 壊れた版のまま子どもが使う日を作らない

#### Acceptance Criteria
1. When 新しい版のサービスを起動したとき, the Device Deployment shall self-check を行い、サービスが 30 秒動き続けること、ログに新しい版が記録されたことを確かめる
2. If 入れた後の確認のどれかが失敗したとき, the Device Deployment shall 入れる前の版と、その版の依存関係に戻し、サービスを起動し直す
3. When 切り戻したとき, the Device Deployment shall 戻した版でも入れた後の確認を行い、失敗した確認の項目と理由を表示する
4. If 切り戻した版の確認も失敗したとき, the Device Deployment shall サービスを止め、モーターが止まっていることを確かめてから、人の手が要ることを知らせる
5. If 入れ替えの途中で開発 PC との接続が切れたとき, the Device Deployment shall 実機の側で入れ替えを最後まで進めるか、入れる前の版に戻す。中途半端な版で動かさない
6. The Device Deployment shall 入れ替えの経過と結果を実機に記録し、後から開発者が見られるようにする

### Requirement 5: 試しの版と普段の版
**Objective:** As a 開発者, I want 作業ブランチを試した後、すぐに普段の版に戻したい, so that 子どもが使う時間には、いつもの版で動いている

#### Acceptance Criteria
1. When 開発者が「普段の版に戻す」を実行したとき, the Device Deployment shall 普段の版を実機に入れ、入れた後の確認を行う
2. The Device Deployment shall どの版が普段の版かを、開発者が指定して記録できるようにする
3. Where 普段の版がサンプルのとき, the Device Deployment shall サンプルの作業コピー・仮想環境・サービスで、サンプルを元どおりに動かす
4. While 試しの版が入っている間, the Device Deployment shall 試しの版であることと、普段の版が何かを、版の表示で分かるようにする
5. When 試しの版が入ったまま、決めた時間が過ぎたとき, the Device Deployment shall 開発者に戻し忘れを知らせる

### Requirement 6: self-check
**Objective:** As a 開発者, I want 実機の準備ができているかを 1 つのコマンドで確かめたい, so that 入れ替えの後や、初期設定の後に、どこが悪いかがすぐ分かる

#### Acceptance Criteria
1. When self-check を実行したとき, the Device Deployment shall 基盤の確認の項目（設定の読み込みと検証、LCD、声の合成と再生先、HAT のマイコン、ボタンとサーボの準備、保存先の書き込み、時刻の同期）を確かめ、項目ごとに「よい・だめ・飛ばした」と理由を表示する
2. Where 機能が確認の項目を足したとき, the Device Deployment shall その項目も同じ形で確かめて表示する
3. If 確認の項目のどれかが「だめ」のとき, the Device Deployment shall 失敗を示す終了コードで終える
4. If 確認の項目が決めた時間で終わらないとき, the Device Deployment shall その項目を「だめ（時間切れ）」にして、次の項目へ進む
5. The Device Deployment shall self-check で、モーターを回さず、首を大きく動かさず、大きな音を出さない
6. The Device Deployment shall self-check の表示とログに、子どもの名前・話す文章・設定の値・API キー・pin を出さない
7. While サービスが動いている間, the Device Deployment shall self-check を、動いているサービスの資源を取り合わずに行う（ハードを直に触る項目は飛ばしたと表示するか、サービスに確かめてもらう）
8. The Device Deployment shall self-check を、実機の上で単独でも、入れ替えの一部としても実行できるようにする

### Requirement 7: 版の表示
**Objective:** As a 保護者, I want 今どの版が入っているかを、ロボットを見て分かるようにしたい, so that 試しの版のまま使っていないかを確かめられる

#### Acceptance Criteria
1. When 入れ替えで新しい版が起動したとき, the Device Deployment shall 実機の LCD に版を数秒表示してから、待機画面に戻す
2. When 開発者が版の確認を実行したとき, the Device Deployment shall 実機で動いている版、普段の版、最後の入れ替えの日時と結果を表示する
3. The Device Deployment shall 版の表示に、タグの名前か、タグがなければブランチの名前とコミットの短い識別子を使う

### Requirement 8: 実機の初期設定
**Objective:** As a 開発者, I want 新しい SD カードに OS を入れた状態から、スクリプトで実機の環境を作り直したい, so that SD カードが壊れても、手作業を思い出さずに戻せる

#### Acceptance Criteria
1. When 開発者が初期設定を実行したとき, the Device Deployment shall OS のパッケージ、I2C とスピーカーの有効化、GPIO を使う権限、仮想環境、声のファイルと顔のモデルの取得、サービスの登録、ユーザーのリンガリングを準備する
2. When 初期設定を 2 回目以降に実行したとき, the Device Deployment shall すでに済んでいる手順を変えずに飛ばし、足りない手順だけを行う
3. If 初期設定の手順が失敗したとき, the Device Deployment shall そこで止め、失敗した手順と、やり直し方を表示する
4. When 再起動が要る設定を変えたとき, the Device Deployment shall 最後に再起動が要ることを知らせる。勝手に再起動しない
5. The Device Deployment shall 初期設定で、すでにある個人の設定・記録・API キーを上書きしない
6. The Device Deployment shall 初期設定で、API キーや pin を自分で作ったり、リポジトリから持ち込んだりしない。要るものは、開発者が入れる場所と手順を表示する
7. If 実機の OS が対応していない版のとき, the Device Deployment shall 何も変えずに中止し、対応している版を知らせる
8. When 初期設定が終わったとき, the Device Deployment shall self-check を行い、結果を表示する

### Requirement 9: サンプルからの移行の準備
**Objective:** As a 保護者, I want 新しい版を試しても、今のサンプルとそのデータがなくならないようにしたい, so that 日課ができるまで、子どもは今までどおりロボットを使える

#### Acceptance Criteria
1. The Device Deployment shall サンプルの作業コピー（実機にしかないブランチを含む）・仮想環境・サービス・データを、消したり書き換えたりしない
2. When 初めて新しい版を入れるとき, the Device Deployment shall 版を切り替える前に、サンプルのコードとデータのバックアップを開発 PC に作る（リポジトリの作業コピーの外に、本人だけが読める権限で）
3. When 初めて新しい版を入れるとき, the Device Deployment shall サンプルの首の調整値を、新しい版の調整値に移す。新しい版の調整値がすでにあれば、上書きしない
4. While 新しい版が動いている間, the Device Deployment shall サンプルのサービスを止めておき、2 つが同時にハードを動かさないようにする
5. While サンプルが動いている間, the Device Deployment shall 新しい版のサービスを止めておく

### Requirement 10: 公開リポジトリと個人データ
**Objective:** As a 保護者, I want 入れ替えの道具が、個人データや実機の情報をリポジトリに入れないでほしい, so that 公開リポジトリから家や子どもの情報が漏れない

#### Acceptance Criteria
1. The Device Deployment shall 実機の接続先（IP アドレス・ホスト名・ユーザー名）を、リポジトリのファイルに書かず、開発 PC の利用者の設定から読む
2. The Device Deployment shall バックアップ・入れ替えの記録・self-check の結果を、リポジトリの作業コピーの中に置かない
3. The Device Deployment shall 入れ替えと self-check の表示とログに、API キー・pin・子どもの名前・記録の中身を出さない

### Requirement 11: 手順書
**Objective:** As a 開発者, I want 入れ替えと初期設定のやり方を、後から読んで分かるようにしたい, so that しばらく触らなくても、迷わずに使える

#### Acceptance Criteria
1. The Device Deployment shall 初期設定・入れ替え・試しの版から普段の版に戻すこと・バックアップから戻すこと・self-check の手順書を、リポジトリに置く
2. The Device Deployment shall 手順書に、失敗したときに人がすることと、モーターを確実に止める方法を書く
3. The Device Deployment shall 手順書に、実機の接続先の設定の仕方を、実際の値を書かずに説明する
