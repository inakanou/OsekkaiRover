# Research & Design Decisions

## Summary
- **Feature**: `presence`
- **Discovery Scope**: Extension（`rover-core` の機能を足す口に、首のカメラと顔の認識を使う機能を足す）
- **Key Findings**:
  - 実機の依存は、初期設定（provision）の apt ですでに入る: `python3-opencv`（Trixie で 4.10）・`python3-picamera2`・`python3-numpy`。版の venv は `--system-site-packages` なので、pip の依存は足さない
  - モデル（YuNet 2023mar・SFace 2021dec）も、初期設定が `~/.local/share/osekkai/models/` に sha256 を固定して置いている。presence はそこを読むだけ
  - 開発 PC と CI には OpenCV・numpy・picamera2 がない。判定（あいさつ・追従・認識の比べ方・投票）は純粋な Python にし、OpenCV と picamera2 はアダプタの中だけで遅れて読み込む。顔の特徴は数字の並び（128 個）なので、JSON で基盤の保存先に置ける

## Research Log

### サンプルの振る舞い
- **Sources Consulted**: 開発 PC の控え（サンプルの tar の `watcher.py`・`routine.py`・`main.py`・`lcd.py`・`routine.example.yaml`）。個人の設定・記録・顔のデータは読んでいない
- **Findings**:
  - カメラ: picamera2 の 640×480 RGB888。上下の反転は設定でソフトウェアの 180 度回転
  - 検出: YuNet（入力 320×240、score 0.6）。最も大きい顔。2 回続けて見つかったら採る。2.5 秒見失ったら消し、5 秒を過ぎたら首を 0.8 倍ずつ正面へ
  - 追従: 位置（−1〜1）が 0.15/0.2 を超えたら、パン += x×8、チルト −= y×6。範囲はパン ±40・チルト −15〜30。ループは 0.1 秒休む（10 fps より下）
  - 目: gaze = x×0.6 + パン/40（−1〜1）。顔があれば喜んだ顔
  - 認識: SFace の 128 次元を L2 正規化。人ごとに複数の見本の最大のコサイン。しきい値 0.38。1 秒に 1 回、3 回のうち 2 回
  - 覚える: 0.5 秒ごと、40 秒か 40 個まで、15 個で「よこを むいて」。10 個に届かなくても保存していた（直す）。ラベルは常に "child"（直す）
  - あいさつ: 1 秒以上見続けた後、覚えた顔があれば最大 3 秒認識を待つ。見えなかった時間 20 分以上（起動直後も）。11 時前でその日の最初なら「おはよう」、120 分以上なら「おかえり」、それ以外は 3 つの中からどれか。時間帯は 07:00〜20:30。知らない人はまとめて 1 つの「朝」を使っていた（直す）。記録はメモリだけ（直す）
  - 動きの量: 160×120 の灰色にしてぼかし、画素の差が 25 を超える割合。12 Hz で見て、最大値（直近 2 つの小さい方）
  - 静止画: `switch_mode_and_capture_file` で最大の大きさ。フレーム: 640×480 の 1 枚
  - あいさつの言葉が `[say]` で journal に出ていた（直す）
- **Implications**: 数値はサンプルの値を既定にする。直す点は要件に入れた

### rover-core のつなぎ目
- **Sources Consulted**: `src/osekkai/core/{feature,arbiter,handles,motion,idle,quiet,storage,paths,types}.py`
- **Findings**:
  - 首は `Lease.neck.look(pan, tilt)`・`home()`。基盤が `pan_range`・`tilt_range`（既定 ±80・−30〜60）と調整値をかける
  - 待機画面の顔と目は `ctx.set_idle_face(face, gaze)`（DISPLAY を借りずに使える）
  - 静かな状態は `ctx.is_quiet` と `QuietChanged`
  - 保存は `ctx.store`（機能ごと、原子的、ディレクトリは本人だけ）
  - 機能が自分がハードなしで動いているかを知る口はない
- **Implications**: 首はリースで動かす（サンプルの直接操作を直す）。カメラが使えるかは picamera2 を読み込めるかで決める（PC では使えないになり、presence は何もしない）

