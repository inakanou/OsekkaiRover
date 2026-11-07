"""実機の聞き取り（arecord と faster-whisper）のアダプタのテスト。

faster_whisper と numpy は開発 PC と CI に入れないので、偽の module に差し替える。
arecord は subprocess の Popen と run を偽物に替える。
"""

from __future__ import annotations

import importlib.util
import logging
import math
import struct
import subprocess
import sys
import tempfile
import threading
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from osekkai.voice import texts
from osekkai.voice.adapters import whisper
from osekkai.voice.adapters.whisper import (
    MODEL_FILES,
    WhisperRecognizer,
    arecord_args,
    compute_gain,
    whisper_available,
)
from osekkai.voice.config import VoiceConfig
from osekkai.voice.recognizer import ListenCancelled, RecognitionFailed, RecognizerState

MIC = "plughw:CARD=Test,DEV=0"

# --- 偽の numpy（使う所だけ） ---


class FakeArray:
    def __init__(self, values: Any) -> None:
        self.values = [float(v) for v in values]
        self.dtype = "float64"

    @property
    def size(self) -> int:
        return len(self.values)

    def astype(self, dtype: Any) -> FakeArray:
        result = FakeArray(self.values)
        result.dtype = "float32" if dtype is fake_float32 else str(dtype)
        return result

    def _same(self, values: Any) -> FakeArray:
        result = FakeArray(values)
        result.dtype = self.dtype
        return result

    def __truediv__(self, other: float) -> FakeArray:
        return self._same(v / other for v in self.values)

    def __mul__(self, other: float) -> FakeArray:
        return self._same(v * other for v in self.values)


def fake_float32(value: float) -> float:
    return float(value)


def _make_fake_numpy() -> types.ModuleType:
    module = types.ModuleType("numpy")
    module.int16 = "int16"  # type: ignore[attr-defined]
    module.float32 = fake_float32  # type: ignore[attr-defined]

    def frombuffer(raw: bytes, dtype: Any) -> FakeArray:
        assert dtype == "int16"
        result = FakeArray(struct.unpack(f"<{len(raw) // 2}h", raw))
        result.dtype = "int16"
        return result

    module.frombuffer = frombuffer  # type: ignore[attr-defined]
    module.abs = lambda a: a._same(abs(v) for v in a.values)  # type: ignore[attr-defined]
    module.max = lambda a: max(a.values)  # type: ignore[attr-defined]
    return module


# --- 偽の faster_whisper ---


@dataclass
class Segment:
    text: str


@dataclass
class FakeModelState:
    init_args: tuple[Any, ...] = ()
    init_kwargs: dict[str, Any] = field(default_factory=dict)
    transcribe_calls: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)
    segments: list[str] = field(default_factory=lambda: ["だるまさん", "しよう"])
    produced: int = 0
    init_error: BaseException | None = None
    transcribe_error: BaseException | None = None


def _make_fake_faster_whisper(state: FakeModelState) -> types.ModuleType:
    module = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if state.init_error is not None:
                raise state.init_error
            state.init_args = args
            state.init_kwargs = kwargs

        def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Iterator[Segment], object]:
            state.transcribe_calls.append((audio, kwargs))
            if state.transcribe_error is not None:
                raise state.transcribe_error

            def gen() -> Iterator[Segment]:
                for text in state.segments:
                    state.produced += 1
                    yield Segment(f" {text} ")

            return gen(), object()

    module.WhisperModel = WhisperModel  # type: ignore[attr-defined]
    return module


# --- 偽の arecord ---


class FakeStdout:
    def __init__(self, chunks: list[bytes], block: bool) -> None:
        self._chunks = list(chunks)
        self._block = block
        self.closed_event = threading.Event()
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        if self._block:
            self.closed_event.wait(5.0)
        return b""

    def close(self) -> None:
        self.closed = True
        self.closed_event.set()


class FakeProc:
    def __init__(self, args: list[str], chunks: list[bytes], returncode: int, block: bool) -> None:
        self.args = args
        self.stdout = FakeStdout(chunks, block)
        self._final = returncode
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = -15 if (self.terminated or self.killed) else self._final
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.stdout.closed_event.set()

    def kill(self) -> None:
        self.killed = True
        self.stdout.closed_event.set()


