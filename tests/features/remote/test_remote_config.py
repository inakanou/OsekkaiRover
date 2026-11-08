"""ラジコンの設定（`remote:` の節）の形と検証（1.3、1.4、4.5、5.7、6.2、9.6、11.5、13.1、13.2）。"""

from datetime import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from osekkai.core.config import ConfigLoader, ConfigReport
from osekkai.core.paths import Paths
from osekkai.features.remote.config import RemoteConfig

# 誤りの文言とログに出てはいけない、目立つ値（10 文字未満）
HIDDEN_TIME = "05:43"
HIDDEN_SECONDS = "13.7"
HIDDEN_PIN = "9753"
HIDDEN_DEVICE = "/tmp/vid9"

MSG_FORMAT = '時刻は "HH:MM" のように書いてください'


def _load(xdg_dirs: dict[str, Path], text: str) -> ConfigReport:
    paths = Paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(text, encoding="utf-8")
    return ConfigLoader(paths, {"remote": RemoteConfig}).load()


def _errors(report: ConfigReport) -> list[str]:
    return list(report.feature_errors.get("remote", []))


def _all_texts(report: ConfigReport, caplog: pytest.LogCaptureFixture) -> str:
    parts = [*report.fatal_errors, *report.warnings, *_errors(report)]
    parts.extend(r.getMessage() for r in caplog.records)
    return "\n".join(parts)


def _remote(report: ConfigReport) -> RemoteConfig:
    config = report.features["remote"]
    assert isinstance(config, RemoteConfig)
    return config


# --- 既定値と正しい設定 ---


def test_節がなければ既定値で読める(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "core: {}\n")
    assert _errors(report) == []
    config = _remote(report)
    assert config.enabled is False  # 13.1: 既定は無効
    assert config.pin.get_secret_value() == ""
    assert config.port == 8080  # 1.3
    assert config.hours == (time(7, 0), time(20, 0))  # 11.5
    assert config.speed_slow == 20  # 4.5
    assert config.speed_fast == 35  # 4.5
    assert config.stop_cm == 20.0
    assert config.cliff_stop is True  # 5.7
    assert config.cliff_reference == 500
    assert config.back_max_seconds == 2.0  # 6.2
    assert config.photo_days == 30  # 9.6
    assert config.usb_camera == ""  # 13.1: 空なら自動で探す


def test_書いた値で読める(xdg_dirs: dict[str, Path]) -> None:
    text = (
        "remote:\n"
        "  enabled: true\n"
        '  pin: "0412"\n'
        "  port: 8090\n"
        '  hours: ["06:30", "19:00"]\n'
        "  speed_slow: 15\n"
        "  speed_fast: 40\n"
        "  stop_cm: 30\n"
        "  cliff_stop: false\n"
        "  cliff_reference: 700\n"
        "  back_max_seconds: 1.5\n"
        "  photo_days: 7\n"
        "  usb_camera: /dev/video2\n"
    )
    report = _load(xdg_dirs, text)
    assert _errors(report) == []
    assert report.warnings == []
    config = _remote(report)
    assert config.enabled is True
    assert config.pin.get_secret_value() == "0412"
    assert config.port == 8090
    assert config.hours == (time(6, 30), time(19, 0))
    assert config.speed_slow == 15
    assert config.speed_fast == 40
    assert config.stop_cm == 30.0
    assert config.cliff_stop is False
    assert config.cliff_reference == 700
    assert config.back_max_seconds == 1.5
    assert config.photo_days == 7
    assert config.usb_camera == "/dev/video2"


@pytest.mark.parametrize(
    ("line", "attr", "value"),
    [
        ("port: 1024", "port", 1024),
        ("port: 65535", "port", 65535),
        ("speed_slow: 1", "speed_slow", 1),
        ("speed_fast: 100", "speed_fast", 100),
        ("stop_cm: 5", "stop_cm", 5.0),
        ("stop_cm: 100", "stop_cm", 100.0),
        ("cliff_reference: 0", "cliff_reference", 0),
        ("cliff_reference: 4095", "cliff_reference", 4095),
        ("back_max_seconds: 0.5", "back_max_seconds", 0.5),
        ("back_max_seconds: 10", "back_max_seconds", 10.0),
        ("photo_days: 1", "photo_days", 1),
        ("photo_days: 365", "photo_days", 365),
    ],
)
def test_範囲の端は読める(xdg_dirs: dict[str, Path], line: str, attr: str, value: float) -> None:
    report = _load(xdg_dirs, f"remote:\n  {line}\n")
    assert _errors(report) == []
    assert getattr(_remote(report), attr) == value


