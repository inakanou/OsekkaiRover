"""声の出力（4.6、6.1、6.3〜6.6）。"""

import logging
import threading
import time
from pathlib import Path

import pytest

from osekkai.adapters.fake.clock import FakeClock
from osekkai.adapters.fake.devices import FakePlayer, FakeSynth
from osekkai.core.cache import CacheManager
from osekkai.core.config import VoiceSettings
from osekkai.core.paths import Paths
from osekkai.core.ports import Unavailable
from osekkai.core.types import DeviceUnavailable, SpeechFailed
from osekkai.core.voice import MAX_LINE_CHARS, VoiceService

SECRET = "ひみつのことばポンポコリン"


@pytest.fixture
def paths(xdg_dirs: dict[str, Path]) -> Paths:
    return Paths()


@pytest.fixture
def cache(paths: Paths, clock: FakeClock) -> CacheManager:
    return CacheManager(paths.cache_dir, max_bytes=10 * 1024 * 1024, clock=clock)


@pytest.fixture
def settings() -> VoiceSettings:
    return VoiceSettings(sink="speaker")


def _tts_files(cache: CacheManager) -> list[Path]:
    tts = cache.root / "core" / "tts"
    if not tts.exists():
        return []
    return sorted(p for p in tts.iterdir() if p.is_file())


def test_文章を合成して設定の再生先で流す(cache: CacheManager, settings: VoiceSettings) -> None:
    synth, player = FakeSynth(), FakePlayer()
    VoiceService(synth, player, cache, settings).speak("こんにちは。")
    assert synth.lines == ["こんにちは。"]
    assert len(player.played) == 1
    wav, sink = player.played[0]
    assert sink == "speaker"
    assert wav.parent == cache.root / "core" / "tts"


def test_同じ文章は合成を1回だけにする(cache: CacheManager, settings: VoiceSettings) -> None:
    synth, player = FakeSynth(), FakePlayer()
    voice = VoiceService(synth, player, cache, settings)
    voice.speak("おはよう。")
    voice.speak("おはよう。")
    assert synth.lines == ["おはよう。"]
    assert len(player.played) == 2
    assert player.played[0][0] == player.played[1][0]
    files = _tts_files(cache)
    assert len(files) == 1
    assert len(files[0].stem) == 16
    assert files[0].suffix == ".wav"


def test_声の設定が変われば作り直す(cache: CacheManager) -> None:
    synth, player = FakeSynth(), FakePlayer()
    VoiceService(synth, player, cache, VoiceSettings(pitch=1.0)).speak("おはよう。")
    VoiceService(synth, player, cache, VoiceSettings(pitch=2.0)).speak("おはよう。")
    VoiceService(synth, player, cache, VoiceSettings(speed=1.5)).speak("おはよう。")
    VoiceService(synth, player, cache, VoiceSettings(htsvoice=Path("/v/b.htsvoice"))).speak(
        "おはよう。"
    )
    assert len(synth.lines) == 4
    assert len(_tts_files(cache)) == 4


def test_残さない指定ならキャッシュに残さない(cache: CacheManager, settings: VoiceSettings) -> None:
    synth, player = FakeSynth(), FakePlayer()
    voice = VoiceService(synth, player, cache, settings)
    voice.speak("ないしょだよ。", keep=False)
    voice.speak("ないしょだよ。", keep=False)
    assert synth.lines == ["ないしょだよ。", "ないしょだよ。"]
    assert len(player.played) == 2
    assert _tts_files(cache) == []
    assert not any(wav.exists() for wav, _ in player.played)


def test_残さない指定は失敗しても一時ファイルを消す(
    cache: CacheManager, settings: VoiceSettings
) -> None:
    player = FakePlayer(fail=True)
    voice = VoiceService(FakeSynth(), player, cache, settings)
    with pytest.raises(SpeechFailed):
        voice.speak("ないしょだよ。", keep=False)
    assert _tts_files(cache) == []


