"""声の合成・再生と時刻の同期のアダプタのテスト。

外部のコマンド（open_jtalk、paplay、timedatectl）は実行せず、偽の `run` を渡す。
numpy は読み込まない（正規化は、偽の拡大の関数を渡して確かめる）。
"""

from __future__ import annotations

import struct
import subprocess
import wave
from pathlib import Path
from typing import Any

import pytest

from osekkai.adapters.pi.openjtalk import (
    PEAK_TARGET,
    SYNTH_TIMEOUT,
    OpenJTalkSynth,
    normalize_wav,
    peak_gain,
)
from osekkai.adapters.pi.pipewire import PLAY_TIMEOUT, PaplayPlayer
from osekkai.adapters.pi.timesync import QUERY_TIMEOUT, TimedatectlTimeSync
from osekkai.core.config import VoiceSettings

HIDDEN = "たろうくん、おはよう"


class FakeRun:
    """subprocess.run の代わり。呼ばれた引数を残し、決めた結果を返す。"""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: Any = b"",
        raises: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        self.calls.append((list(args), kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


def _settings(**kw: Any) -> VoiceSettings:
    base: dict[str, Any] = {
        "open_jtalk": "/usr/bin/open_jtalk",
        "dictionary": Path("/dic/naist"),
        "htsvoice": Path("~/voices/a.htsvoice"),
        "pitch": 3.0,
        "speed": 1.05,
        "sink": "",
    }
    base.update(kw)
    return VoiceSettings(**base)


def _no_normalize(path: Path) -> None:
    pass


# ---- 合成 ----


def test_合成はopen_jtalkの引数を組み立て1行を標準入力で渡す(tmp_path: Path) -> None:
    run = FakeRun()
    seen: list[Path] = []
    synth = OpenJTalkSynth(run=run, normalize=seen.append)
    out = tmp_path / "x.wav"
    synth.synthesize(HIDDEN, _settings(), out)

    assert len(run.calls) == 1
    args, kwargs = run.calls[0]
    assert args == [
        "/usr/bin/open_jtalk",
        "-x",
        "/dic/naist",
        "-m",
        str(Path("~/voices/a.htsvoice").expanduser()),
        "-fm",
        "3.0",
        "-r",
        "1.05",
        "-ow",
        str(out),
    ]
    assert kwargs["input"] == (HIDDEN + "\n").encode("utf-8")
    assert kwargs["timeout"] == SYNTH_TIMEOUT
    assert kwargs.get("shell", False) is False
    assert seen == [out]


def test_合成は声のファイルのチルダを展開する(tmp_path: Path) -> None:
    run = FakeRun()
    OpenJTalkSynth(run=run, normalize=_no_normalize).synthesize(
        "a", _settings(htsvoice=Path("~/v.htsvoice")), tmp_path / "x.wav"
    )
    voice = run.calls[0][0][4]
    assert "~" not in voice
    assert voice == str(Path.home() / "v.htsvoice")


def test_合成の行に改行があっても1行だけを渡す(tmp_path: Path) -> None:
    run = FakeRun()
    OpenJTalkSynth(run=run, normalize=_no_normalize).synthesize(
        "あ\nい\r\n", _settings(), tmp_path / "x.wav"
    )
    assert run.calls[0][1]["input"] == "あい\n".encode()


def test_合成の失敗は文章とstderrを含まない例外になる(tmp_path: Path) -> None:
    run = FakeRun(returncode=1, stderr=HIDDEN.encode("utf-8"))
    synth = OpenJTalkSynth(run=run, normalize=_no_normalize)
    with pytest.raises(RuntimeError) as info:
        synth.synthesize(HIDDEN, _settings(), tmp_path / "x.wav")
    msg = str(info.value)
    assert "open_jtalk" in msg and "1" in msg
    assert HIDDEN not in msg and "たろう" not in msg
    assert "たろう" not in repr(info.value.args)


def test_合成の時間切れは文章を含まない例外になる(tmp_path: Path) -> None:
    run = FakeRun(raises=subprocess.TimeoutExpired(["open_jtalk"], 30, output=HIDDEN.encode()))
    synth = OpenJTalkSynth(run=run, normalize=_no_normalize)
    with pytest.raises(RuntimeError) as info:
        synth.synthesize(HIDDEN, _settings(), tmp_path / "x.wav")
    assert "timed out" in str(info.value)
    assert HIDDEN not in str(info.value)
    # 元の例外（出力に文章を持ちうる）はつながない
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


def test_合成のコマンドがないときはOSErrorになる(tmp_path: Path) -> None:
    run = FakeRun(raises=FileNotFoundError(2, "No such file", "open_jtalk"))
    synth = OpenJTalkSynth(run=run, normalize=_no_normalize)
    with pytest.raises(OSError):
        synth.synthesize(HIDDEN, _settings(), tmp_path / "x.wav")


def test_合成の後に正規化が失敗しても文章を含まない(tmp_path: Path) -> None:
    def broken(path: Path) -> None:
        raise ValueError("bad wav")

    synth = OpenJTalkSynth(run=FakeRun(), normalize=broken)
    with pytest.raises(ValueError) as info:
        synth.synthesize(HIDDEN, _settings(), tmp_path / "x.wav")
    assert HIDDEN not in str(info.value)


# ---- 正規化 ----


def _write_wav(path: Path, samples: list[int], rate: int = 48000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _read_wav(path: Path) -> tuple[int, list[int]]:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        data = w.readframes(w.getnframes())
    return rate, list(struct.unpack(f"<{len(data) // 2}h", data))


def _py_scale(frames: bytes, gain: float) -> bytes:
    n = len(frames) // 2
    vals = struct.unpack(f"<{n}h", frames)
    return struct.pack(f"<{n}h", *(round(v * gain) for v in vals))


def _py_peak(frames: bytes) -> int:
    n = len(frames) // 2
    return max((abs(v) for v in struct.unpack(f"<{n}h", frames)), default=0)


def test_最大の振幅は095になる倍率で拡大する() -> None:
    assert peak_gain(1000) == pytest.approx(PEAK_TARGET * 32767 / 1000)
    assert PEAK_TARGET == 0.95
    assert peak_gain(32768) == pytest.approx(0.95 * 32767 / 32768)


def test_無音なら倍率はない() -> None:
    assert peak_gain(0) is None


def test_正規化でWAVの最大の振幅が095になる(tmp_path: Path) -> None:
    path = tmp_path / "x.wav"
    _write_wav(path, [0, 100, -400, 250, 0], rate=48000)
    normalize_wav(path, peak=_py_peak, scale=_py_scale)
    rate, samples = _read_wav(path)
    assert rate == 48000
    assert len(samples) == 5
    assert max(abs(s) for s in samples) == round(0.95 * 32767)
    assert samples[2] < 0  # 符号は保つ
    assert not list(tmp_path.glob("*.tmp"))


def test_無音のWAVはそのまま残す(tmp_path: Path) -> None:
    path = tmp_path / "x.wav"
    _write_wav(path, [0, 0, 0])
    before = path.read_bytes()

    def must_not_scale(frames: bytes, gain: float) -> bytes:
        raise AssertionError("scaled silence")

    normalize_wav(path, peak=_py_peak, scale=must_not_scale)
    assert path.read_bytes() == before


def test_16ビットでないWAVは断る(tmp_path: Path) -> None:
    path = tmp_path / "x.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(8000)
        w.writeframes(b"\x80\x90")
    with pytest.raises(ValueError):
        normalize_wav(path, peak=_py_peak, scale=_py_scale)


# ---- 再生 ----


def test_再生先がなければ既定の再生先で再生する(tmp_path: Path) -> None:
    run = FakeRun()
    wav = tmp_path / "x.wav"
    PaplayPlayer(run=run).play(wav, "")
    args, kwargs = run.calls[0]
    assert args == ["paplay", str(wav)]
    assert kwargs["timeout"] == PLAY_TIMEOUT
    assert kwargs.get("shell", False) is False


def test_再生先を指定して再生する(tmp_path: Path) -> None:
    run = FakeRun()
    wav = tmp_path / "x.wav"
    PaplayPlayer(run=run).play(wav, "alsa_output.usb")
    assert run.calls[0][0] == ["paplay", "--device=alsa_output.usb", str(wav)]


def test_再生の失敗は終了コードだけの例外になる(tmp_path: Path) -> None:
    run = FakeRun(returncode=2, stderr=b"hidden stuff")
    with pytest.raises(RuntimeError) as info:
        PaplayPlayer(run=run).play(tmp_path / "x.wav", "")
    assert "paplay" in str(info.value) and "2" in str(info.value)
    assert "hidden" not in str(info.value)


def test_再生の時間切れは例外になる(tmp_path: Path) -> None:
    run = FakeRun(raises=subprocess.TimeoutExpired(["paplay"], 60))
    with pytest.raises(RuntimeError, match="timed out"):
        PaplayPlayer(run=run).play(tmp_path / "x.wav", "")


# ---- 時刻の同期 ----


def test_timedatectlがyesなら同期している(tmp_path: Path) -> None:
    run = FakeRun(stdout="yes\n")
    sync = TimedatectlTimeSync(run=run, marker=tmp_path / "none")
    assert sync.is_synchronized() is True
    args, kwargs = run.calls[0]
    assert args == ["timedatectl", "show", "-p", "NTPSynchronized", "--value"]
    assert kwargs["timeout"] == QUERY_TIMEOUT == 2.0


def test_timedatectlがnoなら印のファイルがあっても同期していない(tmp_path: Path) -> None:
    marker = tmp_path / "synchronized"
    marker.touch()
    sync = TimedatectlTimeSync(run=FakeRun(stdout="no\n"), marker=marker)
    assert sync.is_synchronized() is False


@pytest.mark.parametrize(
    "run",
    [
        FakeRun(returncode=1),
        FakeRun(raises=subprocess.TimeoutExpired(["timedatectl"], 2)),
        FakeRun(raises=FileNotFoundError(2, "No such file")),
        FakeRun(raises=RuntimeError("boom")),
        FakeRun(stdout="maybe\n"),
    ],
)
def test_timedatectlが使えなければ印のファイルで判定する(tmp_path: Path, run: FakeRun) -> None:
    marker = tmp_path / "synchronized"
    sync = TimedatectlTimeSync(run=run, marker=marker)
    assert sync.is_synchronized() is False
    marker.touch()
    assert sync.is_synchronized() is True


def test_印のファイルを見られなくても例外を出さない(tmp_path: Path) -> None:
    class BadPath:
        def exists(self) -> bool:
            raise PermissionError("denied")

    sync = TimedatectlTimeSync(run=FakeRun(returncode=1), marker=BadPath())  # type: ignore[arg-type]
    assert sync.is_synchronized() is False


def test_合成に失敗したら作りかけのWAVを消す(tmp_path: Path) -> None:
    out = tmp_path / "x.wav"
    out.write_bytes(b"partial")
    synth = OpenJTalkSynth(run=FakeRun(returncode=1), normalize=_no_normalize)
    with pytest.raises(RuntimeError):
        synth.synthesize("a", _settings(), out)
    assert not out.exists()