@dataclass
class FakeArecord:
    chunks: list[bytes] = field(default_factory=list)
    returncode: int = 0
    block: bool = False
    procs: list[FakeProc] = field(default_factory=list)
    popen_kwargs: list[dict[str, Any]] = field(default_factory=list)
    list_output: str = ""
    list_returncode: int = 0
    list_error: BaseException | None = None
    run_calls: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)

    def popen(self, args: list[str], **kwargs: Any) -> FakeProc:
        self.popen_kwargs.append(kwargs)
        proc = FakeProc(args, self.chunks, self.returncode, self.block)
        self.procs.append(proc)
        return proc

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.run_calls.append((args, kwargs))
        if self.list_error is not None:
            raise self.list_error
        return subprocess.CompletedProcess(args, self.list_returncode, self.list_output, "")


def _pcm(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


LIST_OUTPUT = f"null\n    Discard all samples\n{MIC}\n    USB Camera, USB Audio\n"


@pytest.fixture
def model_state(monkeypatch: pytest.MonkeyPatch) -> FakeModelState:
    state = FakeModelState()
    monkeypatch.setitem(sys.modules, "faster_whisper", _make_fake_faster_whisper(state))
    monkeypatch.setitem(sys.modules, "numpy", _make_fake_numpy())
    return state


@pytest.fixture
def arecord(monkeypatch: pytest.MonkeyPatch) -> FakeArecord:
    fake = FakeArecord(chunks=[_pcm(100, -200), _pcm(300)], list_output=LIST_OUTPUT)
    monkeypatch.setattr(subprocess, "Popen", fake.popen)
    monkeypatch.setattr(subprocess, "run", fake.run)
    return fake


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "models" / "faster-whisper-base"
    directory.mkdir(parents=True)
    for name in MODEL_FILES:
        (directory / name).write_bytes(b"x")
    return directory


def _config(**kwargs: Any) -> VoiceConfig:
    return VoiceConfig(mic_device=MIC, **kwargs)


def _ready(model_dir: Path, **kwargs: Any) -> WhisperRecognizer:
    recognizer = WhisperRecognizer(_config(**kwargs), model_dir)
    recognizer.prepare()
    assert recognizer.state is RecognizerState.READY
    return recognizer


class Recording:
    def __init__(self) -> None:
        self.events: list[bool] = []

    def __call__(self, on: bool) -> None:
        self.events.append(on)


def _close(actual: list[float], expected: list[float]) -> bool:
    # pytest.approx は読み込まれた numpy を使うので、偽の numpy の間は使わない
    return len(actual) == len(expected) and all(
        math.isclose(a, b, rel_tol=1e-6) for a, b in zip(actual, expected, strict=True)
    )


def _always() -> bool:
    return True


# --- 判定の補助 ---


def test_faster_whisper_があれば読み込めると答える(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert whisper_available() is True


def test_faster_whisper_がなければ読み込めないと答える(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def find_spec(name: str) -> None:
        seen.append(name)
        return None

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert whisper_available() is False
    assert seen == ["faster_whisper"]


def test_判定の補助は読み込みの誤りでも読み込めないと答える(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def find_spec(name: str) -> None:
        raise ValueError("broken")

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert whisper_available() is False


def test_開発の環境には_faster_whisper_がない() -> None:
    # 開発 PC と CI には入れない決まり（入っていたら、このテストで気づく）
    assert whisper_available() is False


# --- 音量 ---


def test_音量は最大値が_0_9_になるように持ち上げる() -> None:
    assert compute_gain(0.09, 20.0) == pytest.approx(10.0)


def test_音量の持ち上げは上限で止める() -> None:
    assert compute_gain(0.001, 20.0) == pytest.approx(20.0)
    assert compute_gain(0.0, 20.0) == pytest.approx(20.0)


def test_大きい声は持ち上げない向きにもする() -> None:
    assert compute_gain(1.0, 20.0) == pytest.approx(0.9)


# --- arecord の引数 ---


def test_arecord_の引数() -> None:
    assert arecord_args(MIC, 5.0) == [
        "arecord",
        "-q",
        "-D",
        MIC,
        "-f",
        "S16_LE",
        "-r",
        "16000",
        "-c",
        "1",
        "-d",
        "5",
        "-t",
        "raw",
    ]


def test_arecord_の秒は丸めて_1_秒より短くしない() -> None:
    assert arecord_args(MIC, 4.6)[11] == "5"
    # -d 0 は止まらない録音になるので、使わない
    assert arecord_args(MIC, 0.2)[11] == "1"


# --- 準備 ---


def test_はじめは準備中(model_dir: Path) -> None:
    assert WhisperRecognizer(_config(), model_dir).state is RecognizerState.PREPARING


def test_準備でモデルを決まった形で読み込む(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    _ready(model_dir)
    assert model_state.init_args == (str(model_dir),)
    assert model_state.init_kwargs == {"device": "cpu", "compute_type": "int8", "cpu_threads": 3}
    args, kwargs = arecord.run_calls[0]
    assert args == ["arecord", "-L"]
    assert 0 < kwargs["timeout"] <= 5


def test_マイクがなければ使えない(
    model_dir: Path,
    arecord: FakeArecord,
    model_state: FakeModelState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    arecord.list_output = "null\n    Discard all samples\nplughw:CARD=Other,DEV=0\n"
    recognizer = WhisperRecognizer(_config(), model_dir)
    with caplog.at_level(logging.INFO):
        recognizer.prepare()
    assert recognizer.state is RecognizerState.UNAVAILABLE
    assert model_state.init_args == ()  # モデルは読まない
    assert MIC not in caplog.text


def test_マイクの名前は説明の行では数えない(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    arecord.list_output = f"default\n    {MIC} の説明\n"
    recognizer = WhisperRecognizer(_config(), model_dir)
    recognizer.prepare()
    assert recognizer.state is RecognizerState.UNAVAILABLE


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("arecord"), subprocess.TimeoutExpired(["arecord", "-L"], 5.0)],
)
def test_マイクの確かめに失敗したら使えない(
    model_dir: Path,
    arecord: FakeArecord,
    model_state: FakeModelState,
    error: BaseException,
) -> None:
    arecord.list_error = error
    recognizer = WhisperRecognizer(_config(), model_dir)
    recognizer.prepare()
    assert recognizer.state is RecognizerState.UNAVAILABLE


def test_マイクの一覧が失敗の終わりなら使えない(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    arecord.list_returncode = 1
    recognizer = WhisperRecognizer(_config(), model_dir)
    recognizer.prepare()
    assert recognizer.state is RecognizerState.UNAVAILABLE


def test_モデルのディレクトリがなければ使えない(
    tmp_path: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    recognizer = WhisperRecognizer(_config(), tmp_path / "missing")
    recognizer.prepare()
    assert recognizer.state is RecognizerState.UNAVAILABLE
    assert model_state.init_args == ()


@pytest.mark.parametrize("name", MODEL_FILES)
def test_モデルのファイルが欠けていたら使えない(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState, name: str
) -> None:
    (model_dir / name).unlink()
    recognizer = WhisperRecognizer(_config(), model_dir)
    recognizer.prepare()
    assert recognizer.state is RecognizerState.UNAVAILABLE


def test_モデルを読み込めなければ使えず_型の名前だけを記録する(
    model_dir: Path,
    arecord: FakeArecord,
    model_state: FakeModelState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    model_state.init_error = RuntimeError("秘密の文言")
    recognizer = WhisperRecognizer(_config(), model_dir)
    with caplog.at_level(logging.INFO):
        recognizer.prepare()
    assert recognizer.state is RecognizerState.UNAVAILABLE
    assert "RuntimeError" in caplog.text
    assert "秘密の文言" not in caplog.text
    assert str(model_dir) not in caplog.text


def test_faster_whisper_を読み込めなければ使えない(
    model_dir: Path, arecord: FakeArecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    # sys.modules に None を入れると、import は ImportError になる
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    recognizer = WhisperRecognizer(_config(), model_dir)
    recognizer.prepare()
    assert recognizer.state is RecognizerState.UNAVAILABLE


def test_確かめの補助(model_dir: Path, arecord: FakeArecord, tmp_path: Path) -> None:
    assert whisper.mic_present(MIC) is True
    assert whisper.mic_present("plughw:CARD=Other,DEV=0") is False
    assert whisper.model_files_present(model_dir) is True
    assert whisper.model_files_present(tmp_path / "missing") is False


# --- 聞き取り ---


def test_録音して文字にする(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    recognizer = _ready(model_dir)
    recording = Recording()
    result = recognizer.listen(
        5.0, hints=["だるまさん", "なぞなぞ"], should_continue=_always, on_recording=recording
    )
    assert result == "だるまさんしよう"
    assert recording.events == [True, False]
    proc = arecord.procs[0]
    assert proc.args == arecord_args(MIC, 5.0)
    assert arecord.popen_kwargs[0]["stdout"] is subprocess.PIPE
    assert arecord.popen_kwargs[0]["stderr"] is subprocess.DEVNULL
    assert proc.stdout.closed


def test_transcribe_の引数とヒント(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    recognizer = _ready(model_dir)
    recognizer.listen(
        5.0, hints=["だるまさん", "なぞなぞ"], should_continue=_always, on_recording=Recording()
    )
    (_audio, kwargs) = model_state.transcribe_calls[0]
    assert kwargs == {
        "language": "ja",
        "beam_size": 1,
        "vad_filter": True,
        "condition_on_previous_text": False,
        "without_timestamps": True,
        "initial_prompt": texts.DEFAULT_HINT + " だるまさん。なぞなぞ",
    }


def test_ヒントがなければ既定のヒントだけ(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    recognizer = _ready(model_dir)
    recognizer.listen(5.0, hints=[], should_continue=_always, on_recording=Recording())
    assert model_state.transcribe_calls[0][1]["initial_prompt"] == texts.DEFAULT_HINT


def test_ヒントは長くしすぎない(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    recognizer = _ready(model_dir)
    hints = [f"ことば{i:03d}" for i in range(200)]
    recognizer.listen(5.0, hints=hints, should_continue=_always, on_recording=Recording())
    prompt = model_state.transcribe_calls[0][1]["initial_prompt"]
    assert prompt.startswith(texts.DEFAULT_HINT + " ことば000。ことば001")
    assert len(prompt) <= whisper.MAX_PROMPT_CHARS


def test_声は_float32_にして音量を上限つきで持ち上げる(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    arecord.chunks = [_pcm(16, -32)]  # 最大値は 32/32768 → 0.9 倍まではとても遠い
    recognizer = _ready(model_dir, max_gain=20.0)
    recognizer.listen(5.0, hints=[], should_continue=_always, on_recording=Recording())
    audio = model_state.transcribe_calls[0][0]
    assert audio.dtype == "float32"
    assert _close(audio.values, [16 / 32768 * 20.0, -32 / 32768 * 20.0])


def test_大きめの声は最大値が_0_9_になる(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    arecord.chunks = [_pcm(8192, -16384)]  # 最大値 0.5
    recognizer = _ready(model_dir, max_gain=20.0)
    recognizer.listen(5.0, hints=[], should_continue=_always, on_recording=Recording())
    audio = model_state.transcribe_calls[0][0]
    assert _close(audio.values, [0.45, -0.9])


def test_静かなら空の文字(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    model_state.segments = []
    recognizer = _ready(model_dir)
    result = recognizer.listen(5.0, hints=[], should_continue=_always, on_recording=Recording())
    assert result == ""


def test_録音の途中で取り消すとプロセスを止めて_ListenCancelled(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    arecord.block = True
    recognizer = _ready(model_dir)
    recording = Recording()
    polls = {"n": 0}

    def should_continue() -> bool:
        polls["n"] += 1
        return polls["n"] < 3

    with pytest.raises(ListenCancelled):
        recognizer.listen(5.0, hints=[], should_continue=should_continue, on_recording=recording)
    proc = arecord.procs[0]
    assert proc.terminated or proc.killed
    assert recording.events == [True, False]
    assert model_state.transcribe_calls == []


def test_segment_の間で取り消すと残りを回さない(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    model_state.segments = ["いち", "に", "さん"]
    recognizer = _ready(model_dir)
    recording = Recording()

    def should_continue() -> bool:
        # 文字にし始めたら（1 つ目の segment の後）取り消す
        return not model_state.transcribe_calls

    with pytest.raises(ListenCancelled):
        recognizer.listen(5.0, hints=[], should_continue=should_continue, on_recording=recording)
    assert model_state.produced == 1
    assert recording.events == [True, False]


def test_録音が失敗の終わりなら_RecognitionFailed(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    arecord.returncode = 1
    recognizer = _ready(model_dir)
    recording = Recording()
    with pytest.raises(RecognitionFailed) as info:
        recognizer.listen(5.0, hints=[], should_continue=_always, on_recording=recording)
    assert info.value.reason == whisper.MSG_RECORD_FAILED
    assert recording.events == [True, False]
    assert model_state.transcribe_calls == []


def test_録音が空なら_RecognitionFailed(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    arecord.chunks = []
    recognizer = _ready(model_dir)
    with pytest.raises(RecognitionFailed) as info:
        recognizer.listen(5.0, hints=[], should_continue=_always, on_recording=Recording())
    assert info.value.reason == whisper.MSG_RECORD_FAILED


def test_arecord_を起動できなければ_RecognitionFailed_で録音の合図はしない(
    model_dir: Path,
    arecord: FakeArecord,
    model_state: FakeModelState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recognizer = _ready(model_dir)

    def popen(args: list[str], **kwargs: Any) -> FakeProc:
        raise FileNotFoundError("arecord")

    monkeypatch.setattr(subprocess, "Popen", popen)
    recording = Recording()
    with pytest.raises(RecognitionFailed) as info:
        recognizer.listen(5.0, hints=[], should_continue=_always, on_recording=recording)
    assert info.value.reason == "FileNotFoundError"
    assert recording.events == []


def test_録音が終わらなければ止めて_RecognitionFailed(
    model_dir: Path,
    arecord: FakeArecord,
    model_state: FakeModelState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(whisper, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(whisper, "_RECORD_MARGIN_SECONDS", 0.0)
    arecord.block = True
    recognizer = _ready(model_dir)
    recording = Recording()
    with pytest.raises(RecognitionFailed) as info:
        recognizer.listen(0.05, hints=[], should_continue=_always, on_recording=recording)
    assert info.value.reason == whisper.MSG_RECORD_FAILED
    proc = arecord.procs[0]
    assert proc.terminated or proc.killed
    assert recording.events == [True, False]


def test_文字にする処理の失敗は型の名前だけの_RecognitionFailed(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    model_state.transcribe_error = RuntimeError("秘密の文言")
    recognizer = _ready(model_dir)
    with pytest.raises(RecognitionFailed) as info:
        recognizer.listen(5.0, hints=[], should_continue=_always, on_recording=Recording())
    assert info.value.reason == "RuntimeError"


def test_録音の合図は文字にする前に消す(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    recognizer = _ready(model_dir)
    events: list[str] = []
    original = sys.modules["faster_whisper"].WhisperModel.transcribe

    def transcribe(self: Any, audio: Any, **kwargs: Any) -> Any:
        events.append("transcribe")
        return original(self, audio, **kwargs)

    sys.modules["faster_whisper"].WhisperModel.transcribe = transcribe
    recognizer.listen(
        5.0,
        hints=[],
        should_continue=_always,
        on_recording=lambda on: events.append(f"recording:{on}"),
    )
    assert events == ["recording:True", "recording:False", "transcribe"]


def test_準備の前の聞き取りは_RecognitionFailed(
    model_dir: Path, arecord: FakeArecord, model_state: FakeModelState
) -> None:
    recognizer = WhisperRecognizer(_config(), model_dir)
    recording = Recording()
    with pytest.raises(RecognitionFailed):
        recognizer.listen(5.0, hints=[], should_continue=_always, on_recording=recording)
    assert arecord.procs == []
    assert recording.events == []


def test_声をファイルに書かない(
    model_dir: Path,
    arecord: FakeArecord,
    model_state: FakeModelState,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("一時ファイルを作った")

    for name in (
        "NamedTemporaryFile",
        "TemporaryFile",
        "SpooledTemporaryFile",
        "mkstemp",
        "mkdtemp",
        "TemporaryDirectory",
    ):
        monkeypatch.setattr(tempfile, name, forbidden)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    recognizer = _ready(model_dir)
    before = sorted(p.name for p in model_dir.iterdir())
    recognizer.listen(5.0, hints=[], should_continue=_always, on_recording=Recording())
    assert list(work.iterdir()) == []
    assert sorted(p.name for p in model_dir.iterdir()) == before
    assert "tempfile" not in vars(whisper)


def test_聞き取りの文字と言葉はログに出さない(
    model_dir: Path,
    arecord: FakeArecord,
    model_state: FakeModelState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        recognizer = _ready(model_dir)
        recognizer.listen(
            5.0, hints=["なぞなぞ"], should_continue=_always, on_recording=Recording()
        )
    assert "だるまさん" not in caplog.text
    assert "なぞなぞ" not in caplog.text
    assert MIC not in caplog.text
