"""日課の設定の形と検証（1.1〜1.4、1.7〜1.9、8.6）。"""

from datetime import date, time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from routine_fixtures import (
    build_config,
    load_report,
    routine_section,
    routine_yaml,
    task_dict,
)

from osekkai.core.config import ConfigReport
from osekkai.core.paths import Paths
from osekkai.features.routine.config import RoutineConfig, TaskConfig

# 誤りの文言に出てはいけない、目立つ値
HIDDEN_NAME = "ひみつのよびな"
HIDDEN_ID = "zq_hidden_task"
HIDDEN_PHRASE = "ひみつの ことば"
HIDDEN_TIME = "05:43"


def _load(xdg_dirs: dict[str, Path], section: dict[str, Any]) -> ConfigReport:
    return load_report(Paths(), routine_yaml(section))


def _errors(report: ConfigReport) -> list[str]:
    return list(report.feature_errors.get("routine", []))


def _all_texts(report: ConfigReport, caplog: pytest.LogCaptureFixture) -> str:
    parts = [*report.fatal_errors, *report.warnings, *_errors(report)]
    parts.extend(r.getMessage() for r in caplog.records)
    return "\n".join(parts)


def _morning_and_sleep(**sleep_fields: Any) -> list[dict[str, Any]]:
    return [
        task_dict("wash", at="07:00"),
        task_dict("sleep", at="20:30", sleep=True, **sleep_fields),
    ]


# --- 正しい設定と既定値 ---


def test_節がなければ既定値で読める(xdg_dirs: dict[str, Path]) -> None:
    report = load_report(Paths(), "core: {}\n")
    assert _errors(report) == []
    config = report.features["routine"]
    assert isinstance(config, RoutineConfig)
    assert config.enabled is True
    assert config.call_name == ""
    assert config.extra_holidays == []
    assert config.wake.weekday is None
    assert config.wake.holiday is None
    assert config.keep_days == 90
    assert config.tasks == []


def test_正しい設定は既定値つきで読める(xdg_dirs: dict[str, Path]) -> None:
    section = routine_section(
        [
            task_dict("wash", at="07:00", days="weekday", notice="もうすぐ かおを あらうよ"),
            task_dict("teeth", at=None, after="wash", ready="はみがきの じゅんびを しよう"),
            task_dict("sleep", at="20:30", sleep=True, firm=True),
        ],
        call_name="テストくん",
        extra_holidays=["2026-05-01"],
        wake={"weekday": "06:30", "holiday": "07:30"},
        keep_days=30,
    )
    report = _load(xdg_dirs, section)
    assert _errors(report) == []
    assert report.warnings == []
    config = report.features["routine"]
    assert isinstance(config, RoutineConfig)
    assert config.call_name == "テストくん"
    assert config.extra_holidays == [date(2026, 5, 1)]
    assert config.wake.weekday == time(6, 30)
    assert config.wake.holiday == time(7, 30)
    assert config.keep_days == 30
    wash, teeth, sleep = config.tasks
    assert (wash.id, wash.at, wash.after, wash.days) == ("wash", time(7, 0), None, "weekday")
    assert wash.notice == "もうすぐ かおを あらうよ"
    assert (teeth.at, teeth.after, teeth.days) == (None, "wash", "everyday")
    assert teeth.ready == "はみがきの じゅんびを しよう"
    assert sleep.sleep is True
    assert sleep.firm is True


def test_省略した項目は既定値になる() -> None:
    task = TaskConfig.model_validate(task_dict())
    assert task.days == "everyday"
    assert task.notice is None
    assert task.notice_min == 10
    assert task.ready is None
    assert task.remind_min == 5
    assert task.max_reminders == 2
    assert task.firm is False
    assert task.sleep is False


