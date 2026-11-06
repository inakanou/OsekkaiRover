"""実機の軽い確かめ（device-deployment 6.1、6.5）。動かさない・鳴らさない。

- I2C: i2cdetect と同じ quick write で、アドレスが応えるかだけを見る。データは書かない
- GPIO: 線を入力として取れるかを見て、すぐ放す。出力にはしない
- 再生先: `pactl` の一覧に、設定の再生先（空なら既定の再生先）があるか
- 合成: 決まった短い文を Open JTalk で WAV にする。再生はしない
- 時刻: timedatectl（使えなければ印のファイル）

モーターとサーボには書き込まない（そのための口を持たない）。
バスと gpiochip は確かめのたびに開いて、終わったら閉じる。
例外のメッセージには、再生先の名前も文章も入れない。
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from osekkai.adapters.pi.gpio import Gpio
from osekkai.adapters.pi.i2c_bus import I2cBus
from osekkai.adapters.pi.openjtalk import OpenJTalkSynth, normalize_wav
from osekkai.adapters.pi.timesync import SYNC_MARKER, TimedatectlTimeSync

if TYPE_CHECKING:
    from osekkai.core.config import VoiceSettings

logger = logging.getLogger(__name__)

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]

PROBE_PHRASE: Final = "てすと"  # 合成の確かめの決まった短い文
PACTL: Final = "pactl"
PACTL_TIMEOUT: Final = 3.0


class ProbeGpio(Gpio):
    """線を入力として取れるかだけを確かめる Gpio。"""

    def input_free(self, pin: int) -> bool:
        """入力として取れたら、すぐ放して True。取れなければ（ほかが持つなど）False。"""
        with self._lock:
            try:
                # ボタンと同じくプルアップで取る（線の電圧を変えない）
                self._lg.gpio_claim_input(self._handle, pin, self._lg.SET_PULL_UP)
            except Exception as error:  # lgpio.error など
                logger.debug("線を入力として取れません: %s", type(error).__name__)
                return False
            self._lg.gpio_free(self._handle, pin)
            return True


class PiProbes:
    """`osekkai.checks.Probes` の実機の実装。"""

    def __init__(
        self,
        *,
        i2c_bus: Callable[[], I2cBus] = I2cBus,
        gpio: Callable[[], ProbeGpio] = ProbeGpio,
        run: Runner = subprocess.run,
        normalize: Callable[[Path], None] = normalize_wav,
        timesync_marker: Path = SYNC_MARKER,
    ) -> None:
        self._i2c_bus = i2c_bus
        self._gpio = gpio
        self._run = run
        self._synth = OpenJTalkSynth(run=run, normalize=normalize)
        self._timesync = TimedatectlTimeSync(run=run, marker=timesync_marker)

    def i2c_responds(self, address: int) -> bool:
        bus = self._i2c_bus()
        try:
            return bus.probe(address)  # バスを開けないときは OSError のまま
        finally:
            bus.close()

    def gpio_input_free(self, pin: int) -> bool:
        gpio = self._gpio()
        try:
            return gpio.input_free(pin)
        finally:
            gpio.close()

    def audio_sink_exists(self, sink: str) -> bool:
        names = self._sink_names()
        if not names:
            return False
        if sink:
            return sink in names
        default = self._pactl("get-default-sink").strip()
        return default in names

    def synthesize(self, settings: VoiceSettings, out: Path) -> None:
        self._synth.synthesize(PROBE_PHRASE, settings, out)

    def time_synchronized(self) -> bool:
        return self._timesync.is_synchronized()

    def _sink_names(self) -> set[str]:
        names: set[str] = set()
        for line in self._pactl("list", "short", "sinks").splitlines():
            columns = line.split("\t")
            if len(columns) >= 2 and columns[1]:
                names.add(columns[1])
        return names

    def _pactl(self, *args: str) -> str:
        """pactl の標準出力。失敗は値を含まない例外にする（コマンドがないときは OSError）。"""
        try:
            done = self._run(
                [PACTL, *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=PACTL_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{PACTL} timed out after {PACTL_TIMEOUT:g} s") from None
        if done.returncode != 0:
            raise RuntimeError(f"{PACTL} failed with return code {done.returncode}")
        return str(done.stdout)
