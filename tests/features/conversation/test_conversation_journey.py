"""おしゃべりを、受け取りから上限まで通して確かめる。

要件 2.1、2.2、2.3、5.1、5.2、5.3、5.5、8.1、8.4、10.1、10.2。

偽の時計・偽の会話の AI と、本物の基盤の部品（調停役・静かな状態・保存先）で、
本物の ConversationFeature を別のスレッドで動かし、入口と同じに受け手の口へ言葉を渡す。

1. 話しかける → 返事 → 続けて話す（前のやりとりが送られる）→ 押さずに 130 秒 → 新しい会話
2. 60 回話した後は上限の言葉。動かし直しても上限。日付が変わると話せる。
   時間帯の外と、時刻が確かでないときの言葉
3. 端末の偽物が動いていて鍵がないとき、台本の返事が端末の声に出る

どの流れでも、ログ（DEBUG まで）・self-check の結果・保存先のファイルに、
子どもの言葉・返事・呼び名・偽の鍵が出ないことを確かめる（8.1、8.4）。
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from conversation_fakes import FakeChatModel, FlowKit

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import JST, FakeClock
from osekkai.adapters.fake.console import _reset_typed_input_for_tests, build_console_set
from osekkai.core.config import ConfigLoader
from osekkai.core.paths import Paths
from osekkai.core.selfcheck import CheckContext, SelfCheckRunner, render_lines
from osekkai.features.conversation import checks as checks_module
from osekkai.features.conversation import texts
from osekkai.features.conversation.adapters.scripted import SCRIPT
from osekkai.features.conversation.config import ConversationConfig
from osekkai.features.conversation.envfile import FILE_NAME, VARIABLE_NAME
from osekkai.features.conversation.feature import ConversationFeature
from osekkai.features.conversation.flow import LeaseConversationIO, Outcome
from osekkai.features.conversation.ports import ChatModelPort, ChatResult, Replied
from osekkai.features.conversation.privacy import NAME_MARK
from osekkai.features.conversation.session import FORGET_AFTER_S, Session
from osekkai.voice import api
from osekkai.voice.api import Heard
from osekkai.voice.intents import Fallback

# 外に出てはいけない、目立つ言葉（呼び名・子どもの言葉・返事・偽の鍵）
CALL_NAME = "ぽぽまる"
ASK_1 = "ぽぽまるはね、くじらのるるが すき"
ASK_2 = "ぺんぎんのぴぴも すき"
ASK_3 = "あざらしのむむは どこ"
REPLY_1 = f"{NAME_MARK}、くじらのるる かわいいね"
REPLY_2 = "ぺんぎんのぴぴ すてきだね"
REPLY_3 = "あざらしのむむは うみに いるよ"
FAKE_VALUE = "qqfake7"  # 偽の鍵の値（10 文字より短く）
MARKERS = (
    CALL_NAME,
    "くじらのるる",
    "ぺんぎんのぴぴ",
    "あざらしのむむ",
    "かわいいね",
    "すてきだね",
    "うみに いるよ",
    FAKE_VALUE,
)
SCRIPT_MARKERS = tuple(line.replace(NAME_MARK, "") for line in SCRIPT)

MORNING = datetime(2026, 3, 2, 9, 0, tzinfo=JST)
NEXT_MORNING = datetime(2026, 3, 3, 9, 0, tzinfo=JST)
NIGHT = datetime(2026, 3, 3, 22, 0, tzinfo=JST)
WAIT_S = 5.0  # 別のスレッドを待つ上限（実際の秒）
POLL_S = 0.005  # 機能が待ち行列を見る間隔（実際の秒）
LIMIT = 60


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    api._reset_for_tests()
    _reset_typed_input_for_tests()
    yield
    api._reset_for_tests()
    _reset_typed_input_for_tests()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(MORNING)


@pytest.fixture
def kit(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> FlowKit:
    return FlowKit(fakes, clock)


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def _wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "待ちきれませんでした"
        time.sleep(0.002)


class Driver:
    """本物の機能を別のスレッドで動かし、入口のように言葉を渡す。"""

    def __init__(
        self,
        kit: FlowKit,
        *,
        model: ChatModelPort | None = None,
        use_io_factory: bool = True,
        choose_default: bool = False,
    ) -> None:
        self.kit = kit
        # 既定の口は、借りた本物の資源の声と画面・既定の会話の AI の選び方
        self.feature = ConversationFeature(
            kit.ctx,
            ConversationConfig(call_name=CALL_NAME),
            io_factory=kit.io_factory if use_io_factory else LeaseConversationIO,
            choose_model=None if choose_default else (lambda: model),
            poll_s=POLL_S,
        )
        self.outcomes: list[Outcome] = []
        flow = self.feature._flow
        original = flow.handle

        def counted(heard: Heard, session: Session | None) -> tuple[Outcome, Session | None]:
            outcome, after = original(heard, session)
            self.outcomes.append(outcome)
            return outcome, after

        flow.handle = counted  # type: ignore[method-assign]
        # 答える 1 歩（やりとりを持ち替え、続けて聞くを頼む）が終わった回数
        self.answered = 0
        self._follow_up = False
        answer_step = self.feature._answer

        def answered(heard: Heard) -> None:
            try:
                answer_step(heard)
            finally:
                self.answered += 1

        self.feature._answer = answered  # type: ignore[method-assign]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.feature.run, args=(self.stop,), daemon=True)
        self.thread.start()
        _wait_until(lambda: api.registry().owner_alive("conversation"))

    def answer(self, text: str, *, follow_up: bool = False) -> Outcome:
        """入口と同じに当てはまらない言葉を渡し、答える 1 歩が終わるまで待って結果を返す。

        続けて聞くの頼みはこの歩のものを読んで消す（次の言葉に持ち越さない）。
        """
        assert api.registry().take_follow_up_request() is None  # 前の歩の頼みは残っていない
        before = self.answered
        outcomes_before = len(self.outcomes)
        route = api.registry().route(text)
        assert isinstance(route, Fallback)
        route.registration.handler(Heard(text, follow_up))
        _wait_until(lambda: self.answered > before)
        assert len(self.outcomes) == outcomes_before + 1
        self._follow_up = api.registry().take_follow_up_request() == "conversation"
        return self.outcomes[-1]

    def follow_up_asked(self) -> bool:
        """最後の歩で続けて聞くを頼んだか。"""
        return self._follow_up

    def finish(self) -> None:
        self.stop.set()
        self.thread.join(WAIT_S)
        assert not self.thread.is_alive()


def _spoken(kit: FlowKit) -> list[str]:
    return [s.text for io_ in kit.ios for s in io_.spoken]


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def _files_text(root: Path) -> str:
    """root の下のファイルの名前と中身を 1 つの文字列にする（読めない文字は飛ばす）。"""
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        parts.append(str(path.relative_to(root)))
        if path.is_file():
            parts.append(path.read_bytes().decode("utf-8", errors="ignore"))
    return "\n".join(parts)


def _write_key(paths: Paths) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    target = paths.config_dir / FILE_NAME
    target.write_text(f"{VARIABLE_NAME}={FAKE_VALUE}\n", encoding="utf-8")
    os.chmod(target, 0o600)


def _self_check_text(paths: Paths) -> str:
    """設定ファイルと鍵のファイルを使って、おしゃべりの self-check を動かした結果の文字。"""
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(f"conversation:\n  call_name: {CALL_NAME}\n", encoding="utf-8")
    report = ConfigLoader(paths, {"conversation": ConversationConfig}).load(create_missing=False)
    ctx = CheckContext(paths=paths, report=report, service=None, hardware_allowed=True)
    results = SelfCheckRunner().run(checks_module.CONVERSATION_CHECKS, ctx)
    assert [name for name, _ in results] == ["conversation.setup"]  # 鍵を読む項目が動いた
    return "\n".join([*render_lines(results), repr(results)])


def _assert_private(texts_to_check: list[str], markers: tuple[str, ...] = MARKERS) -> None:
    for text in texts_to_check:
        for marker in markers:
            assert marker not in text


# --- 1. 続けて話す・130 秒で忘れる ---


def test_話しかけて続けて話し_130秒押さなければ次は新しい会話になる(
    kit: FlowKit,
    xdg_dirs: dict[str, Path],
    logs: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeChatModel([Replied(REPLY_1), Replied(REPLY_2), Replied(REPLY_3)])
    d = Driver(kit, model=fake)

    # 話しかける → 返事（呼び名を入れて話す）→ 続けて聞くを頼む
    assert d.answer(ASK_1) is Outcome.REPLIED
    assert d.follow_up_asked()
    assert d.feature.remembering
    first = fake.calls[0].turns
    assert [(t.role, t.text) for t in first] == [("user", f"{NAME_MARK}はね、くじらのるるが すき")]
    assert _spoken(kit)[-1] == f"{CALL_NAME}、くじらのるる かわいいね"

    # 続けて話す → 前のやりとりが送られる（呼び名は目印のまま）
    assert d.answer(ASK_2, follow_up=True) is Outcome.REPLIED
    assert d.follow_up_asked()
    second = fake.calls[1].turns
    assert [(t.role, t.text) for t in second] == [
        ("user", f"{NAME_MARK}はね、くじらのるるが すき"),
        ("assistant", REPLY_1),
        ("user", ASK_2),
    ]
    assert _spoken(kit)[-1] == REPLY_2

    # 押さずに 130 秒 → やりとりを忘れる
    kit.clock.advance(FORGET_AFTER_S)
    _wait_until(lambda: not d.feature.remembering)

    # 次は（続けての言葉でも）新しい会話
    assert d.answer(ASK_3, follow_up=True) is Outcome.REPLIED
    assert d.follow_up_asked()
    assert d.feature.remembering
    assert [t.text for t in fake.calls[2].turns] == [ASK_3]
    d.finish()

    for call in fake.calls:  # 外部に呼び名を送らない
        assert all(CALL_NAME not in t.text for t in call.turns)
        assert CALL_NAME not in call.system

    _write_key(kit.paths)
    monkeypatch.setattr(checks_module, "anthropic_available", lambda: True)
    _assert_private([_logged(logs), _self_check_text(kit.paths)])
    _assert_private([_files_text(xdg_dirs["XDG_STATE_HOME"])])


# --- 2. 上限・動かし直し・日付・時間帯・時刻 ---


def test_60回話すと上限の言葉で_動かし直しても上限で_日付が変わると話せる(
    kit: FlowKit,
    xdg_dirs: dict[str, Path],
    logs: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script: list[ChatResult] = [Replied(REPLY_2) for _ in range(LIMIT + 1)]
    fake = FakeChatModel(script)
    d = Driver(kit, model=fake)
    for _ in range(LIMIT):
        assert d.answer(ASK_1) is Outcome.REPLIED
        assert d.follow_up_asked()
    assert len(fake.calls) == LIMIT

    # 61 回目は上限の言葉で、外には送らず、続けて聞くも頼まない（5.3）
    assert d.answer(ASK_2) is Outcome.GATED
    assert _spoken(kit)[-1] == texts.SAY_LIMIT
    assert not d.follow_up_asked()
    assert len(fake.calls) == LIMIT
    d.finish()

    # 動かし直す（新しい機能と記録の口が、同じ保存先を読む）→ まだ上限
    restarted = Driver(kit, model=fake)
    assert restarted.answer(ASK_2) is Outcome.GATED
    assert not restarted.follow_up_asked()
    assert _spoken(kit)[-1] == texts.SAY_LIMIT
    assert len(fake.calls) == LIMIT

    # 日付が変わると話せる（5.5）
    kit.clock.set_now(NEXT_MORNING)
    assert restarted.answer(ASK_2) is Outcome.REPLIED
    assert restarted.follow_up_asked()
    assert _spoken(kit)[-1] == REPLY_2
    assert len(fake.calls) == LIMIT + 1

    # 時間帯の外（5.1）
    kit.clock.set_now(NIGHT)
    assert restarted.answer(ASK_3) is Outcome.GATED
    assert not restarted.follow_up_asked()
    assert _spoken(kit)[-1] == texts.SAY_SLEEP_TIME

    # 時刻が確かでない（5.2）
    kit.clock.set_now(NEXT_MORNING)
    kit.clock.set_synchronized(False)
    assert restarted.answer(ASK_3) is Outcome.GATED
    assert not restarted.follow_up_asked()
    assert _spoken(kit)[-1] == texts.SAY_UNAVAILABLE
    assert len(fake.calls) == LIMIT + 1  # 門で止めた言葉は外に送らない
    restarted.finish()

    usage = json.loads(next(xdg_dirs["XDG_STATE_HOME"].rglob("usage.json")).read_text("utf-8"))
    assert usage["date"] == NEXT_MORNING.date().isoformat()
    assert usage["count"] == 1

    _write_key(kit.paths)
    monkeypatch.setattr(checks_module, "anthropic_available", lambda: True)
    _assert_private([_logged(logs), _self_check_text(kit.paths)])
    _assert_private([_files_text(xdg_dirs["XDG_STATE_HOME"])])


# --- 3. 端末の偽物と台本の返事 ---


def test_端末の偽物が動いていて鍵がなければ_台本の返事が端末の声に出る(
    xdg_dirs: dict[str, Path],
    clock: FakeClock,
    logs: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    read_end, write_end = os.pipe()
    inp = os.fdopen(read_end, "r", encoding="utf-8")
    console = build_console_set(clock, out=out, inp=inp)
    kit = FlowKit(console, clock)
    kit.router.start()  # 端末の偽物が行を読み始める（typed_input_active が立つ）
    try:
        # 会話の AI は既定の選び方（鍵のファイルはない）。声と画面は借りた本物の資源
        d = Driver(kit, use_io_factory=False, choose_default=True)
        assert d.answer(ASK_1) is Outcome.REPLIED
        assert d.follow_up_asked()
        assert d.answer(ASK_2, follow_up=True) is Outcome.REPLIED
        assert d.follow_up_asked()
        d.finish()
    finally:
        os.close(write_end)  # 入力の終わりで読み手が終わる
        kit.router.close()
        inp.close()

    voiced = [line for line in out.getvalue().splitlines() if line.startswith("[声] ")]
    said = "".join(line.removeprefix("[声] ") for line in voiced)
    first, second = SCRIPT[0], SCRIPT[1]
    for piece in first.replace(NAME_MARK, CALL_NAME).split("。"):
        assert piece.strip() in said
    for piece in second.split("。"):
        assert piece.strip() in said

    logged = _logged(logs)
    assert "台本の返事を使います" in logged
    # 流れの後に鍵を置いて self-check も見る（流れの間は鍵なし）
    _write_key(kit.paths)
    monkeypatch.setattr(checks_module, "anthropic_available", lambda: True)
    private = (*MARKERS, *SCRIPT_MARKERS)
    _assert_private([logged, _self_check_text(kit.paths)], private)
    _assert_private([_files_text(xdg_dirs["XDG_STATE_HOME"])], private)