### OpenCV の口（4.10）
- **Findings**: `cv2.FaceDetectorYN.create(model, "", (w, h), score_threshold, nms_threshold, top_k)` と `setInputSize`・`detect(img) -> (retval, faces)`（各行 15 個: x, y, w, h, 目鼻口の 10 個, score）。`cv2.FaceRecognizerSF.create(model, "")` と `alignCrop(img, row)`・`feature(aligned) -> 1×128`。`cv2.imencode(".jpg", img, [IMWRITE_JPEG_QUALITY, q])`
- **Implications**: アダプタの中だけで使う。1 回の検出は 320×240 で行う

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| 判定は純粋・映像はアダプタ（採用） | 追従・認識の比べ方・投票・あいさつを純粋な Python に、カメラと OpenCV をアダプタに | PC と CI で判定を全部試せる。numpy なしで動く | アダプタの形は実機確認で確かめる | 日課・音声の入口と同じ形 |
| サンプルの形 | 1 つのスレッドで全部を行い、首を直接動かす | 速く写せる | 首の取り合い。PC で試せない | 採らない |

## Design Decisions

### Decision: 顔の特徴は基盤の保存先に JSON で置く
- **Context**: 要件 5.4、6.1、6.4。サンプルは numpy の npz だった
- **Selected Approach**: `ctx.store` の `faces` 鍵に、人ごとの呼び名と見本（128 個の小数の並び）を置く。原子的に書き、ディレクトリは本人だけが読める
- **Rationale**: 開発 PC と CI に numpy がなくても読み書きと比べ方を試せる。40 個 × 128 × 数人でも数百 KB
- **Trade-offs**: npz より大きいが、問題にならない。サンプルの npz は写さない（覚え直す）

### Decision: カメラの貸し借りは presence の `api.py` の口で行う
- **Context**: 要件 7.x。基盤にカメラの資源はない
- **Selected Approach**: `osekkai.features.presence.api.borrow_camera(owner, *, alive)` が「借りたカメラ」を返す。借りている間、presence は見ることと首を止める。返すか、`alive` が偽になるまで続く
- **Rationale**: 基盤を変えない。カメラを使う機能は presence だけを相手にする
- **Trade-offs**: 首（NECK）はカメラと別に、下流の機能が基盤から借りる

### Decision: 顔を覚えるコマンドは、試しの実行と同じく基盤を 1 つの機能だけで動かす
- **Context**: 要件 5.1〜5.5、5.8。声・LCD・カメラが要る
- **Selected Approach**: `python -m osekkai.features.presence --learn-face <名前> --call-name <呼び名>` が単一の実行のロックを取り、`Runtime` を「覚える機能」だけの登録で動かす（日課の試しのコマンドと同じ形）
- **Trade-offs**: 起動の声が流れる（日課の試しと同じ。テストのときだけの振る舞いとして受け入れた）

### Decision: 優先度は見守りの帯（BACKGROUND、10）
- **Context**: 要件 4.1
- **Trade-offs**: ラジコン（30）より下。ほかのどの機能が首・声を求めても譲る

### Decision: 代わりの検出（Haar）は作らない
- **Context**: サンプルは YuNet がないとき Haar にしていた
- **Selected Approach**: モデルは初期設定で必ず置かれる。なければ見ることを止め、self-check で知らせる（8.4、8.5）
- **Rationale**: 精度の低い検出で間違ったあいさつをしない。作るものを減らす

## Risks & Mitigations
- 実機の CPU（日課の声・聞き取りと取り合う）— 1 秒に数回の検出に絞り、静かな状態とほかの機能の間は止める。聞き取りの間は音声の入口が首を借りるので、presence は止まる
- picamera2 の色の並び（RGB888 が実際は BGR の並び）— サンプルと同じく BGR として扱い、実機確認で色を確かめる
- 認識のしきい値（0.38）が家の明るさで合わない — 設定で変えられるようにする

## References
- [OpenCV FaceDetectorYN](https://docs.opencv.org/4.x/df/d20/classcv_1_1FaceDetectorYN.html)
- [OpenCV FaceRecognizerSF](https://docs.opencv.org/4.x/da/d09/classcv_1_1FaceRecognizerSF.html)
- [Picamera2 manual](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf)