def test_2_つの速さは同じでもよい(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "remote:\n  speed_slow: 30\n  speed_fast: 30\n")
    assert _errors(report) == []
    assert _remote(report).speed_fast == 30


def test_引用符のない時刻も時と分として読む(xdg_dirs: dict[str, Path]) -> None:
    # YAML 1.1 は 19:30 を 60 進の数（1170）として読むので、時と分に戻す
    report = _load(xdg_dirs, "remote:\n  hours: [7:05, 19:30]\n")
    assert _errors(report) == []
    assert _remote(report).hours == (time(7, 5), time(19, 30))


def test_日をまたぐ時間帯は読める(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, 'remote:\n  hours: ["22:00", "06:00"]\n')
    assert _errors(report) == []
    assert _remote(report).hours == (time(22, 0), time(6, 0))


# --- pin ---


@pytest.mark.parametrize("pin", ["1234", "0000", "12345678", "0412"])
def test_4_から_8_けたの数字の_pin_は使える(pin: str) -> None:
    assert RemoteConfig(pin=SecretStr(pin)).pin_usable() is True  # 1.4


@pytest.mark.parametrize(
    "pin",
    [
        "",
        "123",
        "123456789",
        "12a4",
        "12 34",
        " 1234",
        "1234\n",
        "１２３４",
        "١٢٣٤",
        "12.4",
        "-123",
    ],
)
def test_使えない_pin(pin: str) -> None:
    # 全角やアラビアの数字は ASCII の数字ではない（1.4）
    assert RemoteConfig(pin=SecretStr(pin)).pin_usable() is False


@pytest.mark.parametrize(
    "line",
    [
        "pin: ab",
        "pin: 12",
        'pin: "123456789"',
        "pin: 1234",  # 引用符のない数は先頭の 0 が消えるので使わない
        "pin: 0412",  # YAML 1.1 では 8 進の数になる
        "pin: true",
        "pin: [1, 2]",
        "pin:",
    ],
)
def test_使えない_pin_は設定の誤りにしない(xdg_dirs: dict[str, Path], line: str) -> None:
    report = _load(xdg_dirs, f"remote:\n  {line}\n")
    assert _errors(report) == []  # 1.4: 誤りにせず、待ち受けないだけ
    assert _remote(report).pin_usable() is False


def test_pin_の値は表示に出ない(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, f'remote:\n  pin: "{HIDDEN_PIN}"\n')
    config = _remote(report)
    assert config.pin_usable() is True
    assert HIDDEN_PIN not in repr(config)
    assert HIDDEN_PIN not in str(config)
    assert HIDDEN_PIN not in str(config.model_dump())


def test_ほかの項目の誤りでも_pin_の値は出ない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, f'remote:\n  pin: "{HIDDEN_PIN}"\n  port: 80\n')
    assert _errors(report)
    assert HIDDEN_PIN not in _all_texts(report, caplog)