def test_引用符のない時刻も時と分として読む(xdg_dirs: dict[str, Path]) -> None:
    # YAML 1.1 は 21:00 を 60 進の数（1260）として読むので、時と分に戻す
    text = (
        "routine:\n"
        "  wake:\n"
        "    weekday: 6:30\n"
        "    holiday: 07:30\n"
        "  tasks:\n"
        "    - id: wash\n"
        "      at: 7:05\n"
        "      call: a\n"
        "      praise: b\n"
        "      lcd: c\n"
        "    - id: sleep\n"
        "      at: 21:00\n"
        "      sleep: true\n"
        "      call: a\n"
        "      praise: b\n"
        "      lcd: c\n"
    )
    report = load_report(Paths(), text)
    assert _errors(report) == []
    config = report.features["routine"]
    assert isinstance(config, RoutineConfig)
    assert config.wake.weekday == time(6, 30)
    assert config.wake.holiday == time(7, 30)
    assert [t.at for t in config.tasks] == [time(7, 5), time(21, 0)]


def test_設定のモデルを直接作る補助で読める() -> None:
    config = build_config([task_dict("wash")], call_name="テストくん")
    assert config.tasks[0].id == "wash"
    assert config.call_name == "テストくん"


# --- 誤り（場所と理由。値を入れない） ---


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        pytest.param(
            routine_section([task_dict(HIDDEN_ID), task_dict(HIDDEN_ID, at="08:00")]),
            "tasks.1.id: ",
            id="idの重複",
        ),
        pytest.param(
            routine_section([task_dict("a"), task_dict("b", at="08:00", after="a")]),
            "tasks.1: ",
            id="atとafterの両方",
        ),
        pytest.param(
            routine_section([task_dict("a"), task_dict("b", at=None)]),
            "tasks.1: ",
            id="atもafterもない",
        ),
        pytest.param(
            routine_section([task_dict("a"), task_dict("b", at=None, after=HIDDEN_ID)]),
            "tasks.1.after: ",
            id="存在しないafter",
        ),
        pytest.param(
            routine_section(
                [
                    task_dict("a"),
                    task_dict("b", at=None, after="c"),
                    task_dict("c", at=None, after="b"),
                ]
            ),
            "tasks.1.after: ",
            id="afterの輪",
        ),
        pytest.param(
            routine_section([task_dict("a"), task_dict("b", at=None, after="b")]),
            "tasks.1.after: ",
            id="自分を指すafter",
        ),
        pytest.param(
            routine_section([task_dict("a"), task_dict("b", at=None, after="a", notice="x")]),
            "tasks.1.notice: ",
            id="afterのタスクの予告",
        ),
        pytest.param(
            routine_section(
                [task_dict("a"), task_dict("b", at=None, after="a", sleep=True)],
                wake={"weekday": "06:00", "holiday": "06:00"},
            ),
            "tasks.1.sleep: ",
            id="afterの寝るタスク",
        ),
        pytest.param(
            routine_section(
                [
                    task_dict("a"),
                    task_dict("s1", at="20:00", sleep=True),
                    task_dict("s2", at="21:00", sleep=True, days="weekday"),
                ]
            ),
            "tasks.2.sleep: ",
            id="毎日と平日の寝るタスクが2つ",
        ),
        pytest.param(
            routine_section(
                [
                    task_dict("a"),
                    task_dict("s1", at="20:00", sleep=True, days="holiday"),
                    task_dict("s2", at="21:00", sleep=True, days="holiday"),
                ]
            ),
            "tasks.2.sleep: ",
            id="休日の寝るタスクが2つ",
        ),
        pytest.param(
            routine_section(
                [task_dict("a", days="weekday"), task_dict("s", at="20:00", sleep=True)]
            ),
            "wake.holiday: ",
            id="休日の起床時刻が決められない",
        ),
        pytest.param(
            routine_section(
                [task_dict("a", days="holiday"), task_dict("s", at="20:00", sleep=True)]
            ),
            "wake.weekday: ",
            id="平日の起床時刻が決められない",
        ),
        pytest.param(
            routine_section(
                [task_dict("s", at="21:00", sleep=True, days="weekday")],
                wake={"holiday": "07:00"},
            ),
            "wake.weekday: ",
            id="寝るタスクのほかに平日の時刻がない",
        ),
        pytest.param(
            routine_section(
                [task_dict("a"), task_dict("s", at="06:00", sleep=True)],
                wake={"weekday": "06:30", "holiday": "05:00"},
            ),
            "tasks.1.at: ",
            id="寝るタスクが平日の起床時刻より前",
        ),
        pytest.param(
            routine_section(
                [task_dict("a"), task_dict("s", at="07:30", sleep=True, days="holiday")],
                wake={"weekday": "06:30", "holiday": "07:30"},
            ),
            "tasks.1.at: ",
            id="寝るタスクが休日の起床時刻と同じ",
        ),
    ],
)
def test_つながりの誤りは場所と理由で出て日課を開始しない(
    xdg_dirs: dict[str, Path], section: dict[str, Any], expected: str
) -> None:
    report = _load(xdg_dirs, section)
    assert "routine" not in report.features
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith("routine: ")
    assert expected in errors[0]


