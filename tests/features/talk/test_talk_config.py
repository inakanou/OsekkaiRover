"""朝と夜のお話の設定（`talk:` の節）の形と検証（1.1〜1.4、1.8、5.4〜5.6、9.1、9.2、10.2）。"""

from pathlib import Path

import pytest

from osekkai.core.config import ConfigLoader, ConfigReport
from osekkai.core.paths import Paths
from osekkai.features.talk.config import Location, TalkConfig, WarningArea

# 誤りの文言とログに出てはいけない、目立つ値
HIDDEN_NAME = "みはるん"
HIDDEN_TOPIC = "ぷてらのどん"
HIDDEN_LATITUDE = "91.37"
HIDDEN_LONGITUDE = "-181.73"
HIDDEN_OFFICE = "13579"
HIDDEN_CITY = "24680135"


def _load(xdg_dirs: dict[str, Path], text: str) -> ConfigReport:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")
    return ConfigLoader(paths, {"talk": TalkConfig}).load()


def _errors(report: ConfigReport) -> list[str]:
    return list(report.feature_errors.get("talk", []))


def _all_texts(report: ConfigReport, caplog: pytest.LogCaptureFixture) -> str:
    parts = [*report.fatal_errors, *report.warnings, *_errors(report)]
    parts.extend(r.getMessage() for r in caplog.records)
    return "\n".join(parts)


def _talk(report: ConfigReport) -> TalkConfig:
    config = report.features["talk"]
    assert isinstance(config, TalkConfig)
    return config


def _assert_one_error(report: ConfigReport, key: str) -> str:
    errors = _errors(report)
    assert len(errors) == 1, errors
    assert errors[0].startswith(f"{key}: ")
    assert len(errors[0]) > len(f"{key}: ")  # 理由がある
    assert "talk" not in report.features  # 1.4: 開始しない
    return errors[0]


# --- 既定値と正しい設定 ---


def test_節がなければ使わない設定として既定値で読める(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "core: {}\n")
    assert _errors(report) == []
    config = _talk(report)
    assert config.enabled is False  # 1.2
    assert config.call_name == ""
    assert config.night is True
    assert config.morning is True
    assert config.location is None
    assert config.warning_area is None
    assert config.topics == []
    assert config.rain_percent == 50  # 5.4
    assert config.cold_below == 10  # 5.5
    assert config.hot_at_or_above == 30  # 5.6
    assert config.fetch_timeout_seconds == 20.0  # 10.2


def test_空の節でも使わない設定として読める(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "talk: {}\n")
    assert _errors(report) == []
    assert _talk(report).enabled is False


def test_書いた値で読める(xdg_dirs: dict[str, Path]) -> None:
    text = (
        "talk:\n"
        "  enabled: true\n"
        "  call_name: たろう\n"
        "  night: true\n"
        "  morning: false\n"
        "  location: {latitude: 35.68, longitude: 139.69}\n"
        '  warning_area: {office: "130000", city: "1311300"}\n'
        '  topics: ["きょうりゅう", " でんしゃ "]\n'
        "  rain_percent: 60\n"
        "  cold_below: 8\n"
        "  hot_at_or_above: 32\n"
        "  fetch_timeout_seconds: 15\n"
    )
    report = _load(xdg_dirs, text)
    assert _errors(report) == []
    assert report.warnings == []
    config = _talk(report)
    assert config.enabled is True
    assert config.call_name == "たろう"
    assert config.night is True
    assert config.morning is False  # 1.3
    assert config.location == Location(latitude=35.68, longitude=139.69)
    assert config.warning_area == WarningArea(office="130000", city="1311300")
    assert config.topics == ["きょうりゅう", "でんしゃ"]  # 前後の空白は落とす
    assert config.rain_percent == 60
    assert config.cold_below == 8
    assert config.hot_at_or_above == 32
    assert config.fetch_timeout_seconds == 15.0