# --- 誤り ---


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("port: 1023", "remote.port"),
        ("port: 65536", "remote.port"),
        ("speed_slow: 0", "remote.speed_slow"),
        ("speed_slow: 101", "remote.speed_slow"),
        ("speed_fast: 0", "remote.speed_fast"),
        ("speed_fast: 101", "remote.speed_fast"),
        ("stop_cm: 4.9", "remote.stop_cm"),
        ("stop_cm: 100.1", "remote.stop_cm"),
        ("cliff_reference: -1", "remote.cliff_reference"),
        ("cliff_reference: 4096", "remote.cliff_reference"),
        ("back_max_seconds: 0.4", "remote.back_max_seconds"),
        ("back_max_seconds: 10.1", "remote.back_max_seconds"),
        ("photo_days: 0", "remote.photo_days"),
        ("photo_days: 366", "remote.photo_days"),
    ],
)
def test_範囲の外は場所と理由だけを出して開始しない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture, line: str, key: str
) -> None:
    report = _load(xdg_dirs, f"remote:\n  {line}\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith(f"{key}: ")
    assert len(errors[0]) > len(f"{key}: ")  # 理由がある
    assert "remote" not in report.features  # 13.2
    value = line.split(": ", 1)[1]
    assert value not in _all_texts(report, caplog)


def test_はやいが_ゆっくり_より遅いと誤り(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, "remote:\n  speed_slow: 47\n  speed_fast: 46\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith("remote.speed_fast: ")
    assert "remote" not in report.features
    texts = _all_texts(report, caplog)
    assert "47" not in texts
    assert "46" not in texts


@pytest.mark.parametrize(
    ("hours", "key", "reason"),
    [
        ("[7, 19:30]", "remote.hours.0", MSG_FORMAT),
        ("[0, 19:30]", "remote.hours.0", MSG_FORMAT),
        ("[7:00, 7.5]", "remote.hours.1", MSG_FORMAT),
        ("[true, 19:30]", "remote.hours.0", MSG_FORMAT),
        ("[1440, 19:30]", "remote.hours.0", MSG_FORMAT),
        ('["07:00", "05:43:21"]', "remote.hours.1", "時刻に秒は書けません"),
        ('["05:43+09:00", "19:30"]', "remote.hours.0", "時刻にタイムゾーンは書けません"),
    ],
)
def test_時刻の書き方の誤りは場所と決まった理由だけを出す(
    xdg_dirs: dict[str, Path],
    caplog: pytest.LogCaptureFixture,
    hours: str,
    key: str,
    reason: str,
) -> None:
    report = _load(xdg_dirs, f"remote:\n  hours: {hours}\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith(f"{key}: ")
    assert reason in errors[0]
    assert "remote" not in report.features
    texts = _all_texts(report, caplog)
    assert HIDDEN_TIME not in texts
    assert "05:43:21" not in texts
    assert "1440" not in texts


def test_始めと終わりが同じ時間帯は誤り(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, f'remote:\n  hours: ["{HIDDEN_TIME}", "{HIDDEN_TIME}"]\n')
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith("remote.hours: ")
    assert "始めと終わりが同じ" in errors[0]  # 11.5
    assert "remote" not in report.features
    assert HIDDEN_TIME not in _all_texts(report, caplog)


def test_時間帯が_2_つでなければ誤り(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, 'remote:\n  hours: ["07:00"]\n')
    errors = _errors(report)
    assert errors
    assert all(e.startswith("remote.hours") for e in errors)


def test_誤りの文言に後退の長さの値が出ない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, f"remote:\n  back_max_seconds: {HIDDEN_SECONDS}\n")
    assert _errors(report)
    assert HIDDEN_SECONDS not in _all_texts(report, caplog)


def test_USB_カメラの指定は_dev_で始まらなければ誤りで値を出さない(
    xdg_dirs: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    report = _load(xdg_dirs, f"remote:\n  usb_camera: {HIDDEN_DEVICE}\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith("remote.usb_camera: ")
    assert len(errors[0]) > len("remote.usb_camera: ")
    assert "remote" not in report.features
    assert HIDDEN_DEVICE not in _all_texts(report, caplog)


def test_速さは整数でなければ誤り(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "remote:\n  speed_slow: 2.5\n")
    errors = _errors(report)
    assert len(errors) == 1
    assert errors[0].startswith("remote.speed_slow: ")


def test_知らない項目は警告になる(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "remote:\n  daily_limit_min: 60\n")
    # 11.6: 1 日の上限は持たない。書いても効かないので知らない項目として警告する
    assert _errors(report) == []
    assert "remote.daily_limit_min: 知らない項目です" in report.warnings


def test_有効にできる(xdg_dirs: dict[str, Path]) -> None:
    report = _load(xdg_dirs, "remote:\n  enabled: true\n")
    assert _remote(report).enabled is True