def test_時刻のタスクがあれば起床時刻を書かなくてよい(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, routine_section(_morning_and_sleep()))
    assert _errors(report) == []


def test_起床時刻の設定は時刻のタスクより先に使う(xdg_dirs: dict[str, Path]) -> None:
    # 最初の時刻のタスク（07:00）より遅い起床時刻を書くと、寝るタスクはそれより後でないといけない
    tasks = [task_dict("wash", at="07:00"), task_dict("sleep", at="08:00", sleep=True)]
    report = _load(xdg_dirs, routine_section(tasks, wake={"weekday": "09:00"}))
    errors = _errors(report)
    assert len(errors) == 1
    assert "tasks.1.at: " in errors[0]


def test_寝るタスクがなければ起床時刻は要らない(xdg_dirs: dict[str, Path]) -> None:
    tasks = [task_dict("a", at=None, after="b"), task_dict("b", days="weekday")]
    report = _load(xdg_dirs, routine_section(tasks))
    assert _errors(report) == []


def test_平日と休日の寝るタスクは1つずつならよい(xdg_dirs: dict[str, Path]) -> None:
    tasks = [
        task_dict("a"),
        task_dict("s1", at="20:00", sleep=True, days="weekday"),
        task_dict("s2", at="21:00", sleep=True, days="holiday"),
    ]
    report = _load(xdg_dirs, routine_section(tasks))
    assert _errors(report) == []


def test_いくつもの誤りはまとめて出る(xdg_dirs: dict[str, Path]) -> None:
    tasks = [task_dict("a"), task_dict("a", at="08:00"), task_dict("b", at=None, after="zz")]
    errors = _errors(_load(xdg_dirs, routine_section(tasks)))
    assert len(errors) == 1
    assert "tasks.1.id: " in errors[0]
    assert "tasks.2.after: " in errors[0]


@pytest.mark.parametrize(
    ("task_fields", "location"),
    [
        pytest.param({"id": "Bad-ID"}, "routine.tasks.0.id: ", id="idの書き方"),
        pytest.param({"id": "a" * 33}, "routine.tasks.0.id: ", id="idが長い"),
        pytest.param({"at": "25:00"}, "routine.tasks.0.at: ", id="時刻の書き方"),
        pytest.param({"at": "7時"}, "routine.tasks.0.at: ", id="時刻の文字"),
        pytest.param({"at": "21:00+09:00"}, "routine.tasks.0.at: ", id="時刻のタイムゾーン"),
        pytest.param({"at": 24 * 60}, "routine.tasks.0.at: ", id="時刻の数が大きい"),
        pytest.param({"at": True}, "routine.tasks.0.at: ", id="時刻が真偽"),
        pytest.param({"at": 7}, "routine.tasks.0.at: ", id="時刻が小さい数"),
        pytest.param({"at": 0}, "routine.tasks.0.at: ", id="時刻が0"),
        pytest.param({"at": 59}, "routine.tasks.0.at: ", id="時刻が59"),
        pytest.param({"at": "21:00:30"}, "routine.tasks.0.at: ", id="時刻の秒"),
        pytest.param({"at": "07:00:00.5"}, "routine.tasks.0.at: ", id="時刻の秒の小数"),
        pytest.param({"days": "monday"}, "routine.tasks.0.days: ", id="対象の日"),
        pytest.param({"call": ""}, "routine.tasks.0.call: ", id="声かけが空"),
        pytest.param({"praise": ""}, "routine.tasks.0.praise: ", id="ほめる言葉が空"),
        pytest.param({"lcd": ""}, "routine.tasks.0.lcd: ", id="LCDが空"),
        pytest.param({"lcd": "x" * 17}, "routine.tasks.0.lcd: ", id="LCDが長い"),
        pytest.param({"notice_min": 0}, "routine.tasks.0.notice_min: ", id="予告の早さが小さい"),
        pytest.param({"notice_min": 121}, "routine.tasks.0.notice_min: ", id="予告の早さが大きい"),
        pytest.param({"remind_min": 0}, "routine.tasks.0.remind_min: ", id="催促の間隔が小さい"),
        pytest.param({"remind_min": 61}, "routine.tasks.0.remind_min: ", id="催促の間隔が大きい"),
        pytest.param(
            {"max_reminders": -1}, "routine.tasks.0.max_reminders: ", id="催促の回数が小さい"
        ),
        pytest.param(
            {"max_reminders": 11}, "routine.tasks.0.max_reminders: ", id="催促の回数が大きい"
        ),
        pytest.param({"calll": "x"}, "routine.tasks.0.calll: ", id="タスクの知らない項目"),
    ],
)
def test_タスクの項目の誤りはキーのパスで出る(
    xdg_dirs: dict[str, Path], task_fields: dict[str, Any], location: str
) -> None:
    report = _load(xdg_dirs, routine_section([task_dict("a", **task_fields)]))
    assert "routine" not in report.features
    errors = _errors(report)
    assert any(e.startswith(location) for e in errors), errors