@pytest.mark.parametrize(
    ("line", "attr", "value"),
    [
        ("rain_percent: 1", "rain_percent", 1),
        ("rain_percent: 100", "rain_percent", 100),
        ("cold_below: -20", "cold_below", -20),
        ("cold_below: 30", "cold_below", 30),
        ("hot_at_or_above: 20", "hot_at_or_above", 20),
        ("hot_at_or_above: 45", "hot_at_or_above", 45),
        ("fetch_timeout_seconds: 5", "fetch_timeout_seconds", 5.0),
        ("fetch_timeout_seconds: 60", "fetch_timeout_seconds", 60.0),
    ],
)
def test_範囲の端は読める(xdg_dirs: dict[str, Path], line: str, attr: str, value: float) -> None:
    report = _load(xdg_dirs, f"talk:\n  {line}\n")
    assert _errors(report) == []
    assert getattr(_talk(report), attr) == value


@pytest.mark.parametrize(
    "location",
    [
        "{latitude: -90, longitude: -180}",
        "{latitude: 90, longitude: 180}",
    ],
)
def test_座標の端は読める(xdg_dirs: dict[str, Path], location: str) -> None:
    report = _load(xdg_dirs, f"talk:\n  location: {location}\n")
    assert _errors(report) == []
    assert _talk(report).location is not None


def test_好きなものは_20_個_20_文字まで読める(xdg_dirs: dict[str, Path]) -> None:
    topics = ", ".join(f'"{"あ" * 19}{chr(0x3041 + i)}"' for i in range(20))
    report = _load(xdg_dirs, f"talk:\n  topics: [{topics}]\n")
    assert _errors(report) == []
    assert len(_talk(report).topics) == 20


def test_呼び名が空なら好きなものは何でもよい(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, 'talk:\n  topics: ["たろうの でんしゃ"]\n')
    assert _errors(report) == []


# --- 誤り ---


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("rain_percent: 0", "talk.rain_percent"),
        ("rain_percent: 101", "talk.rain_percent"),
        ("cold_below: -21", "talk.cold_below"),
        ("cold_below: 31", "talk.cold_below"),
        ("hot_at_or_above: 19", "talk.hot_at_or_above"),
        ("hot_at_or_above: 46", "talk.hot_at_or_above"),
        ("fetch_timeout_seconds: 4.9", "talk.fetch_timeout_seconds"),
        ("fetch_timeout_seconds: 60.1", "talk.fetch_timeout_seconds"),
        ("rain_percent: 55.5", "talk.rain_percent"),
    ],
)
def test_範囲の外は場所と理由だけを出して開始しない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture, line: str, key: str
) -> None:
    report = _load(xdg_dirs, f"talk:\n  {line}\n")
    _assert_one_error(report, key)
    value = line.split(": ", 1)[1]
    assert value not in _all_texts(report, caplog)


@pytest.mark.parametrize(
    ("location", "key", "hidden"),
    [
        (
            f"{{latitude: {HIDDEN_LATITUDE}, longitude: 139.69}}",
            "talk.location.latitude",
            HIDDEN_LATITUDE,
        ),
        (
            f"{{latitude: -{HIDDEN_LATITUDE}, longitude: 139.69}}",
            "talk.location.latitude",
            HIDDEN_LATITUDE,
        ),
        (
            f"{{latitude: 35.68, longitude: {HIDDEN_LONGITUDE}}}",
            "talk.location.longitude",
            HIDDEN_LONGITUDE,
        ),
        ("{latitude: 35.68, longitude: 181.73}", "talk.location.longitude", "181.73"),
        (f"{{latitude: {HIDDEN_NAME}, longitude: 139.69}}", "talk.location.latitude", HIDDEN_NAME),
        ("{latitude: 35.68}", "talk.location.longitude", "35.68"),
        (HIDDEN_NAME, "talk.location", HIDDEN_NAME),
    ],
)
def test_現在地の書き方の誤りは場所と理由だけを出す(
    xdg_dirs: dict[str, Path],
    caplog: pytest.LogCaptureFixture,
    location: str,
    key: str,
    hidden: str,
) -> None:
    report = _load(xdg_dirs, f"talk:\n  location: {location}\n")
    _assert_one_error(report, key)  # 1.4
    assert hidden not in _all_texts(report, caplog)  # 1.8


