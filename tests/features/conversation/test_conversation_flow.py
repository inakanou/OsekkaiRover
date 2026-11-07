"""1 回の返事の流れ。

要件 1.1、1.2、1.5、2.1、2.3、3.2、3.3、5.4、6.1〜6.4、6.6、7.1〜7.4、8.2、8.4、8.5。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from conversation_fakes import FakeChatModel, FakeConversationIO, FlowKit, Shown, Spoken

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import JST, FakeClock
from osekkai.core.storage import StateStore
from osekkai.core.types import Face, LeaseRevoked, Priority, Resource, SpeechFailed
from osekkai.features.conversation import texts
from osekkai.features.conversation.config import ConversationConfig
from osekkai.features.conversation.flow import (
    ACQUIRE_WAIT_S,
    RESOURCES,
    Outcome,
    ReplyFlow,
)
from osekkai.features.conversation.ports import (
    ChatResult,
    Failed,
    FailureKind,
    Refused,
    Replied,
)
from osekkai.features.conversation.privacy import NAME_MARK, NameGuard
from osekkai.features.conversation.session import FORGET_AFTER_S, Session
from osekkai.features.conversation.usage import DailyUsage
from osekkai.voice.api import Heard

CALL_NAME = "たろう"
VARIANTS = ["太郎"]
ASKED = "たろうはね、きょう こうえんに いったよ"
REPLY = "{name}、こうえん たのしかったね！"
SPOKEN_REPLY = "たろう、こうえん たのしかったね！"
WAIT_S = 5.0  # 別のスレッドの終わりを待つ上限（実際の秒）
NORMAL = Face.NORMAL.text()
HAPPY = Face.HAPPY.text()


class Harness:
    """流れと、その偽物の一式。"""

    def __init__(
        self,
        kit: FlowKit,
        script: list[ChatResult],
        *,
        config: ConversationConfig | None = None,
        delay_s: float = 0.0,
        ready: bool = True,
    ) -> None:
        self.kit = kit
        self.config = config or ConversationConfig(call_name=CALL_NAME, call_name_variants=VARIANTS)
        self.model = FakeChatModel(script, delay_s=delay_s)
        self.store = kit.ctx.store
        self.usage = DailyUsage.load(self.store, kit.clock)
        self.stop = threading.Event()
        self.flow = ReplyFlow(
            kit.ctx,
            self.config,
            (lambda: self.model) if ready else (lambda: None),
            self.usage,
            NameGuard(self.config.call_name, self.config.call_name_variants),
            self.stop.is_set,
            io_factory=kit.io_factory,
        )

    def handle(
        self, text: str = ASKED, *, follow_up: bool = False, session: Session | None = None
    ) -> tuple[Outcome, Session | None]:
        return self.flow.handle(Heard(text, follow_up), session)

    def start(
        self, text: str = ASKED, *, follow_up: bool = False, session: Session | None = None
    ) -> Callable[[], tuple[Outcome, Session | None]]:
        """別のスレッドで答えさせ、終わりを待って結果を返す口を返す。"""
        box: list[tuple[Outcome, Session | None]] = []
        thread = threading.Thread(
            target=lambda: box.append(self.handle(text, follow_up=follow_up, session=session)),
            daemon=True,
        )
        thread.start()

        def join() -> tuple[Outcome, Session | None]:
            thread.join(WAIT_S)
            assert not thread.is_alive()
            return box[0]

        return join

    def wait_called(self) -> None:
        assert self.model.called.wait(WAIT_S)

    @property
    def io(self) -> FakeConversationIO:
        return self.kit.ios[-1]

    def count(self) -> int | None:
        """保存先の今日の回数。ファイルがなければ None。"""
        path = self.store.directory / "usage.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data["count"], int)
        return data["count"]


@pytest.fixture
def kit(xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock) -> FlowKit:
    return FlowKit(fakes, clock)


def _spoken_texts(kit: FlowKit) -> list[str]:
    return [s.text for io in kit.ios for s in io.spoken]


def _assert_released(kit: FlowKit) -> None:
    for resource in RESOURCES:
        assert kit.arbiter.holder(resource) != "conversation"


# --- 表示の順と声 ---


def test_返事を待つ間はｶﾝｶﾞｴﾁｭｳ_話す前にﾛｰﾊﾞｰと喜んだ顔を出す(kit: FlowKit) -> None:
    h = Harness(kit, [Replied(REPLY)])

    outcome, _ = h.handle()

    assert outcome is Outcome.REPLIED
    assert h.io.events == [
        Shown(texts.LCD_THINKING, NORMAL),
        Shown(texts.LCD_ROBOT, HAPPY),
        Spoken(SPOKEN_REPLY, keep=False),
    ]
    _assert_released(kit)


def test_問い合わせはｶﾝｶﾞｴﾁｭｳを出した後に別のスレッドで行う(kit: FlowKit) -> None:
    h = Harness(kit, [Replied(REPLY)], delay_s=WAIT_S)
    join = h.start()
    h.wait_called()

    assert h.io.events == [Shown(texts.LCD_THINKING, NORMAL)]
    h.model.release()
    assert join()[0] is Outcome.REPLIED


def test_AIの返事は声を残さず_決まった言葉は残す(kit: FlowKit) -> None:
    h = Harness(kit, [Replied(REPLY), Refused()])

    h.handle()
    h.handle()

    assert [io.spoken[0].keep for io in kit.ios] == [False, True]


def test_借りたリースの声と画面で話し_返事の声のファイルを残さない(
    xdg_dirs: dict[str, Path], fakes: FakeDeviceSet, clock: FakeClock
) -> None:
    kit = FlowKit(fakes, clock)
    config = ConversationConfig(call_name=CALL_NAME)
    model = FakeChatModel([Replied(REPLY)])
    flow = ReplyFlow(
        kit.ctx,
        config,
        lambda: model,
        DailyUsage.load(kit.ctx.store, clock),
        NameGuard(CALL_NAME, []),
        lambda: False,
    )

    outcome, _ = flow.handle(Heard(ASKED, False), None)

    assert outcome is Outcome.REPLIED
    assert fakes.synth.lines == [SPOKEN_REPLY]
    assert list(kit.cache.root.rglob("*.wav")) == []  # 8.2


# --- 呼び名とやりとり ---


def test_送るやりとりに呼び名がなく_決まりの文と目印だけを送る(kit: FlowKit) -> None:
    h = Harness(kit, [Replied(REPLY)])

    h.handle("たろうと タロウと 太郎だよ")

    call = h.model.calls[0]
    assert call.system == texts.SYSTEM_PROMPT
    assert call.timeout_s == h.config.reply_timeout_seconds
    sent = " ".join(t.text for t in call.turns)
    for name in ("たろう", "タロウ", "太郎"):
        assert name not in sent
    assert call.turns[-1].text == f"{NAME_MARK}と {NAME_MARK}と {NAME_MARK}だよ"


def test_返事の目印は呼び名に戻して話し_やりとりには目印のまま残す(kit: FlowKit) -> None:
    h = Harness(kit, [Replied(REPLY), Replied("うん！")])

    _, session = h.handle()
    h.handle("つぎ", follow_up=True, session=session)

    assert kit.ios[0].spoken[0].text == SPOKEN_REPLY
    turns = h.model.calls[1].turns
    assert [t.role for t in turns] == ["user", "assistant", "user"]
    assert turns[1].text == REPLY
    assert CALL_NAME not in " ".join(t.text for t in turns)


def test_続けての言葉は前のやりとりを踏まえる(kit: FlowKit) -> None:
    h = Harness(kit, [Replied("いいね。"), Replied("そうなんだ。")])

    _, session = h.handle("いぬが すき")
    outcome, session = h.handle("ねこも すき", follow_up=True, session=session)

    assert outcome is Outcome.REPLIED
    assert [t.text for t in h.model.calls[1].turns] == ["いぬが すき", "いいね。", "ねこも すき"]
    assert session is not None
    assert len(session.turns_with("x")) == 5


def test_続けての言葉でない言葉は新しい会話にする(kit: FlowKit) -> None:
    h = Harness(kit, [Replied("いいね。"), Replied("そうなんだ。")])

    _, session = h.handle("いぬが すき")
    _, after = h.handle("ねこも すき", follow_up=False, session=session)

    assert [t.text for t in h.model.calls[1].turns] == ["ねこも すき"]
    assert after is not session


def test_切れたやりとりは続けての言葉でも使わない(kit: FlowKit) -> None:
    h = Harness(kit, [Replied("いいね。"), Replied("そうなんだ。")])

    _, session = h.handle("いぬが すき")
    kit.clock.advance(FORGET_AFTER_S)
    h.handle("ねこも すき", follow_up=True, session=session)

    assert [t.text for t in h.model.calls[1].turns] == ["ねこも すき"]


# --- 失敗・時間切れ・拒否・空 ---


@pytest.mark.parametrize(
    ("kind", "phrase"),
    [
        (FailureKind.CONNECTION, texts.SAY_CONNECTION),
        (FailureKind.BUSY, texts.SAY_BUSY),
        (FailureKind.OTHER, texts.SAY_OTHER),
        (FailureKind.TIMEOUT, texts.SAY_OTHER),
    ],
)
def test_失敗の種類ごとの言葉を普通の顔で話し_数えずやりとりに残さない(
    kit: FlowKit, kind: FailureKind, phrase: str
) -> None:
    h = Harness(kit, [Replied("いいね。"), Failed(kind), Replied("うん。")])
    _, session = h.handle("いぬが すき")

    outcome, after = h.handle("ねこも すき", follow_up=True, session=session)

    assert outcome is Outcome.FAILED
    assert kit.ios[1].events[1:] == [Shown(texts.LCD_ROBOT, NORMAL), Spoken(phrase, keep=True)]
    assert h.count() == 1  # 最初の返事の分だけ
    assert after is session
    h.handle("もういちど", follow_up=True, session=after)
    assert [t.text for t in h.model.calls[2].turns] == ["いぬが すき", "いいね。", "もういちど"]


def test_失敗を話し終えたら時刻を進め_続けて聞く間にやりとりが切れない(kit: FlowKit) -> None:
    h = Harness(kit, [Replied("いいね。"), Failed(FailureKind.BUSY), Replied("うん。")])
    _, session = h.handle("いぬが すき")
    kit.clock.advance(FORGET_AFTER_S - 10)
    _, session = h.handle("ねこも すき", follow_up=True, session=session)
    kit.clock.advance(FORGET_AFTER_S - 10)

    h.handle("もういちど", follow_up=True, session=session)

    assert len(h.model.calls[2].turns) == 3


def test_返事を待つ時間を過ぎたら時間切れの言葉を話し_遅れた返事は捨てる(kit: FlowKit) -> None:
    h = Harness(kit, [Replied("おそい へんじ")], delay_s=WAIT_S)
    join = h.start()
    h.wait_called()

    kit.clock.advance(h.config.reply_timeout_seconds)
    outcome, _ = join()
    h.model.release()

    assert outcome is Outcome.FAILED
    assert _spoken_texts(kit) == [texts.SAY_OTHER]
    assert h.count() is None


def test_締め切りの前に届いた返事は話す(kit: FlowKit) -> None:
    h = Harness(kit, [Replied(REPLY)], delay_s=WAIT_S)
    join = h.start()
    h.wait_called()

    kit.clock.advance(h.config.reply_timeout_seconds - 1)
    h.model.release()

    assert join()[0] is Outcome.REPLIED
    assert _spoken_texts(kit) == [SPOKEN_REPLY]


def test_拒否は決まった言葉を話して数え_やりとりには残さない(kit: FlowKit) -> None:
    h = Harness(kit, [Replied("いいね。"), Refused(), Replied("うん。")])
    _, session = h.handle("いぬが すき")

    outcome, after = h.handle("こわい はなし", follow_up=True, session=session)

    assert outcome is Outcome.REPLIED
    assert kit.ios[1].spoken == [Spoken(texts.SAY_REFUSED, keep=True)]
    assert h.count() == 2
    h.handle("もういちど", follow_up=True, session=after)
    assert [t.text for t in h.model.calls[2].turns] == ["いぬが すき", "いいね。", "もういちど"]


@pytest.mark.parametrize("reply", ["", "   ", "。", "🎉✨", "- *"])
def test_文字の残らない返事は空の言葉を話して数え_やりとりには残さない(
    kit: FlowKit, reply: str
) -> None:
    h = Harness(kit, [Replied(reply), Replied("うん。")])

    outcome, session = h.handle("いぬが すき")

    assert outcome is Outcome.REPLIED
    assert kit.ios[0].spoken == [Spoken(texts.SAY_EMPTY, keep=True)]
    assert h.count() == 1
    h.handle("もういちど", follow_up=True, session=session)
    assert [t.text for t in h.model.calls[1].turns] == ["もういちど"]


def test_長い返事は整えてから話す(kit: FlowKit) -> None:
    h = Harness(kit, [Replied("ひとつめ。ふたつめ。みっつめ。")])

    h.handle()

    assert _spoken_texts(kit) == ["ひとつめ。ふたつめ。"]


def test_話した返事だけを1回ずつ数える(kit: FlowKit) -> None:
    h = Harness(kit, [Replied("いいね。"), Replied("うん。"), Failed(FailureKind.CONNECTION)])

    h.handle()
    h.handle()
    h.handle()

    assert h.count() == 2


# --- 門 ---


def test_鍵がなければ決まった言葉を話して会話を捨てる(kit: FlowKit) -> None:
    h = Harness(kit, [], ready=False)

    outcome, session = h.handle(session=Session(10, kit.clock.monotonic()), follow_up=True)

    assert (outcome, session) == (Outcome.GATED, None)
    assert h.io.events == [
        Shown(texts.LCD_ROBOT, NORMAL),
        Spoken(texts.SAY_UNAVAILABLE, keep=True),
    ]
    _assert_released(kit)


def test_時間帯の外なら送らずに寝る時間の言葉を話す(kit: FlowKit) -> None:
    kit.clock.set_now(datetime(2026, 1, 1, 21, 30, tzinfo=JST))
    h = Harness(kit, [Replied(REPLY)])

    outcome, session = h.handle()

    assert (outcome, session) == (Outcome.GATED, None)
    assert h.model.calls == []
    assert _spoken_texts(kit) == [texts.SAY_SLEEP_TIME]
    assert h.count() is None


def test_上限に達したら送らずに上限の言葉を話す(kit: FlowKit) -> None:
    config = ConversationConfig(call_name=CALL_NAME, daily_limit=1)
    h = Harness(kit, [Replied("いいね。"), Replied(REPLY)], config=config)
    h.handle()

    outcome, _ = h.handle()

    assert outcome is Outcome.GATED
    assert len(h.model.calls) == 1
    assert _spoken_texts(kit)[-1] == texts.SAY_LIMIT
    assert h.count() == 1


def test_時刻が確かでない間は回数の記録を読まずに決まった言葉を話す(kit: FlowKit) -> None:
    store: StateStore = kit.ctx.store
    store.directory.mkdir(parents=True, exist_ok=True)
    path = store.directory / "usage.json"
    old = json.dumps({"version": 1, "date": "2025-12-31", "count": 5, "blocked": False})
    path.write_text(old, encoding="utf-8")
    kit.clock.set_synchronized(False)
    h = Harness(kit, [Replied(REPLY)])

    outcome, _ = h.handle()

    assert outcome is Outcome.GATED
    assert _spoken_texts(kit) == [texts.SAY_UNAVAILABLE]
    assert h.model.calls == []
    assert path.read_text(encoding="utf-8") == old  # 読めなかった日として書き直していない


def test_時刻が確かでない間は壊れた記録も読まない(kit: FlowKit) -> None:
    store: StateStore = kit.ctx.store
    store.directory.mkdir(parents=True, exist_ok=True)
    path = store.directory / "usage.json"
    path.write_text("{こわれた", encoding="utf-8")
    kit.clock.set_synchronized(False)
    h = Harness(kit, [Replied(REPLY)])

    h.handle()

    assert path.read_text(encoding="utf-8") == "{こわれた"  # 退避も書き直しもしていない


# --- 静かな状態・借りられない・中断・止める旗 ---


def test_静かな状態なら問い合わせず声も画面も使わない(kit: FlowKit) -> None:
    kit.quiet.request_quiet()
    h = Harness(kit, [Replied(REPLY)])

    outcome, session = h.handle()

    assert (outcome, session) == (Outcome.DROPPED, None)
    assert kit.ios == []
    assert kit.leases == []
    assert h.model.calls == []


def test_借りた後に静かな状態なら答えない(kit: FlowKit) -> None:
    h = Harness(kit, [Replied(REPLY)])
    kit.ctx.after_acquire = kit.quiet.request_quiet

    outcome, _ = h.handle()

    assert outcome is Outcome.DROPPED
    assert h.model.calls == []
    assert kit.ios == []
    _assert_released(kit)


def test_返事を待つ間に静かな状態になったら話さず数えない(kit: FlowKit) -> None:
    h = Harness(kit, [Replied(REPLY)], delay_s=WAIT_S)
    join = h.start()
    h.wait_called()

    kit.quiet.request_quiet()
    h.model.release()
    outcome, session = join()

    assert (outcome, session) == (Outcome.DROPPED, None)
    assert h.io.events == [Shown(texts.LCD_THINKING, NORMAL)]
    assert h.count() is None
    _assert_released(kit)


def test_声と画面を借りられなければ話さず数えない(kit: FlowKit) -> None:
    held = kit.arbiter.acquire("routine", Priority.SCHEDULED, {Resource.VOICE})
    h = Harness(kit, [Replied(REPLY)])

    outcome, session = h.handle()

    held.release()
    assert (outcome, session) == (Outcome.DROPPED, None)
    assert kit.ios == []
    assert h.model.calls == []
    assert h.count() is None
    assert ACQUIRE_WAIT_S == 2.0


def test_返事を待つ間に中断されたら話さずに資源を返す(kit: FlowKit) -> None:
    h = Harness(kit, [Replied(REPLY)], delay_s=WAIT_S)
    join = h.start()
    h.wait_called()

    # 日課が声と画面を求める（中断の知らせ → 返されるまで待つ）
    taken = kit.arbiter.acquire("routine", Priority.SCHEDULED, {Resource.VOICE, Resource.DISPLAY})
    outcome, session = join()
    h.model.release()

    assert (outcome, session) == (Outcome.DROPPED, None)
    assert taken.active
    assert _spoken_texts(kit) == []
    assert h.count() is None
    taken.release()


def test_話している途中で中断されたら言い終えて1回数え_やりとりには残さない(kit: FlowKit) -> None:
    h = Harness(kit, [Replied(REPLY)])

    def preempt(_text: str) -> None:
        lease = kit.leases[-1]
        threading.Thread(
            target=lambda: kit.arbiter.acquire(
                "routine", Priority.SCHEDULED, {Resource.VOICE}
            ).release(),
            daemon=True,
        ).start()
        assert lease.preempt_requested.wait(WAIT_S)

    kit.prepare = lambda io: setattr(io, "before_say", preempt)

    outcome, session = h.handle()

    assert (outcome, session) == (Outcome.DROPPED, None)
    assert _spoken_texts(kit) == [SPOKEN_REPLY]  # 言い終えた
    assert h.count() == 1  # 話した返事は数える（5.4）


@pytest.mark.parametrize("result", [Replied(REPLY), Refused()])
def test_話している間に静かな状態になっても話した返事は1回数え_やりとりには残さない(
    kit: FlowKit, result: ChatResult
) -> None:
    h = Harness(kit, [result])
    kit.prepare = lambda io: setattr(io, "before_say", lambda _t: kit.quiet.request_quiet())

    outcome, session = h.handle()

    assert (outcome, session) == (Outcome.DROPPED, None)
    assert len(_spoken_texts(kit)) == 1
    assert h.count() == 1


def test_話している間に止める旗が立っても話した返事は1回数え_やりとりには残さない(
    kit: FlowKit,
) -> None:
    h = Harness(kit, [Replied(REPLY)])
    kit.prepare = lambda io: setattr(io, "before_say", lambda _t: h.stop.set())

    outcome, session = h.handle()

    assert (outcome, session) == (Outcome.DROPPED, None)
    assert _spoken_texts(kit) == [SPOKEN_REPLY]
    assert h.count() == 1


def test_話している間に止まっても失敗の言葉は数えない(kit: FlowKit) -> None:
    h = Harness(kit, [Failed(FailureKind.CONNECTION)])
    kit.prepare = lambda io: setattr(io, "before_say", lambda _t: h.stop.set())

    outcome, session = h.handle()

    assert (outcome, session) == (Outcome.DROPPED, None)
    assert _spoken_texts(kit) == [texts.SAY_CONNECTION]
    assert h.count() is None


@pytest.mark.parametrize("error", [SpeechFailed("x"), LeaseRevoked("x")])
def test_話せなかったら数えず会話を捨てる(kit: FlowKit, error: Exception) -> None:
    h = Harness(kit, [Replied(REPLY)])
    kit.prepare = lambda io: setattr(io, "say_error", error)

    outcome, session = h.handle()

    assert (outcome, session) == (Outcome.DROPPED, None)
    assert h.count() is None
    _assert_released(kit)


def test_止める旗が立ったら届いた返事を話さない(kit: FlowKit) -> None:
    h = Harness(kit, [Replied(REPLY)], delay_s=WAIT_S)
    join = h.start()
    h.wait_called()

    h.stop.set()
    h.model.release()
    outcome, _ = join()

    assert outcome is Outcome.DROPPED
    assert _spoken_texts(kit) == []
    assert h.count() is None


# --- 子どもの言葉とログ ---


@pytest.mark.parametrize(
    "result",
    [
        Replied(""),
        Refused(),
        Failed(FailureKind.CONNECTION),
        Failed(FailureKind.OTHER),
        Replied("うん、いいね。"),
    ],
)
def test_子どもの言葉をそのまま話さない(kit: FlowKit, result: ChatResult) -> None:
    child = "ぼくの ひみつの ことば"
    h = Harness(kit, [result])

    h.handle(child)

    assert _spoken_texts(kit)
    for text in _spoken_texts(kit):
        assert child not in text


def test_ログには子どもの言葉も返事も呼び名も出さない(
    kit: FlowKit, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    h = Harness(kit, [Replied(REPLY), Refused(), Failed(FailureKind.BUSY)])

    for _ in range(3):
        h.handle()
    kit.clock.set_synchronized(False)
    h.handle()

    logged = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (ASKED, REPLY, SPOKEN_REPLY, CALL_NAME, "こうえん"):
        assert secret not in logged
    assert "今日 1 回目" in logged
    assert "BUSY" in logged
    assert "CLOCK_UNSYNCED" in logged