@pytest.mark.parametrize(
    ("fields", "location"),
    [
        pytest.param({"keep_days": 6}, "routine.keep_days: ", id="残す日数が小さい"),
        pytest.param({"keep_days": 3651}, "routine.keep_days: ", id="残す日数が大きい"),
        pytest.param({"extra_holidays": ["2026-13-01"]}, "routine.extra_holidays.0: ", id="日付"),
        pytest.param({"wake": {"weekday": "6時"}}, "routine.wake.weekday: ", id="起床時刻"),
        pytest.param({"wake": {"weekday": 7}}, "routine.wake.weekday: ", id="起床時刻が小さい数"),
        pytest.param({"wake": {"holiday": 0}}, "routine.wake.holiday: ", id="起床時刻が0"),
        pytest.param(
            {"wake": {"holiday": "07:00:30"}}, "routine.wake.holiday: ", id="起床時刻の秒"
        ),
        pytest.param({"tasks": "wash"}, "routine.tasks: ", id="タスクの一覧の形"),
    ],
)
def test_節の項目の誤りはキーのパスで出る(
    xdg_dirs: dict[str, Path], fields: dict[str, Any], location: str
) -> None:
    report = _load(xdg_dirs, routine_section(**fields))
    assert "routine" not in report.features
    assert any(e.startswith(location) for e in _errors(report)), _errors(report)


def test_節の知らない項目は警告になる(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, routine_section(colour="red", wake={"weekend": "07:00"}))
    assert _errors(report) == []
    assert "routine" in report.features
    assert sorted(report.warnings) == [
        "routine.colour: 知らない項目です",
        "routine.wake.weekend: 知らない項目です",
    ]


def test_直接作ると誤りは例外になる() -> None:
    with pytest.raises(ValidationError):
        build_config([task_dict("a"), task_dict("a")])


# --- 値を出さない（1.9） ---

