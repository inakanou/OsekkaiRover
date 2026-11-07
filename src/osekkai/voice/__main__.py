"""開発用の聞き取りの確認（`python -m osekkai.voice --listen-once`。9.3、9.4）。

実機のマイクとモデルで 1 回だけ録音して文字にし、文字とかかった時間を標準出力にだけ出す。
ログには文字を出さない。基盤は起動しない（声・画面・ボタンは使わない）。

- 単一の実行のロックを取る。取れなければ（ロボットが動いている）、録音せずに 75 で終える
- 設定は入口の節（`voice:`）だけを読む。設定ファイルがなければ既定値で動く（作らない）。
  指定した設定ファイルがない・読めない・誤りがあるときは 1 で終える
- 準備ができない・聞き取りに失敗したときは、決まった理由で 1 で終える

基盤の起動（core.runtime）は読み込まないので、75 と知らせの文言はここに書く。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from osekkai.core.config import ConfigLoader, ConfigReport
from osekkai.core.instance_lock import InstanceLock
from osekkai.core.logging_setup import configure_logging
from osekkai.core.paths import Paths
from osekkai.voice.adapters.whisper import WhisperRecognizer
from osekkai.voice.config import VoiceConfig, model_dir
from osekkai.voice.recognizer import RecognitionFailed, Recognizer, RecognizerState

VOICE_SECTION: Final = "voice"
EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_ALREADY_RUNNING: Final = 75  # 基盤（core.runtime）の「すでに動いている」と同じ番号

ALREADY_RUNNING_TEXT: Final = "ロボットが動いています。止めてから試してください"
LOCK_UNUSABLE_TEXT: Final = "単一の実行のロックを確かめられません"
CONFIG_ERROR_TEXT: Final = "設定に誤りがあります"
NOT_READY_TEXT: Final = "聞き取りの準備ができません（マイクかモデルを確かめてください）"
FAILED_TEXT: Final = "聞き取りに失敗しました"
NOT_HEARD_TEXT: Final = "聞き取れませんでした"
HEARD_PREFIX: Final = "聞き取った言葉: "


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("秒の数を書いてください") from None
    if not value > 0:
        raise argparse.ArgumentTypeError("0 より大きい秒の数を書いてください")
    return value


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m osekkai.voice", description="音声の入口の開発用の確かめ"
    )
    parser.add_argument(
        "--listen-once",
        action="store_true",
        required=True,
        help="1 回だけ録音して、聞き取った言葉とかかった時間を出す",
    )
    parser.add_argument(
        "--seconds",
        type=_positive_seconds,
        metavar="N",
        help="録音の長さ（秒）。書かなければ設定の listen_seconds",
    )
    parser.add_argument("--config", type=Path, metavar="PATH", help="設定ファイルを指定する")
    parser.add_argument("--verbose", action="store_true", help="詳しいログを出す")
    return parser.parse_args(argv)


def _build_recognizer(config: VoiceConfig, paths: Paths) -> Recognizer:
    """聞き取りを作る（テストで偽物に替える）。"""
    return WhisperRecognizer(config, model_dir(paths, config))


def _load_config(paths: Paths, override: Path | None) -> VoiceConfig | None:
    """入口の節を読む。誤りがあれば標準エラーに出して None。

    ほかの機能の節は「知らない項目」になるので、基盤の読み込みのログは止め、
    誤りだけをここで出す（基盤の文言は値を含まない）。
    """
    config_logger = logging.getLogger(ConfigLoader.__module__)
    level = config_logger.level
    config_logger.setLevel(logging.CRITICAL + 1)
    try:
        report = ConfigLoader(paths, {VOICE_SECTION: VoiceConfig}).load(
            override, create_missing=False
        )
    except OSError as error:  # 読めないなど。パスは出さない
        print(f"{CONFIG_ERROR_TEXT}: {type(error).__name__}", file=sys.stderr)
        return None
    finally:
        config_logger.setLevel(level)
    if report.missing:
        return VoiceConfig()  # 既定の場所にないときは既定値で試す
    return _voice_config(report)


def _voice_config(report: ConfigReport) -> VoiceConfig | None:
    messages = [*report.fatal_errors, *report.feature_errors.get(VOICE_SECTION, ())]
    config = report.features.get(VOICE_SECTION)
    if messages or not isinstance(config, VoiceConfig):
        print(CONFIG_ERROR_TEXT, file=sys.stderr)
        for message in messages:
            print(f"  {message}", file=sys.stderr)
        return None
    return config


def listen_once(args: argparse.Namespace, paths: Paths, monotonic: Callable[[], float]) -> int:
    """ロックを取った後の、1 回の聞き取り。"""
    config = _load_config(paths, args.config)
    if config is None:
        return EXIT_ERROR
    recognizer = _build_recognizer(config, paths)
    recognizer.prepare()
    if recognizer.state is not RecognizerState.READY:
        print(NOT_READY_TEXT, file=sys.stderr)
        return EXIT_ERROR

    marks: dict[bool, float] = {}

    def on_recording(recording: bool) -> None:
        marks[recording] = monotonic()

    seconds = args.seconds if args.seconds is not None else config.listen_seconds
    try:
        text = recognizer.listen(
            seconds, hints=(), should_continue=lambda: True, on_recording=on_recording
        )
    except RecognitionFailed as error:
        print(f"{FAILED_TEXT}: {error.reason}", file=sys.stderr)
        return EXIT_ERROR
    finished = monotonic()
    started = marks.get(True, finished)
    stopped = marks.get(False, finished)

    # 聞き取った言葉は標準出力にだけ出す（ログには出さない。9.3）
    print(f"{HEARD_PREFIX}{text}" if text else NOT_HEARD_TEXT)
    print(f"録音 {stopped - started:.1f} 秒、文字にする {finished - stopped:.1f} 秒")
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None, *, monotonic: Callable[[], float] = time.monotonic
) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    paths = Paths()
    lock = InstanceLock(paths.lock_file)
    try:
        acquired = lock.acquire()
    except OSError as error:
        print(f"{LOCK_UNUSABLE_TEXT}: {type(error).__name__}", file=sys.stderr)
        return EXIT_ERROR
    if not acquired:
        # 録音を始めない（動いているロボットのマイクを取り合わない。9.4）
        print(ALREADY_RUNNING_TEXT, file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    try:
        return listen_once(args, paths, monotonic)
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
