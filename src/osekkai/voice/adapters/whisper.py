"""実機の聞き取り: arecord で録音し、faster-whisper で文字にする（1.3、1.7、6.4、6.5、7.1、7.2）。

- faster_whisper と numpy は関数の中で遅れて読み込む（開発 PC と CI には入れない）
- サブプロセス（arecord）を使うのは、入口の中でこのモジュールだけ
- 声のバイト列と配列は関数の中だけに置き、ファイルに書かない。外にも送らない
- ログには決まった文言と例外の型の名前だけを出す（マイクの名前、文字、語句は出さない）
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any

from osekkai.voice.config import VoiceConfig
from osekkai.voice.recognizer import ListenCancelled, RecognitionFailed, RecognizerState
from osekkai.voice.texts import DEFAULT_HINT

logger = logging.getLogger(__name__)

# モデルのディレクトリに要るファイル（CTranslate2 の形）
MODEL_FILES: tuple[str, ...] = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")

MSG_RECORD_FAILED = "録音に失敗しました"
MSG_NOT_READY = "準備ができていません"

SAMPLE_RATE = 16000
CPU_THREADS = 3  # 日課の声と基盤のスレッドのために、1 つのコアを空ける
TARGET_PEAK = 0.9  # 持ち上げた後の最大値
MAX_PROMPT_CHARS = 200  # ヒントの長さの上限（長すぎると文字にするのが遅くなる）

_LIST_TIMEOUT_SECONDS = 5.0  # arecord -L の打ち切り
_POLL_SECONDS = 0.1  # 録音の間に、続けてよいかを見る間隔
_RECORD_MARGIN_SECONDS = 3.0  # 録音の長さより、これだけ待っても終わらなければ止める
_STOP_WAIT_SECONDS = 1.0
_READ_SIZE = 4096


def whisper_available() -> bool:
    """faster_whisper を読み込めるかを返す（読み込みはしない）。

    機能と self-check はこれを呼ぶ（faster_whisper の名前を、このモジュールの外に出さない）。
    """
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def mic_present(device: str) -> bool:
    """`arecord -L` の一覧に `device` の名前の行があるかを返す（短い時間で打ち切る）。"""
    try:
        result = subprocess.run(
            ["arecord", "-L"],
            capture_output=True,
            text=True,
            errors="replace",  # 説明に UTF-8 でない文字があっても読む
            timeout=_LIST_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        logger.warning("マイクの一覧を読めません: %s", type(error).__name__)
        return False
    if result.returncode != 0:
        logger.warning("マイクの一覧を読めません: 終わりの番号 %d", result.returncode)
        return False
    # 名前は行の先頭に書かれる。説明の行は字下げされている
    return any(
        line == device for line in result.stdout.splitlines() if line and not line[0].isspace()
    )


def model_files_present(model_dir: Path) -> bool:
    """モデルのディレクトリに、要るファイルがそろっているかを返す。"""
    return all((model_dir / name).is_file() for name in MODEL_FILES)


def compute_gain(peak: float, max_gain: float) -> float:
    """最大値が TARGET_PEAK になる倍率を、`max_gain` を上限にして返す。"""
    return min(TARGET_PEAK / max(abs(peak), 1e-4), max_gain)


def arecord_args(device: str, seconds: float) -> list[str]:
    """録音の arecord の引数（16 kHz・モノ・16 bit・生のバイト列を標準出力へ）。"""
    # -d 0 は止まらない録音になるので、1 秒より短くしない
    duration = max(1, int(round(seconds)))
    return [
        "arecord",
        "-q",
        "-D",
        device,
        "-f",
        "S16_LE",
        "-r",
        str(SAMPLE_RATE),
        "-c",
        "1",
        "-d",
        str(duration),
        "-t",
        "raw",
    ]


def build_prompt(hints: Sequence[str]) -> str:
    """既定のヒントの後ろに、登録の語句を長さの上限まで足す。"""
    prompt = DEFAULT_HINT
    added: list[str] = []
    for hint in hints:
        if not hint or hint in added:
            continue
        candidate = f"{DEFAULT_HINT} {'。'.join([*added, hint])}"
        if len(candidate) > MAX_PROMPT_CHARS:
            break
        added.append(hint)
        prompt = candidate
    return prompt


def _to_audio(raw: bytes, max_gain: float) -> Any:
    """16 bit のバイト列を float32（-1〜1）にして、音量を上限つきで持ち上げる。"""
    import numpy as np

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    peak = float(np.max(np.abs(samples)))
    return samples * np.float32(compute_gain(peak, max_gain))


def _read_all(stream: IO[bytes], chunks: list[bytes]) -> None:
    """標準出力を終わりまで読む（録音のスレッド）。"""
    try:
        while True:
            chunk = stream.read(_READ_SIZE)
            if not chunk:
                return
            chunks.append(chunk)
    except (OSError, ValueError):
        # 止めたときに閉じられた、など。足りない分は録音の失敗として扱う
        return


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """arecord を止める。止まらなければ殺す。"""
    try:
        proc.terminate()
        proc.wait(timeout=_STOP_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=_STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("録音のプロセスが止まりません")
    except OSError as error:
        logger.warning("録音のプロセスを止められません: %s", type(error).__name__)


class WhisperRecognizer:
    """arecord と faster-whisper の聞き取り（Recognizer の口を満たす）。"""

    def __init__(
        self, config: VoiceConfig, model_dir: Path, *, log: logging.Logger | None = None
    ) -> None:
        self._config = config
        self._model_dir = model_dir
        self._log = log or logger
        self._model: Any = None
        self._state = RecognizerState.PREPARING

    @property
    def state(self) -> RecognizerState:
        return self._state

    def prepare(self) -> None:
        """マイクを確かめ、モデルを読み込む。失敗は UNAVAILABLE にする（6.4、6.5）。"""
        if not mic_present(self._config.mic_device):
            self._log.warning("マイクが見つかりません")
            self._state = RecognizerState.UNAVAILABLE
            return
        if not model_files_present(self._model_dir):
            self._log.warning("聞き取りのモデルがありません")
            self._state = RecognizerState.UNAVAILABLE
            return
        try:
            from faster_whisper import WhisperModel

            model = WhisperModel(
                str(self._model_dir),
                device="cpu",
                compute_type="int8",
                cpu_threads=CPU_THREADS,
            )
        except Exception as error:
            self._log.warning("聞き取りのモデルを読み込めません: %s", type(error).__name__)
            self._state = RecognizerState.UNAVAILABLE
            return
        self._model = model
        self._state = RecognizerState.READY
        self._log.info("聞き取りの準備ができました")

    def listen(
        self,
        seconds: float,
        *,
        hints: Sequence[str],
        should_continue: Callable[[], bool],
        on_recording: Callable[[bool], None],
    ) -> str:
        model = self._model
        if self._state is not RecognizerState.READY or model is None:
            raise RecognitionFailed(MSG_NOT_READY)
        raw = self._record(seconds, should_continue, on_recording)
        if not should_continue():
            raise ListenCancelled
        try:
            audio = _to_audio(raw, self._config.max_gain)
        except Exception as error:
            raise RecognitionFailed.from_error(error) from None
        del raw
        return self._transcribe(model, audio, hints, should_continue)

    def _record(
        self,
        seconds: float,
        should_continue: Callable[[], bool],
        on_recording: Callable[[bool], None],
    ) -> bytes:
        """録音してバイト列を返す。終わったら（失敗や取り消しでも）録音の合図を消す。"""
        try:
            proc = subprocess.Popen(
                arecord_args(self._config.mic_device, seconds),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError) as error:
            raise RecognitionFailed.from_error(error) from None
        try:
            # 合図が例外を出しても、finally で arecord を止める
            on_recording(True)
            stdout = proc.stdout
            if stdout is None:
                raise RecognitionFailed(MSG_RECORD_FAILED)
            chunks: list[bytes] = []
            reader = threading.Thread(
                target=_read_all, args=(stdout, chunks), name="voice-arecord", daemon=True
            )
            reader.start()
            polls = max(1, int((seconds + _RECORD_MARGIN_SECONDS) / _POLL_SECONDS) + 1)
            for _ in range(polls):
                if not should_continue():
                    _stop(proc)
                    raise ListenCancelled
                reader.join(_POLL_SECONDS)
                if not reader.is_alive():
                    break
            else:
                self._log.warning("録音が終わりません")
                _stop(proc)
                raise RecognitionFailed(MSG_RECORD_FAILED)
            try:
                returncode = proc.wait(timeout=_STOP_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                _stop(proc)
                raise RecognitionFailed(MSG_RECORD_FAILED) from None
            raw = b"".join(chunks)
            chunks.clear()
            if returncode != 0 or len(raw) < 2:
                raise RecognitionFailed(MSG_RECORD_FAILED)
            # 16 bit に足りない端は捨てる
            return raw[: len(raw) - len(raw) % 2]
        finally:
            if proc.poll() is None:
                _stop(proc)
            if proc.stdout is not None:
                with contextlib.suppress(OSError):
                    proc.stdout.close()
            # 文字にする前に録音の合図を消す（LED を消す）
            on_recording(False)

    def _transcribe(
        self,
        model: Any,
        audio: Any,
        hints: Sequence[str],
        should_continue: Callable[[], bool],
    ) -> str:
        """文字にする。segment ごとに続けてよいかを見て、偽なら残りを計算しない。"""
        parts: list[str] = []
        try:
            segments, _info = model.transcribe(
                audio,
                language="ja",
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
                without_timestamps=True,
                initial_prompt=build_prompt(hints),
            )
            for segment in segments:
                parts.append(str(segment.text).strip())
                if not should_continue():
                    raise ListenCancelled
        except ListenCancelled:
            raise
        except Exception as error:
            raise RecognitionFailed.from_error(error) from None
        return "".join(parts)