_LEAKY_SECTIONS = [
    pytest.param(
        routine_section(
            [
                task_dict(HIDDEN_ID, at=HIDDEN_TIME, call=HIDDEN_PHRASE, notice=HIDDEN_PHRASE),
                task_dict(HIDDEN_ID, at=HIDDEN_TIME, praise=HIDDEN_PHRASE),
            ],
            call_name=HIDDEN_NAME,
        ),
        id="idの重複",
    ),
    pytest.param(
        routine_section(
            [
                task_dict(HIDDEN_ID, at=None, after=HIDDEN_ID, notice=HIDDEN_PHRASE),
                task_dict("b", at=HIDDEN_TIME, after=HIDDEN_ID + "_x", lcd=HIDDEN_PHRASE),
            ],
            call_name=HIDDEN_NAME,
        ),
        id="afterの誤り",
    ),
    pytest.param(
        routine_section(
            [
                task_dict(HIDDEN_ID, at=HIDDEN_TIME, sleep=True, call=HIDDEN_PHRASE),
                task_dict("s2", at=HIDDEN_TIME, sleep=True),
            ],
            call_name=HIDDEN_NAME,
            wake={"weekday": "06:00"},
        ),
        id="寝るタスクの誤り",
    ),
    pytest.param(
        routine_section(
            [
                task_dict(
                    HIDDEN_ID.upper(),
                    at=HIDDEN_TIME + "x",
                    days=HIDDEN_PHRASE,
                    lcd=HIDDEN_PHRASE * 3,
                    **{HIDDEN_ID: HIDDEN_PHRASE},
                )
            ],
            call_name=HIDDEN_NAME,
            keep_days=5,
            extra_holidays=[HIDDEN_TIME],
            wake={"weekday": HIDDEN_PHRASE},
        ),
        id="項目の誤り",
    ),
]


@pytest.mark.parametrize("section", _LEAKY_SECTIONS)
def test_誤りの文言とログに設定の値が出ない(
    xdg_dirs: dict[str, Path],
    caplog: pytest.LogCaptureFixture,
    section: dict[str, Any],
) -> None:
    caplog.set_level("DEBUG")
    report = _load(xdg_dirs, section)
    assert _errors(report), "誤りが出るはず"
    text = _all_texts(report, caplog)
    for value in (HIDDEN_NAME, HIDDEN_PHRASE, HIDDEN_TIME, HIDDEN_ID.upper()):
        assert value not in text
    for error in _errors(report):
        message = error.split(": ", 1)[1]
        assert HIDDEN_ID not in message  # 場所（キーのパス）にだけ知らない項目の名前が出てよい


def test_正しい設定を読んでもログに値が出ない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    section = routine_section(
        [task_dict(HIDDEN_ID, at=HIDDEN_TIME, call=HIDDEN_PHRASE)], call_name=HIDDEN_NAME
    )
    report = _load(xdg_dirs, section)
    assert _errors(report) == []
    text = _all_texts(report, caplog)
    for value in (HIDDEN_NAME, HIDDEN_PHRASE, HIDDEN_TIME, HIDDEN_ID):
        assert value not in text


def test_起床時刻は設定かその種類の日の最初の時刻のタスク() -> None:
    config = build_config(
        [
            task_dict("late", at="08:00"),
            task_dict("early", at="06:45", days="holiday"),
            task_dict("next", at=None, after="late"),
            task_dict("sleep", at="20:00", sleep=True, days="weekday"),
        ],
        wake={"weekday": "06:00"},
    )
    assert config.wake_time("weekday") == time(6, 0)
    assert config.wake_time("holiday") == time(6, 45)


@pytest.mark.parametrize(
    ("text", "location"),
    [
        pytest.param(
            "routine:\n  tasks:\n    - id: a\n      at: 7\n      call: a\n"
            "      praise: b\n      lcd: c\n",
            "routine.tasks.0.at: ",
            id="タスクの時刻",
        ),
        pytest.param(
            "routine:\n  wake:\n    weekday: 7\n", "routine.wake.weekday: ", id="起床時刻"
        ),
        pytest.param(
            "routine:\n  wake:\n    holiday: 21:00:30\n",
            "routine.wake.holiday: ",
            id="引用符のない秒つきの時刻",
        ),
    ],
)
def test_YAMLの時と分でない数は誤り(xdg_dirs: dict[str, Path], text: str, location: str) -> None:
    report = load_report(Paths(), text)
    assert "routine" not in report.features
    errors = _errors(report)
    assert any(e.startswith(location) for e in errors), errors


def test_秒が0の時刻は読める() -> None:
    config = build_config([task_dict("a", at="07:00:00")], wake={"weekday": "06:30:00"})
    assert config.tasks[0].at == time(7, 0)
    assert config.wake.weekday == time(6, 30)