@pytest.mark.parametrize(
    ("area", "key", "hidden"),
    [
        (
            f'{{office: "{HIDDEN_OFFICE}", city: "1311300"}}',
            "talk.warning_area.office",
            HIDDEN_OFFICE,
        ),
        ('{office: "1300001", city: "1311300"}', "talk.warning_area.office", "1300001"),
        ('{office: "13000a", city: "1311300"}', "talk.warning_area.office", "13000a"),
        (f'{{office: "130000", city: "{HIDDEN_CITY}"}}', "talk.warning_area.city", HIDDEN_CITY),
        ('{office: "130000", city: "131130"}', "talk.warning_area.city", "131130"),
        ('{office: "130000", city: "１３１１３００"}', "talk.warning_area.city", "１３１１３００"),
        ('{office: "130000"}', "talk.warning_area.city", "130000"),
    ],
)
def test_注意報の地域のコードの誤りは場所と理由だけを出す(
    xdg_dirs: dict[str, Path],
    caplog: pytest.LogCaptureFixture,
    area: str,
    key: str,
    hidden: str,
) -> None:
    report = _load(xdg_dirs, f"talk:\n  warning_area: {area}\n")
    _assert_one_error(report, key)
    assert hidden not in _all_texts(report, caplog)


@pytest.mark.parametrize(
    ("topics", "key"),
    [
        (f'["{HIDDEN_TOPIC}", ""]', "talk.topics.1"),
        (f'["{HIDDEN_TOPIC}", "   "]', "talk.topics.1"),
        (f'["{HIDDEN_TOPIC}{"あ" * 16}"]', "talk.topics.0"),
        (f'["{HIDDEN_TOPIC}", [1]]', "talk.topics.1"),
    ],
)
def test_好きなものの空や長すぎる項目は場所と理由だけを出す(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture, topics: str, key: str
) -> None:
    report = _load(xdg_dirs, f"talk:\n  topics: {topics}\n")
    _assert_one_error(report, key)  # 1.4
    assert HIDDEN_TOPIC not in _all_texts(report, caplog)  # 1.8


def test_好きなものが多すぎると誤り(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    topics = ", ".join(f'"{HIDDEN_TOPIC}{i}"' for i in range(21))
    report = _load(xdg_dirs, f"talk:\n  topics: [{topics}]\n")
    _assert_one_error(report, "talk.topics")
    assert HIDDEN_TOPIC not in _all_texts(report, caplog)


@pytest.mark.parametrize(
    "topic",
    [HIDDEN_NAME, f"{HIDDEN_NAME}の でんしゃ", f"だいすき{HIDDEN_NAME}"],
)
def test_呼び名を含む好きなものは誤りで値を出さない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture, topic: str
) -> None:
    text = f'talk:\n  call_name: {HIDDEN_NAME}\n  topics: ["きょうりゅう", "{topic}"]\n'
    report = _load(xdg_dirs, text)
    message = _assert_one_error(report, "talk.topics")  # 9.1 の守り
    assert "呼び名" in message
    texts = _all_texts(report, caplog)
    assert HIDDEN_NAME not in texts
    assert "きょうりゅう" not in texts
    assert "でんしゃ" not in texts


def test_呼び名を含まない好きなものは読める(xdg_dirs: dict[str, Path]) -> None:
    text = f'talk:\n  call_name: {HIDDEN_NAME}\n  topics: ["きょうりゅう", "でんしゃ"]\n'
    report = _load(xdg_dirs, text)
    assert _errors(report) == []
    assert _talk(report).call_name == HIDDEN_NAME


def test_ほかの項目の誤りでも呼び名と好きなものと現在地は出ない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    text = (
        "talk:\n"
        f"  call_name: {HIDDEN_NAME}\n"
        f'  topics: ["{HIDDEN_TOPIC}"]\n'
        "  location: {latitude: 35.6812, longitude: 139.7671}\n"
        "  rain_percent: 0\n"
    )
    report = _load(xdg_dirs, text)
    _assert_one_error(report, "talk.rain_percent")
    texts = _all_texts(report, caplog)
    for hidden in (HIDDEN_NAME, HIDDEN_TOPIC, "35.6812", "139.7671"):
        assert hidden not in texts  # 1.8


def test_知らない項目は警告になり値を出さない(xdg_dirs: dict[str, Path]) -> None:
    report = _load(
        xdg_dirs,
        f"talk:\n  location: {{latitude: 35.68, longitude: 139.69, town: {HIDDEN_NAME}}}\n",
    )
    assert _errors(report) == []
    assert "talk.location.town: 知らない項目です" in report.warnings
    assert all(HIDDEN_NAME not in w for w in report.warnings)
