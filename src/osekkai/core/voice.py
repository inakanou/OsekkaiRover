"""声の出力（4.6、6.1、6.3〜6.6）。

文章を文の区切りで分け、分けた単位ごとに合成して続けて再生する。合成した声は
`cache/osekkai/core/tts/<鍵>.wav` に残し、同じ文章では合成を省く。
ログには文字数とキャッシュの有無だけを出し、文章は出さない。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from osekkai.core.paths import ensure_private_dir
from osekkai.core.ports import AudioPlayerPort, SpeechSynthPort, Unavailable
from osekkai.core.types import DeviceUnavailable, SpeechFailed

if TYPE_CHECKING:
    from osekkai.core.cache import CacheManager
    from osekkai.core.config import VoiceSettings

logger = logging.getLogger(__name__)

MAX_LINE_CHARS = 300  # Open JTalk の 1 行の制限
CACHE_OWNER = "core"
TTS_DIR = "tts"
KEY_CHARS = 16

# 区切りの記号は前の文に付けたまま分ける
_SENTENCE_END = re.compile(r"(?<=[。！？])")


def split_lines(text: str) -> list[str]:
    """改行を除き、文の区切りで 1 行 300 文字以内に分ける。空の行は除く。"""
    joined = text.replace("\r", "").replace("\n", "")
    lines: list[str] = []
    for sentence in _SENTENCE_END.split(joined):
        for start in range(0, len(sentence), MAX_LINE_CHARS):
            piece = sentence[start : start + MAX_LINE_CHARS]
            if piece.strip():
                lines.append(piece)
    return lines


class VoiceService:
    """文章を声にして流す。発話を 1 つずつにし、キャッシュを管理する。"""

    def __init__(
        self,
        synth: SpeechSynthPort | Unavailable,
        player: AudioPlayerPort | Unavailable,
        cache: CacheManager,
        settings: VoiceSettings,
    ) -> None:
        self._synth = synth
        self._player = player
        self._cache = cache
        self._settings = settings
        # 発話の終わりまで持つ（4.6）
        self._lock = threading.Lock()

    def speak(self, text: str, *, keep: bool = True) -> None:
        """文章を話し、再生が終わってから戻る。

        話せなければ `SpeechFailed`、デバイスが使えなければ `DeviceUnavailable` を投げる。
        """
        lines = split_lines(text)
        if not lines:
            return
        synth = self._synth
        player = self._player
        if isinstance(synth, Unavailable):
            raise _unavailable(synth)
        if isinstance(player, Unavailable):
            raise _unavailable(player)
        with self._lock:
            try:
                all_cached = self._speak_lines(synth, player, lines, keep)
            except Exception as error:
                logger.warning("話せませんでした: %s", type(error).__name__)
                raise SpeechFailed(type(error).__name__) from error
            finally:
                if keep:
                    self._enforce_limit()
        logger.info(
            "話した（%d 文字、キャッシュ%s）",
            sum(len(line) for line in lines),
            "あり" if all_cached else "なし",
        )

    def _speak_lines(
        self,
        synth: SpeechSynthPort,
        player: AudioPlayerPort,
        lines: list[str],
        keep: bool,
    ) -> bool:
        """各行を合成（またはキャッシュから）して再生する。すべてキャッシュなら True。"""
        directory = ensure_private_dir(self._cache.dir_for(CACHE_OWNER) / TTS_DIR)
        all_cached = True
        for line in lines:
            if keep:
                wav, cached = self._cached_wav(synth, line, directory)
                all_cached = all_cached and cached
                player.play(wav, self._settings.sink)
            else:
                all_cached = False
                wav = self._new_temp(directory)
                try:
                    synth.synthesize(line, self._settings, wav)
                    player.play(wav, self._settings.sink)
                finally:
                    with contextlib.suppress(FileNotFoundError):
                        wav.unlink()
        return all_cached

    def _cached_wav(self, synth: SpeechSynthPort, line: str, directory: Path) -> tuple[Path, bool]:
        """キャッシュの声を返す。なければ合成して入れる。（パス、キャッシュにあったか）。"""
        path = directory / f"{self._key(line)}.wav"
        if path.is_file():
            try:
                self._cache.touch(path)
                return path, True
            except FileNotFoundError:
                pass  # ほかで消えたので作り直す
        temp = self._new_temp(directory)
        try:
            synth.synthesize(line, self._settings, temp)
            os.replace(temp, path)  # 作りかけを使わない
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()
        with contextlib.suppress(FileNotFoundError):
            self._cache.touch(path)
        return path, False

    def _key(self, line: str) -> str:
        settings = self._settings
        source = "\0".join(
            (line, str(settings.htsvoice), repr(settings.pitch), repr(settings.speed))
        )
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:KEY_CHARS]

    @staticmethod
    def _new_temp(directory: Path) -> Path:
        fd, name = tempfile.mkstemp(prefix=".tmp-", suffix=".wav", dir=directory)
        os.close(fd)
        return Path(name)

    def _enforce_limit(self) -> None:
        try:
            self._cache.enforce_limit()
        except OSError as error:
            logger.warning("キャッシュの上限を守れませんでした: %s", type(error).__name__)


def _unavailable(device: Unavailable) -> DeviceUnavailable:
    logger.warning("声のデバイスが使えないので、話せませんでした: %s", device.device)
    return DeviceUnavailable(device.device)
