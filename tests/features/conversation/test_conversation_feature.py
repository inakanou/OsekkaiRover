"""おしゃべりの機能（登録・待ち行列・続けて聞く・やりとりを捨てる）。

要件 1.6、2.2、6.5、7.1、9.1、9.2、10.2。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from conversation_fakes import FakeChatModel, FakeConversationIO, FlowKit

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Lease
from osekkai.core.paths import Paths
from osekkai.core.types import Priority, Resource
from osekkai.features.conversation import feature as feature_module
from osekkai.features.conversation import texts
from osekkai.features.conversation.adapters.claude import ClaudeChatModel
from osekkai.features.conversation.adapters.scripted import ScriptedChatModel
from osekkai.features.conversation.checks import CONVERSATION_CHECKS
from osekkai.features.conversation.config import ConversationConfig
from osekkai.features.conversation.feature import ConversationFeature, choose_chat_model
from osekkai.features.conversation.flow import RESOURCES, Outcome, ReplyFlow
from osekkai.features.conversation.ports import (
    ChatModelPort,
    ChatResult,
    Failed,
    FailureKind,
    Refused,
    Replied,
)
from osekkai.features.conversation.session import FORGET_AFTER_S, Session
from osekkai.voice import api
from osekkai.voice.api import Heard
from osekkai.voice.intents import Fallback, Nobody

CALL_NAME = "たろう"
ASKED = "たろうはね、ねこが すき"
NEXT = "いぬも すき"
REPLY = "{name}、ねこ かわいいね！"
FAKE_VALUE = "sk-fake1"  # 偽の鍵の値（10 文字より短く）
WAIT_S = 5.0  # 別のスレッドを待つ上限（実際の秒）
POLL_S = 0.01  # テストで待ち行列を見る間隔


@pytest.fixture(autouse=True)
def _empty_registry() -> Iterator[None]:
    api._reset_for_tests()
    yield
    api._reset_for_tests()


@pytest.fixture
def kit(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> FlowKit:
    return FlowKit(fakes, clock)


def _wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "待ちきれませんでした"
        time.sleep(0.005)


class Driver:
    """機能を別のスレッドで動かし、入口のように言葉を渡す。"""

    def __init__(
        self,
        kit: FlowKit,
        script: list[ChatResult] | None = None,
        *,
        ready: bool = True,
        delay_s: float = 0.0,
        io_factory: Callable[[Lease], FakeConversationIO] | None = None,
    ) -> None:
        self.kit = kit
        self.fake = FakeChatModel(script or [], delay_s=delay_s)
        chosen: ChatModelPort | None = self.fake if ready else None
        self.chosen_count = 0

        def choose() -> ChatModelPort | None:
            self.chosen_count += 1
            return chosen

        self.feature = ConversationFeature(
            kit.ctx,
            ConversationConfig(call_name=CALL_NAME),
            io_factory=io_factory or kit.io_factory,
            choose_model=choose,
            poll_s=POLL_S,
        )
        self.stop = threading.Event()
        self.handled = 0  # 流れが終わった（戻ったか例外を出した）回数
        flow = self.feature._flow
        original = flow.handle

        def counted(heard: Heard, session: Session | None) -> tuple[Outcome, Session | None]:
            try:
                return original(heard, session)
            finally:
                self.handled += 1

        flow.handle = counted  # type: ignore[method-assign]
        self.thread = threading.Thread(target=self.feature.run, args=(self.stop,), daemon=True)

    def start(self) -> Driver:
        self.thread.start()
        _wait_until(lambda: api.registry().owner_alive("conversation"))
        return self

    def hear(self, text: str = ASKED, *, follow_up: bool = False) -> None:
        """入口と同じに、当てはまらない言葉を受け手の口に渡す。"""
        route = api.registry().route(text)
        assert isinstance(route, Fallback)
        route.registration.handler(Heard(text, follow_up))

    def answer(self, text: str = ASKED, *, follow_up: bool = False) -> None:
        """言葉を渡し、流れが終わるまで待つ。"""
        before = self.handled
        self.hear(text, follow_up=follow_up)
        _wait_until(lambda: self.handled > before)

    def remembering(self) -> bool:
        return self.feature.remembering

    def finish(self) -> None:
        self.stop.set()
        self.thread.join(WAIT_S)
        assert not self.thread.is_alive()


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


# --- 申告 ---


def test_申告は会話の帯で声と画面とボタンを使う() -> None:
    spec = ConversationFeature.spec
    assert spec.name == "conversation"
    assert spec.priority == Priority.INTERACTIVE
    assert spec.resources == RESOURCES == {Resource.VOICE, Resource.DISPLAY, Resource.BUTTON}
    assert spec.config_model is ConversationConfig
    assert spec.checks == CONVERSATION_CHECKS


def test_おしゃべりの設定でなければ作らない(kit: FlowKit) -> None:
    from osekkai.core.config import FeatureConfig

    with pytest.raises(TypeError):
        ConversationFeature(kit.ctx, FeatureConfig())


# --- 受け手の登録と外し ---


def test_動き始めに受け手になり_止めると外れる(kit: FlowKit) -> None:
    d = Driver(kit)
    assert isinstance(api.registry().route(ASKED), Nobody)

    d.start()
    route = api.registry().route(ASKED)
    assert isinstance(route, Fallback)
    assert route.registration.owner == "conversation"

    d.finish()
    assert isinstance(api.registry().route(ASKED), Nobody)


def test_流れの例外で止まっても_終わりに受け手を外す(kit: FlowKit) -> None:
    d = Driver(kit)

    def broken(wait_s: float) -> None:
        raise RuntimeError

    d.feature.step = broken  # type: ignore[method-assign]
    errors: list[BaseException] = []

    def run() -> None:
        try:
            d.feature.run(d.stop)
        except RuntimeError as error:
            errors.append(error)

    d.thread = threading.Thread(target=run, daemon=True)
    d.thread.start()
    d.thread.join(WAIT_S)

    assert not d.thread.is_alive()
    assert len(errors) == 1  # 基盤に知らせる
    assert isinstance(api.registry().route(ASKED), Nobody)


def test_ほかの受け手がいれば記録して止める旗を待ち_その受け手を外さない(
    kit: FlowKit, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    other = api.register_fallback("other", lambda heard: None, alive=lambda: True)
    d = Driver(kit)
    d.thread.start()
    _wait_until(lambda: "ほかの受け手がいる" in _logged(caplog))

    assert d.thread.is_alive()  # 止める旗を待っている
    route = api.registry().route(ASKED)
    assert isinstance(route, Fallback)
    assert route.registration.owner == "other"

    d.finish()
    route = api.registry().route(ASKED)
    assert isinstance(route, Fallback)
    assert route.registration.owner == "other"
    other.remove()


# --- 待ち行列 ---


def test_受け手の口はすぐ戻り_答えている間の言葉は1つだけ待たせる(
    kit: FlowKit, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    d = Driver(kit, [Replied(REPLY), Replied(REPLY)], delay_s=WAIT_S).start()
    d.hear()
    assert d.fake.called.wait(WAIT_S)  # 1 つ目に答えている

    started = time.monotonic()
    d.hear(NEXT)  # 待ち行列に入る
    d.hear("みっつめ")  # いっぱいなので捨てる
    assert time.monotonic() - started < 1.0
    assert "言葉を捨てました" in _logged(caplog)

    d.fake.release()
    _wait_until(lambda: d.handled == 2)
    d.finish()
    assert len(d.fake.calls) == 2
    assert d.fake.calls[1].turns[-1].text == NEXT
    assert "みっつめ" not in _logged(caplog)


# --- 続けて聞く ---


@pytest.mark.parametrize(
    "result",
    [Replied(REPLY), Refused(), Failed(FailureKind.CONNECTION)],
    ids=["replied", "refused", "failed"],
)
def test_話し終えたら資源を返した後に続けて聞くを頼む(
    kit: FlowKit, monkeypatch: pytest.MonkeyPatch, result: ChatResult
) -> None:
    holders: list[list[bool]] = []
    original = api.listen_again

    def recording(owner: str) -> None:
        holders.append([kit.arbiter.holder(r) == "conversation" for r in RESOURCES])
        original(owner)

    monkeypatch.setattr(feature_module, "listen_again", recording)
    d = Driver(kit, [result]).start()

    d.answer()

    assert holders == [[False, False, False]]  # 返した後に頼んだ
    assert api.registry().take_follow_up_request() == "conversation"
    d.finish()


def test_鍵がなければ門の言葉を話し_続けて聞くは頼まない(kit: FlowKit) -> None:
    d = Driver(kit, ready=False).start()

    d.answer()

    assert [s.text for io in kit.ios for s in io.spoken] == [texts.GATE_REPLIES["not_set_up"]]
    assert api.registry().take_follow_up_request() is None
    d.finish()


def test_静かな状態なら答えず_続けて聞くは頼まない(kit: FlowKit) -> None:
    d = Driver(kit, [Replied(REPLY)]).start()
    kit.quiet.request_quiet()

    d.answer()

    assert d.fake.calls == []
    assert api.registry().take_follow_up_request() is None
    d.finish()


@pytest.mark.parametrize(
    ("outcome", "asked"),
    [
        (Outcome.REPLIED, True),
        (Outcome.FAILED, True),
        (Outcome.GATED, False),
        (Outcome.DROPPED, False),
    ],
)
def test_結果ごとに続けて聞くを頼むかが決まる(
    kit: FlowKit, monkeypatch: pytest.MonkeyPatch, outcome: Outcome, asked: bool
) -> None:
    def stub(self: ReplyFlow, heard: Heard, session: Session | None) -> tuple[Outcome, None]:
        return outcome, None

    monkeypatch.setattr(ReplyFlow, "handle", stub)
    d = Driver(kit).start()

    d.answer()

    expected = "conversation" if asked else None
    assert api.registry().take_follow_up_request() == expected
    d.finish()


def test_続けて聞いた言葉には前のやりとりを送る(kit: FlowKit) -> None:
    d = Driver(kit, [Replied(REPLY), Replied(REPLY)]).start()

    d.answer()
    d.answer(NEXT, follow_up=True)

    assert [t.role for t in d.fake.calls[1].turns] == ["user", "assistant", "user"]
    d.finish()


# --- やりとりを捨てる ---


def test_押されないまま130秒たつとやりとりを捨てる(
    kit: FlowKit, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    d = Driver(kit, [Replied(REPLY), Replied(REPLY)]).start()
    d.answer()
    assert d.remembering()

    kit.clock.advance(FORGET_AFTER_S - 1)
    time.sleep(POLL_S * 5)
    assert d.remembering()

    kit.clock.advance(1)
    _wait_until(lambda: not d.remembering())
    assert "やりとりを忘れました" in _logged(caplog)

    # 捨てた後の続けての言葉は新しい会話になる
    d.answer(NEXT, follow_up=True)
    assert len(d.fake.calls[1].turns) == 1
    d.finish()


# --- 思いがけない例外 ---


def test_思いがけない例外は型の名前だけを記録し_やりとりを捨てて次の言葉に答える(
    kit: FlowKit, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    calls = 0

    def flaky(lease: Lease) -> FakeConversationIO:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError(ASKED)
        return kit.io_factory(lease)

    d = Driver(kit, [Replied(REPLY), Replied(REPLY)], io_factory=flaky).start()
    d.answer()
    assert d.remembering()
    api.registry().take_follow_up_request()

    d.answer(NEXT, follow_up=True)  # 例外になる
    assert not d.remembering()
    assert api.registry().take_follow_up_request() is None
    for resource in RESOURCES:
        assert kit.arbiter.holder(resource) != "conversation"

    d.answer("さんかいめ", follow_up=True)  # 次の言葉には新しい会話で答える
    assert len(d.fake.calls) == 2
    assert len(d.fake.calls[1].turns) == 1
    assert api.registry().take_follow_up_request() == "conversation"
    d.finish()

    logged = _logged(caplog)
    assert "RuntimeError" in logged
    for word in (ASKED, NEXT, "さんかいめ", CALL_NAME):
        assert word not in logged


# --- 会話の AI の選び方 ---


def test_会話のAIは初めて使うときに1回だけ選ぶ(kit: FlowKit) -> None:
    d = Driver(kit, [Replied(REPLY), Replied(REPLY)])
    d.start()
    time.sleep(POLL_S * 5)
    assert d.chosen_count == 0

    d.answer()
    d.answer(NEXT, follow_up=True)
    assert d.chosen_count == 1
    d.finish()


def _write_env(paths: Paths, text: str) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    target = paths.config_dir / "env"
    target.write_text(text, encoding="utf-8")
    os.chmod(target, 0o600)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(env={"XDG_CONFIG_HOME": str(tmp_path / "config")}, home=tmp_path)


def _setup(monkeypatch: pytest.MonkeyPatch, *, library: bool, typed: bool) -> None:
    monkeypatch.setattr(feature_module, "anthropic_available", lambda: library)
    monkeypatch.setattr(feature_module, "typed_input_active", lambda: typed)


@pytest.mark.parametrize("typed", [True, False])
def test_鍵とライブラリがあれば_Claude_を選ぶ(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, typed: bool
) -> None:
    caplog.set_level(logging.DEBUG)
    _setup(monkeypatch, library=True, typed=typed)
    _write_env(paths, f"ANTHROPIC_API_KEY={FAKE_VALUE}\n")

    assert isinstance(choose_chat_model(paths), ClaudeChatModel)
    assert FAKE_VALUE not in _logged(caplog)


def test_鍵があってもライブラリがなく端末の偽物が動いていれば台本を選ぶ(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _setup(monkeypatch, library=False, typed=True)
    _write_env(paths, f"ANTHROPIC_API_KEY={FAKE_VALUE}\n")

    assert isinstance(choose_chat_model(paths), ScriptedChatModel)
    assert "ライブラリがない" in _logged(caplog)
    assert FAKE_VALUE not in _logged(caplog)


def test_鍵がなく端末の偽物が動いていれば台本を選ぶ(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _setup(monkeypatch, library=True, typed=True)

    assert isinstance(choose_chat_model(paths), ScriptedChatModel)
    assert "NO_FILE" in _logged(caplog)


@pytest.mark.parametrize("library", [True, False])
def test_鍵も端末の偽物もなければ_なしを選ぶ(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, library: bool
) -> None:
    caplog.set_level(logging.DEBUG)
    _setup(monkeypatch, library=library, typed=False)
    _write_env(paths, "ANTHROPIC_API_KEY=\n")

    assert choose_chat_model(paths) is None
    assert "NOT_WRITTEN" in _logged(caplog)