def test_ログに文章を出さない(
    cache: CacheManager, settings: VoiceSettings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    synth = FakeSynth()
    voice = VoiceService(synth, FakePlayer(), cache, settings)
    voice.speak(f"{SECRET}。")
    voice.speak(f"{SECRET}。")
    voice.speak(f"{SECRET}だよ。", keep=False)
    synth.fail_on.add("synthesize")
    with pytest.raises(SpeechFailed):
        voice.speak(f"{SECRET}かな。")
    assert caplog.records
    assert all(SECRET not in r.getMessage() for r in caplog.records)
    assert SECRET not in caplog.text
    messages = [r.getMessage() for r in caplog.records]
    assert f"話した（{len(SECRET) + 1} 文字、キャッシュなし）" in messages
    assert f"話した（{len(SECRET) + 1} 文字、キャッシュあり）" in messages


def test_合成に失敗したら話せないと知らせて次は話せる(
    cache: CacheManager, settings: VoiceSettings, caplog: pytest.LogCaptureFixture
) -> None:
    synth, player = FakeSynth(fail=True), FakePlayer()
    voice = VoiceService(synth, player, cache, settings)
    with pytest.raises(SpeechFailed):
        voice.speak("あいうえお。")
    assert player.played == []
    assert _tts_files(cache) == []  # 作りかけを残さない
    assert "FakeDeviceError" in caplog.text
    synth.fail_on.clear()
    voice.speak("あいうえお。")
    assert len(player.played) == 1


def test_再生に失敗したら話せないと知らせて次は話せる(
    cache: CacheManager, settings: VoiceSettings, caplog: pytest.LogCaptureFixture
) -> None:
    player = FakePlayer(fail=True)
    voice = VoiceService(FakeSynth(), player, cache, settings)
    with pytest.raises(SpeechFailed):
        voice.speak("かきくけこ。")
    assert "FakeDeviceError" in caplog.text
    player.fail_on.clear()
    voice.speak("かきくけこ。")
    assert len(player.played) == 1


def test_使えないデバイスなら使えないと知らせる(
    cache: CacheManager, settings: VoiceSettings
) -> None:
    gone = Unavailable("synth", "なし")
    with pytest.raises(DeviceUnavailable):
        VoiceService(gone, FakePlayer(), cache, settings).speak("あ。")
    with pytest.raises(DeviceUnavailable):
        VoiceService(FakeSynth(), Unavailable("player", "なし"), cache, settings).speak("あ。")


def test_改行を除き文の区切りで分けて順に流す(cache: CacheManager, settings: VoiceSettings) -> None:
    synth, player = FakeSynth(), FakePlayer()
    VoiceService(synth, player, cache, settings).speak("きょうは\nはれ。\r\nげんき！ほんと？おわり")
    assert synth.lines == ["きょうははれ。", "げんき！", "ほんと？", "おわり"]
    assert len(player.played) == 4


def test_長い文章は300文字以内に分ける(cache: CacheManager, settings: VoiceSettings) -> None:
    synth = FakeSynth()
    text = "あ" * 300 + "い" * 300 + "う" * 100 + "。" + "え" * 10
    VoiceService(synth, FakePlayer(), cache, settings).speak(text)
    assert MAX_LINE_CHARS == 300
    assert all(0 < len(line) <= MAX_LINE_CHARS for line in synth.lines)
    assert "".join(synth.lines) == text
    assert len(synth.lines) == 4


def test_空の文章は何もしない(cache: CacheManager, settings: VoiceSettings) -> None:
    synth, player = FakeSynth(), FakePlayer()
    VoiceService(synth, player, cache, settings).speak(" \n ")
    assert synth.lines == []
    assert player.played == []


def test_上限を超えたら古いキャッシュから消す(paths: Paths, clock: FakeClock) -> None:
    # 偽物の WAV は 1 個 364 バイト。2 個分だけ入る上限にする
    cache = CacheManager(paths.cache_dir, max_bytes=800, clock=clock)
    synth = FakeSynth()
    voice = VoiceService(synth, FakePlayer(), cache, VoiceSettings())
    voice.speak("いち。")
    clock.advance(10)
    voice.speak("に。")
    clock.advance(10)
    voice.speak("いち。")  # 使ったので新しくなる
    clock.advance(10)
    voice.speak("さん。")
    assert len(_tts_files(cache)) == 2
    voice.speak("いち。")
    voice.speak("さん。")
    assert synth.lines == ["いち。", "に。", "さん。"]
    voice.speak("に。")
    assert synth.lines[-1] == "に。"


class _BlockingPlayer:
    """1 回目の再生を合図まで止め、同時に再生した数の最大を数える。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.started = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def play(self, wav: Path, sink: str) -> None:
        with self._lock:
            self.started += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            first = self.started == 1
        if first:
            self.entered.set()
            self.release.wait(5)
        with self._lock:
            self.active -= 1


def test_同時の2つの発話は重ならない(cache: CacheManager, settings: VoiceSettings) -> None:
    synth, player = FakeSynth(), _BlockingPlayer()
    voice = VoiceService(synth, player, cache, settings)
    first = threading.Thread(target=voice.speak, args=("ひとつめ。ふたつめ。",))
    first.start()
    assert player.entered.wait(5)
    second = threading.Thread(target=voice.speak, args=("みっつめ。",))
    second.start()
    time.sleep(0.1)
    assert second.is_alive()
    assert synth.lines == ["ひとつめ。"]  # 2 つめの発話は合成も始めていない
    player.release.set()
    first.join(5)
    second.join(5)
    assert player.started == 3
    assert player.max_active == 1
    assert synth.lines == ["ひとつめ。", "ふたつめ。", "みっつめ。"]


def test_ロックを取った後に続けてはいけなければ何も話さない(
    cache: CacheManager, settings: VoiceSettings
) -> None:
    synth, player = FakeSynth(), FakePlayer()
    voice = VoiceService(synth, player, cache, settings)
    assert voice.speak("いいえ。", should_continue=lambda: False) is False
    assert synth.lines == []
    assert player.played == []
    assert voice.speak("はい。", should_continue=lambda: True) is True
    assert synth.lines == ["はい。"]


def test_続けてよいかは文の間では確かめない(cache: CacheManager, settings: VoiceSettings) -> None:
    synth, player = FakeSynth(), FakePlayer()
    voice = VoiceService(synth, player, cache, settings)
    answers = iter([True, False, False])
    assert voice.speak("ひとつ。ふたつ。", should_continue=lambda: next(answers)) is True
    assert synth.lines == ["ひとつ。", "ふたつ。"]
