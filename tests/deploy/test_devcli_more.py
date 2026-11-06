"""開発 PC のそのほかのサブコマンドのテスト。

版の確認・普段の版の記録・self-check・バックアップの一覧と復元・停止・記録の表示・初期設定と、
戻し忘れの警告。

要件: 3.7、5.2、5.4、5.5、6.8、7.2、8.3、8.4、11.2
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from devcli_fakes import (
    DEPLOY_ID,
    HOST,
    NOW,
    REPO_URL,
    SAMPLE,
    SHA,
    TAG,
    FakeRemote,
    Reply,
    make_setup,
    ng,
    ok,
)
from fake_runner import FakeRunner

from rover_deploy.common import ExitCode, Target, target_to_dict
from rover_deploy.refs import RefNotFound, Resolution
from rover_deploy.remote import AgentReply, DeviceUnreachable, RemoteTimedOut, check_remote_path

BRANCH = Target(kind="release", ref="feat/x", ref_kind="branch", sha="b" * 40)
REMINDER = "試しの版が 3 時間以上入っています。rover-deploy back で戻してください"
HOUR = 3600.0


def status_reply(
    *,
    current: Target | None = TAG,
    everyday: Target | None = SAMPLE,
    running: str = "core",
    elapsed: float | None = 1 * HOUR + 5 * 60,
    reminded_at: float | None = None,
    last_deploy: dict[str, object] | None = None,
    in_progress: str | None = None,
    lock: str = "free",
    state_ok: bool = True,
) -> AgentReply:
    is_trial = current is not None and current != everyday
    return AgentReply(
        0,
        {
            "ok": state_ok,
            "reason": "" if state_ok else "state_broken",
            "running": running,
            "current": None if current is None else target_to_dict(current),
            "current_label": None if current is None else current.ref or "sample",
            "everyday": None if everyday is None else target_to_dict(everyday),
            "everyday_label": None if everyday is None else everyday.ref or "sample",
            "is_trial": is_trial,
            "trial_since": NOW - elapsed if is_trial and elapsed is not None else None,
            "trial_elapsed_s": elapsed if is_trial else None,
            "reminded_at": reminded_at,
            "last_deploy": last_deploy,
            "in_progress": in_progress,
            "lock": lock,
        },
    )


QUIET_STATUS = status_reply(current=SAMPLE, running="sample")  # 試しの版ではない


def remote_with(**replies: Reply | list[Reply]) -> FakeRemote:
    table: dict[str, list[Reply]] = {"status": [QUIET_STATUS]}
    for name, value in replies.items():
        table[name.replace("_", "-")] = value if isinstance(value, list) else [value]
    return FakeRemote(replies=table)


# --- 版の確認 ---


def test_版の確認は動いている版と普段の版と試しの経過を出す(tmp_path: Path) -> None:
    last = {"id": DEPLOY_ID, "outcome": "succeeded", "finished_at": NOW}
    s = make_setup(tmp_path, remote_with(status=status_reply(last_deploy=last)))
    assert s.run("status") == ExitCode.OK
    assert "動いている版: v0.2.0" in s.text
    assert "普段の版: sample" in s.text
    assert "試しの版です（入れてから 1 時間 5 分）" in s.text
    assert f"最後の入れ替え: {DEPLOY_ID} 成功" in s.text
    assert REMINDER not in s.text
    # 版の確認では status を 1 回だけ呼ぶ
    assert s.remote.agent_names() == ["status"]
    assert s.remote.calls[0][0] == "push"


def test_版の確認で試しでなければその旨を出す(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with())
    assert s.run("status") == ExitCode.OK
    assert "動いている版: sample" in s.text
    assert "試しの版ではありません" in s.text
    assert "最後の入れ替え: まだありません" in s.text


def test_版の確認で普段の版が決まっていなければ案内する(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(status=status_reply(current=None, everyday=None)))
    assert s.run("status") == ExitCode.OK
    assert "動いている版: 止まっています" not in s.text  # running は core
    assert "普段の版: まだ決まっていません" in s.text


def test_版の確認で途中で終わった入れ替えを警告する(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(status=status_reply(in_progress=DEPLOY_ID)))
    assert s.run("status") == ExitCode.OK
    assert f"入れ替え {DEPLOY_ID} が途中で終わっています" in s.text
    assert "rover-deploy back" in s.text


def test_版の確認で入れ替えの最中ならその旨を出す(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(status=status_reply(in_progress=DEPLOY_ID, lock="busy")))
    assert s.run("status") == ExitCode.OK
    assert f"入れ替え {DEPLOY_ID} が実機で動いています" in s.text


def test_版の確認で状態が読めなければ10(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(status=status_reply(state_ok=False)))
    assert s.run("status") == ExitCode.ABORTED
    assert "動いている版: v0.2.0" in s.text
    assert "状態のファイルが読めません" in s.text


def test_版の確認でつながらなければ10(tmp_path: Path) -> None:
    remote = remote_with()
    remote.push_error = DeviceUnreachable()
    s = make_setup(tmp_path, remote)
    assert s.run("status") == ExitCode.ABORTED
    assert "実機に接続できません" in s.text


def test_版の確認で道具が思わぬ形で終われば10(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(status=AgentReply(1, {"ok": False})))
    assert s.run("status") == ExitCode.ABORTED


# --- 戻し忘れの警告 ---


@pytest.mark.parametrize(
    ("elapsed", "reminded_at", "warned"),
    [
        (3 * HOUR + 1, None, True),
        (1 * HOUR, NOW - 60, True),  # 実機が知らせた
        (2 * HOUR, None, False),
        (None, None, False),
    ],
)
def test_版の確認でも戻し忘れを最初に警告する(
    tmp_path: Path, elapsed: float | None, reminded_at: float | None, warned: bool
) -> None:
    reply = status_reply(elapsed=elapsed, reminded_at=reminded_at)
    s = make_setup(tmp_path, remote_with(status=reply))
    s.run("status")
    assert (REMINDER in s.text) is warned
    if warned:
        assert s.text.startswith(REMINDER)


LONG_TRIAL = status_reply(elapsed=4 * HOUR)


@pytest.mark.parametrize(
    ("argv", "replies"),
    [
        (("everyday", "sample"), {"set-everyday": ok(everyday_label="sample")}),
        (("self-check",), {"self-check": ok(results=[], no_hardware=False)}),
        (("backups",), {"backups": ok(backups=[])}),
        (("restore", DEPLOY_ID), {"restore": ok(stopped=["osekkai.service"])}),
        (("stop",), {"stop": ok(failed=[])}),
        (("log",), {"log": ok(deploy_id=DEPLOY_ID, events=[], result=None)}),
        (("provision", "--ref", "v0.2.0"), {"provision": ok(skipped=[], applied=[])}),
    ],
)
def test_どのサブコマンドでも戻し忘れを最初に警告する(
    tmp_path: Path, argv: tuple[str, ...], replies: dict[str, Reply]
) -> None:
    remote = FakeRemote(replies={"status": [LONG_TRIAL], **{k: [v] for k, v in replies.items()}})
    s = make_setup(tmp_path, remote)
    s.run(*argv)
    assert s.text.startswith(REMINDER)
    assert s.remote.agent_names()[0] == "status"


def test_入れ替えと普段の版に戻すときも戻し忘れを警告する(tmp_path: Path) -> None:
    remote = FakeRemote(replies={"status": [LONG_TRIAL], "preflight": [ng("driving")]})
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert s.text.startswith(REMINDER)
    remote2 = FakeRemote(replies={"status": [LONG_TRIAL], "preflight": [ng("driving")]})
    s2 = make_setup(tmp_path, remote2)
    assert s2.run("back") == ExitCode.ABORTED
    assert s2.text.startswith(REMINDER)
    assert remote2.agent_names() == ["status", "preflight"]  # status は 1 回だけ


def test_戻し忘れの確かめに失敗してもサブコマンドは進む(tmp_path: Path) -> None:
    remote = FakeRemote(replies={"status": [RemoteTimedOut()], "stop": [ok(failed=[])]})
    s = make_setup(tmp_path, remote)
    assert s.run("stop") == ExitCode.OK
    assert REMINDER not in s.text


# --- 普段の版の記録 ---


def test_普段の版をタグで記録しbackを案内する(tmp_path: Path) -> None:
    remote = remote_with(set_everyday=ok(everyday=target_to_dict(TAG), everyday_label="v0.2.0"))
    s = make_setup(tmp_path, remote)
    assert s.run("everyday", "v0.2.0") == ExitCode.OK
    assert s.resolved == ["v0.2.0"]
    args = s.remote.agent_args("set-everyday")
    assert args[1] == "--target"
    assert json.loads(args[2]) == target_to_dict(TAG)
    assert "普段の版を v0.2.0 にしました。入れ替えは rover-deploy back で行います" in s.text


def test_普段の版をサンプルにするときは版を解決しない(tmp_path: Path) -> None:
    remote = remote_with(set_everyday=ok(everyday_label="sample"))
    s = make_setup(tmp_path, remote, resolution=AssertionError("解決しない"))
    assert s.run("everyday", "sample") == ExitCode.OK
    assert json.loads(s.remote.agent_args("set-everyday")[2]) == target_to_dict(SAMPLE)
    assert "普段の版を sample にしました" in s.text


def test_普段の版にブランチは使えない(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(), resolution=Resolution(BRANCH, also_branch=False))
    assert s.run("everyday", "feat/x") == ExitCode.ABORTED
    assert "普段の版にはタグを指定してください" in s.text
    assert s.remote.calls == []  # 実機に触れない


def test_普段の版が見つからなければ10(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(), resolution=RefNotFound("v9.9.9"))
    assert s.run("everyday", "v9.9.9") == ExitCode.ABORTED
    assert "見つかりません" in s.text


def test_普段の版を記録できなければ10(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(set_everyday=ng("lock_busy")))
    assert s.run("everyday", "sample") == ExitCode.ABORTED
    assert "ほかの入れ替えなど" in s.text


# --- self-check ---

RESULTS = [
    {"name": "core.config", "status": "ok", "reason": ""},
    {"name": "core.lcd", "status": "failed", "reason": "応答がない"},
    {"name": "core.servos", "status": "skipped", "reason": "ほかが動いている"},
]


def test_selfcheckは項目ごとに出しだめがあれば1(tmp_path: Path) -> None:
    reply = AgentReply(
        0, {"ok": False, "reason": "", "results": RESULTS, "no_hardware": False, "running": "core"}
    )
    s = make_setup(tmp_path, remote_with(self_check=reply))
    assert s.run("self-check") == 1
    assert "  よい: core.config" in s.text
    assert "  だめ: core.lcd（応答がない）" in s.text
    assert "  飛ばした: core.servos（ほかが動いている）" in s.text


def test_selfcheckがすべてよければ0(tmp_path: Path) -> None:
    reply = ok(results=RESULTS[:1], no_hardware=True, running="sample")
    s = make_setup(tmp_path, remote_with(self_check=reply))
    assert s.run("self-check") == ExitCode.OK
    assert "  よい: core.config" in s.text
    assert "ハードの項目は確かめていません" in s.text


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("no_release", "新しい基盤の版がありません"),
        ("self_check_error", "self-check を行えませんでした"),
        ("self_check_unreadable", "self-check の結果が読めません"),
        ("lock_busy", "ほかの入れ替えなど"),
    ],
)
def test_selfcheckを行えなければ10(tmp_path: Path, reason: str, expected: str) -> None:
    s = make_setup(tmp_path, remote_with(self_check=ng(reason)))
    assert s.run("self-check") == ExitCode.ABORTED
    assert expected in s.text


# --- バックアップの一覧と復元 ---


def test_バックアップの一覧は識別子と大きさを出す(tmp_path: Path) -> None:
    backups = [
        {"id": "20261026T073320Z", "size_bytes": 3 * 1024 * 1024},
        {"id": "20261025T010000Z", "size_bytes": 1024 * 1024},
    ]
    s = make_setup(tmp_path, remote_with(backups=ok(backups=backups)))
    assert s.run("backups") == ExitCode.OK
    assert "  20261026T073320Z  3.0 MB" in s.text
    assert "  20261025T010000Z  1.0 MB" in s.text
    assert s.text.index("20261026T073320Z") < s.text.index("20261025T010000Z")


def test_バックアップがなければその旨を出す(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(backups=ok(backups=[])))
    assert s.run("backups") == ExitCode.OK
    assert "実機にバックアップがありません" in s.text


def test_バックアップの一覧が読めなければ10(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(backups=ng("backups_unreadable")))
    assert s.run("backups") == ExitCode.ABORTED


def restore_ok(**extra: object) -> AgentReply:
    data: dict[str, object] = {
        "include_sample": False,
        "pre_backup": {"id": "20261026T080000Z", "size_bytes": 2 * 1024 * 1024},
        "stopped": ["osekkai.service"],
        "started": ["osekkai.service"],
    }
    data.update(extra)
    return ok(**data)


def test_識別子を指定して戻す(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(restore=restore_ok()))
    assert s.run("restore", DEPLOY_ID) == ExitCode.OK
    assert s.remote.agent_args("restore") == ("restore", DEPLOY_ID)
    assert f"バックアップ {DEPLOY_ID} から戻しました" in s.text
    assert "戻す前の状態を実機に残しました（識別子: 20261026T080000Z、2.0 MB）" in s.text
    assert "起動したサービス: osekkai.service" in s.text


def test_サンプルのデータも戻す印を渡す(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(restore=restore_ok(include_sample=True)))
    assert s.run("restore", DEPLOY_ID, "--include-sample") == ExitCode.OK
    assert s.remote.agent_args("restore") == ("restore", DEPLOY_ID, "--include-sample")


def test_識別子の形が違えば実機に頼まない(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with())
    assert s.run("restore", "../etc") == ExitCode.ABORTED
    assert "識別子の形が違います" in s.text
    assert s.remote.calls == []


def test_開発PCのファイルを送ってから戻す(tmp_path: Path) -> None:
    local = tmp_path / "backup-20261020T000000Z.tar.gz"
    local.write_bytes(b"x")
    s = make_setup(tmp_path, remote_with(restore=restore_ok()))
    assert s.run("restore", "--file", str(local)) == ExitCode.OK
    sends = [c for c in s.remote.calls if c[0] == "send"]
    assert len(sends) == 1
    remote_path = sends[0][2]
    assert isinstance(remote_path, str)
    assert check_remote_path(remote_path) == remote_path
    assert remote_path == f".local/state/osekkai-deploy/backups/uploaded-{DEPLOY_ID}.tar.gz"
    assert sends[0][1] == local
    # 道具は ~/osekkai-rover/tool で動くので、そこから見た置き場所を渡す
    assert s.remote.agent_args("restore") == ("restore", "--file", f"../../{remote_path}")
    assert s.remote.agent_names() == ["status", "restore"]
    assert "開発 PC のファイルから戻しました" in s.text
    assert ".local/state" not in s.text  # 実機の置き場所は出さない


def test_開発PCのファイルがなければ送らない(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with())
    assert s.run("restore", "--file", str(tmp_path / "none.tar.gz")) == ExitCode.ABORTED
    assert "戻すファイルが見つかりません" in s.text
    assert s.remote.calls == []


def test_開発PCのファイルがディレクトリなら送らない(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with())
    assert s.run("restore", "--file", str(tmp_path)) == ExitCode.ABORTED
    assert s.remote.calls == []


def test_送るのに失敗したら戻さない(tmp_path: Path) -> None:
    from rover_deploy.remote import TransferFailed

    local = tmp_path / "b.tar.gz"
    local.write_bytes(b"x")
    remote = remote_with()
    remote.send_error = TransferFailed()
    s = make_setup(tmp_path, remote)
    assert s.run("restore", "--file", str(local)) == ExitCode.ABORTED
    assert "restore" not in s.remote.agent_names()


def test_止めてから戻したときはサービスの起動を案内する(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(restore=restore_ok(stopped=[], started=[])))
    assert s.run("restore", DEPLOY_ID) == ExitCode.OK
    assert "サービスは止まったままです。rover-deploy back で普段の版を起動してください" in s.text


@pytest.mark.parametrize(
    ("reason", "extra", "code", "expected"),
    [
        ("sample_running", {}, ExitCode.ABORTED, "rover-deploy stop で止めてください"),
        ("driving", {}, ExitCode.ABORTED, "走行中"),
        ("no_backup", {}, ExitCode.ABORTED, "rover-deploy backups で確かめてください"),
        ("backup_failed", {}, ExitCode.ABORTED, "バックアップを作れません"),
        ("bad_backup_file", {}, ExitCode.ABORTED, "送ったファイルを実機で受け付けられません"),
        ("stop_failed", {}, ExitCode.ABORTED, "サービスを止められません"),
        (
            "restore_failed",
            {"stopped": ["osekkai.service"], "started": ["osekkai.service"]},
            ExitCode.ABORTED,
            "戻せませんでした",
        ),
        (
            "restore_failed",
            {"stopped": ["osekkai.service"], "started": []},
            ExitCode.HALTED,
            "人の手が要ります",
        ),
        ("restore_leftover", {}, ExitCode.HALTED, "人の手が要ります"),
        (
            "start_failed_after",
            {"stopped": ["osekkai.service"], "started": []},
            ExitCode.HALTED,
            "サービスを起動できません",
        ),
    ],
)
def test_戻せなければ理由と終了コードを出す(
    tmp_path: Path, reason: str, extra: dict[str, object], code: int, expected: str
) -> None:
    s = make_setup(tmp_path, remote_with(restore=ng(reason, **extra)))
    assert s.run("restore", DEPLOY_ID) == code
    assert expected in s.text


def test_使い方の誤りで返った理由も出す(tmp_path: Path) -> None:
    reply = AgentReply(2, {"ok": False, "reason": "bad_backup_file"})
    s = make_setup(tmp_path, remote_with(restore=reply))
    assert s.run("restore", DEPLOY_ID) == ExitCode.ABORTED
    assert "送ったファイルを実機で受け付けられません" in s.text


def test_restoreは識別子かファイルのどちらかが要る() -> None:
    from rover_deploy import devcli

    with pytest.raises(SystemExit) as info:
        devcli.main(["restore"])
    assert info.value.code == 2
    with pytest.raises(SystemExit) as info:
        devcli.main(["restore", DEPLOY_ID, "--file", "x"])
    assert info.value.code == 2


# --- 停止 ---


def test_停止はモーターの停止まで確かめて0(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(stop=ok(failed=[])))
    assert s.run("stop") == ExitCode.OK
    assert "サービスを止め、モーターが止まったことを確かめました" in s.text


def test_安全停止だけ確かめられなくても12で人の手を案内する(tmp_path: Path) -> None:
    # 版がないときも、版があって安全停止が失敗したときも、同じ答えになる
    reply = AgentReply(0, {"ok": False, "reason": "halt_failed", "failed": ["safe_stop"]})
    s = make_setup(tmp_path, remote_with(stop=reply))
    assert s.run("stop") == ExitCode.HALTED
    assert (
        "サービスは止めました。モーターが止まったことを確かめられませんでした"
        "（版がないか、安全停止が失敗しました）。実機のモーターが止まっているかを確かめてください"
    ) in s.text
    assert "人の手が要ります" in s.text


def test_サービスを止められなければ12(tmp_path: Path) -> None:
    reply = AgentReply(
        0, {"ok": False, "reason": "halt_failed", "failed": ["stop_core", "safe_stop"]}
    )
    s = make_setup(tmp_path, remote_with(stop=reply))
    assert s.run("stop") == ExitCode.HALTED
    assert "止められなかったもの: 新しい基盤のサービス、安全停止" in s.text
    assert "人の手が要ります" in s.text


def test_停止でロックが取られていれば10(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(stop=ng("lock_busy")))
    assert s.run("stop") == ExitCode.ABORTED


# --- 記録の表示 ---


def test_記録は手順と結果だけを出し理由は出さない(tmp_path: Path) -> None:
    events = [
        {"t": NOW, "step": "prepare", "status": "start", "reason": None},
        {"t": NOW, "step": "prepare", "status": "ok", "reason": None},
        {"t": NOW, "step": "verify", "status": "failed", "reason": "secret-reason"},
    ]
    result = {
        "deploy_id": DEPLOY_ID,
        "outcome": "rolled_back",
        "running": target_to_dict(SAMPLE),
        "failed_checks": ["verify"],
    }
    s = make_setup(tmp_path, remote_with(log=ok(deploy_id=DEPLOY_ID, events=events, result=result)))
    assert s.run("log") == ExitCode.OK
    assert s.remote.agent_args("log") == ("log",)
    assert f"入れ替え {DEPLOY_ID} の記録" in s.text
    assert "  準備: よい" in s.text
    assert "  確認: 失敗" in s.text
    assert "結果: 切り戻した" in s.text
    assert "secret-reason" not in s.text


def test_記録の識別子を渡す(tmp_path: Path) -> None:
    reply = ok(deploy_id=DEPLOY_ID, events=[], result=None)
    s = make_setup(tmp_path, remote_with(log=reply))
    assert s.run("log", DEPLOY_ID) == ExitCode.OK
    assert s.remote.agent_args("log") == ("log", DEPLOY_ID)
    assert "結果: まだありません" in s.text


def test_記録の一覧を出す(tmp_path: Path) -> None:
    reply = ok(ids=["20261026T073320Z", "20261025T010000Z"])
    s = make_setup(tmp_path, remote_with(log=reply))
    assert s.run("log", "--list") == ExitCode.OK
    assert s.remote.agent_args("log") == ("log", "--list")
    assert "  20261026T073320Z" in s.text and "  20261025T010000Z" in s.text


def test_記録がなければ10(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with(log=ng("no_log")))
    assert s.run("log") == ExitCode.ABORTED
    assert "記録が見つかりません" in s.text


def test_記録の識別子の形が違えば実機に頼まない(tmp_path: Path) -> None:
    s = make_setup(tmp_path, remote_with())
    assert s.run("log", "x;rm") == ExitCode.ABORTED
    assert s.remote.calls == []


# --- 初期設定 ---


def ls_remote(*refs: str) -> FakeRunner:
    run = FakeRunner()
    lines = "".join(f"{SHA}\trefs/tags/{name}\n" for name in refs)
    run.script(["git", "-C"], stdout=lines)
    return run


def provision_reply(**extra: object) -> AgentReply:
    data: dict[str, object] = {
        "skipped": ["os"],
        "applied": ["apt", "self_check"],
        "failed_step": None,
        "retry_hint": "",
        "needs_reboot": False,
        "self_check_ok": True,
    }
    data.update(extra)
    return ok(**data)


def test_初期設定は最新のタグを解決して端末を割り当てて呼ぶ(tmp_path: Path) -> None:
    run = ls_remote("v0.1.0", "v0.10.0", "v0.2.0", "v0.10.0^{}", "not-a-version")
    remote = FakeRemote(replies={"status": [QUIET_STATUS], "provision": [provision_reply()]})
    s = make_setup(tmp_path, remote, run=run)
    assert s.run("provision") == ExitCode.OK
    assert s.resolved == ["v0.10.0"]
    assert s.remote.agent_names() == ["status", "provision"]
    args = s.remote.agent_args("provision")
    assert args[:2] == ("provision", "--target")
    assert json.loads(args[2]) == target_to_dict(TAG)
    assert args[3:] == ("--repo-url", REPO_URL)
    assert s.remote.ttys == [False, True]  # 端末を割り当てるのは初期設定だけ
    assert s.remote.calls[0][0] == "push"
    assert "初期設定が終わりました" in s.text
    assert REPO_URL not in s.text and HOST not in s.text
    argv = run.argvs[0]
    assert argv[3:] == ("ls-remote", "--tags", "--refs", "origin")


def test_タグがなければmainを使う(tmp_path: Path) -> None:
    s = make_setup(
        tmp_path,
        FakeRemote(replies={"status": [QUIET_STATUS], "provision": [provision_reply()]}),
        run=ls_remote(),
    )
    assert s.run("provision") == ExitCode.OK
    assert s.resolved == ["main"]


def test_版を指定すればタグを探さない(tmp_path: Path) -> None:
    s = make_setup(
        tmp_path, FakeRemote(replies={"status": [QUIET_STATUS], "provision": [provision_reply()]})
    )
    assert s.run("provision", "--ref", "feat/x") == ExitCode.OK
    assert s.resolved == ["feat/x"]


def test_リポジトリに問い合わせられなければ10(tmp_path: Path) -> None:
    run = FakeRunner()
    run.script(["git", "-C"], returncode=128)
    s = make_setup(tmp_path, FakeRemote(), run=run)
    assert s.run("provision") == ExitCode.ABORTED
    assert "リポジトリ（origin）に接続できません" in s.text
    assert s.remote.calls == []


def test_初期設定の失敗は手順とやり直し方を出して10(tmp_path: Path) -> None:
    reply = AgentReply(
        0,
        {
            "ok": False,
            "reason": "provision_failed",
            "skipped": [],
            "applied": ["os"],
            "failed_step": "apt",
            "retry_hint": "ネットワークを確かめてから、もう一度 provision を実行します",
            "needs_reboot": False,
            "self_check_ok": None,
        },
    )
    s = make_setup(
        tmp_path,
        FakeRemote(replies={"status": [QUIET_STATUS], "provision": [reply]}),
        run=ls_remote("v0.2.0"),
    )
    assert s.run("provision") == ExitCode.ABORTED
    assert "失敗した手順: apt" in s.text
    assert "やり直し方: ネットワークを確かめてから、もう一度 provision を実行します" in s.text


def test_再起動が要れば知らせる(tmp_path: Path) -> None:
    reply = provision_reply(needs_reboot=True, self_check_ok=False)
    s = make_setup(
        tmp_path,
        FakeRemote(replies={"status": [QUIET_STATUS], "provision": [reply]}),
        run=ls_remote("v0.2.0"),
    )
    assert s.run("provision") == ExitCode.OK
    assert "再起動が要ります" in s.text
    assert "self-check にだめの項目があります" in s.text


def test_初期設定でロックが取られていれば10(tmp_path: Path) -> None:
    remote = FakeRemote(replies={"status": [QUIET_STATUS], "provision": [ng("lock_busy")]})
    s = make_setup(tmp_path, remote, run=ls_remote("v0.2.0"))
    assert s.run("provision") == ExitCode.ABORTED
    assert "ほかの入れ替えなど" in s.text
