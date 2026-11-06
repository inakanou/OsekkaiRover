"""開発 PC の入れ替えと普段の版に戻す流れのテスト。

要件: 1.5、1.6、1.10、2.1〜2.4、3.2、3.3、4.3、4.5、5.1、9.2、10.3
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from devcli_fakes import (
    DEPLOY_ID,
    HOST,
    NOW,
    REPO_URL,
    SAMPLE,
    TAG,
    FakeRemote,
    Reply,
    make_setup,
    ng,
    ok,
)

from rover_deploy import devcli
from rover_deploy.common import ExitCode, Target, target_to_dict
from rover_deploy.devconfig import ConfigError
from rover_deploy.refs import RefNotFound, Resolution
from rover_deploy.remote import (
    AgentReply,
    DeviceDisconnected,
    DeviceUnreachable,
    StreamProtocolError,
    TransferFailed,
)

REMOTE_BACKUP = f"/home/someone/.local/state/osekkai-deploy/backups/backup-{DEPLOY_ID}.tar.gz"
REMOTE_SAMPLE = f"/home/someone/.local/state/osekkai-deploy/backups/sample-{DEPLOY_ID}.tar.gz"


def preflight_reply(
    *,
    everyday: Target | None = SAMPLE,
    reasons: list[str] | None = None,
    extra: Mapping[str, object] | None = None,
) -> AgentReply:
    reasons = reasons or []
    data: dict[str, object] = {
        "ok": not reasons,
        "reason": reasons[0] if reasons else "",
        "reasons": reasons,
        "running": "sample",
        "current": target_to_dict(SAMPLE),
        "current_label": "sample",
        "everyday": None if everyday is None else target_to_dict(everyday),
        "target": target_to_dict(TAG),
        "target_label": "v0.2.0",
        "safety": {"ok": True, "reason": "", "busy": []},
        "disk": {"ok": True, "free_bytes": 10 * 1024**3, "needed_bytes": 1024**3},
        "dirty": 0,
        "lock": "free",
        "interrupted": None,
        "first_deploy": False,
    }
    data.update(extra or {})
    return AgentReply(0, data)


def backup_reply(*, first: bool = False) -> AgentReply:
    return ok(
        first_deploy=first,
        backup={"id": DEPLOY_ID, "path": REMOTE_BACKUP, "size_bytes": 2 * 1024 * 1024},
        sample_backup=(
            {"id": DEPLOY_ID, "path": REMOTE_SAMPLE, "size_bytes": 1024 * 1024} if first else None
        ),
    )


def result_line(
    outcome: str, running: Target | None, failed: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "t": NOW,
        "result": {
            "deploy_id": DEPLOY_ID,
            "outcome": outcome,
            "running": None if running is None else target_to_dict(running),
            "failed_checks": list(failed),
        },
    }


def step(name: str, status: str, reason: str | None = None) -> dict[str, object]:
    return {"t": NOW, "step": name, "status": status, "reason": reason}


def happy_remote(**overrides: object) -> FakeRemote:
    replies: dict[str, list[Reply]] = {
        "preflight": [preflight_reply()],
        "backup": [backup_reply()],
        "mark-sample-saved": [ok(sample_saved=DEPLOY_ID)],
        "apply": [ok(started=True, deploy_id=DEPLOY_ID)],
        "status": [ok(everyday=target_to_dict(SAMPLE), everyday_label="sample")],
    }
    lines: list[dict[str, object] | BaseException] = [
        step("prepare", "start"),
        step("prepare", "ok"),
        step("switch", "ok"),
        step("verify", "ok"),
        result_line("succeeded", TAG),
    ]
    remote = FakeRemote(replies=replies, lines=lines)
    for key, value in overrides.items():
        setattr(remote, key, value)
    return remote


# --- 成功 ---


def test_入れ替えが成功すれば版を表示して0で終える(tmp_path: Path) -> None:
    s = make_setup(tmp_path, happy_remote())
    assert s.run("deploy", "v0.2.0") == ExitCode.OK
    assert "今の版: sample / これからの版: v0.2.0" in s.text
    assert "結果: 成功" in s.text
    assert "動いている版: v0.2.0" in s.text
    assert s.remote.agent_names() == ["status", "preflight", "backup", "apply"]
    assert s.hosts == [HOST]
    # 準備・切り替え・確認の手順が短く出る。始まりの行は出さない
    assert "  準備: よい" in s.text
    assert "  確認: よい" in s.text
    assert s.text.count("準備") == 1


def test_入れ替えの引数に版と識別子とURLを渡す(tmp_path: Path) -> None:
    s = make_setup(tmp_path, happy_remote())
    s.run("deploy", "v0.2.0")
    target_json = json.loads(s.remote.agent_args("preflight")[2])
    assert target_json == target_to_dict(TAG)
    assert s.remote.agent_args("backup") == ("backup", "--deploy-id", DEPLOY_ID)
    apply = s.remote.agent_args("apply")
    assert apply[:5] == (
        "apply",
        "--target",
        s.remote.agent_args("preflight")[2],
        "--deploy-id",
        DEPLOY_ID,
    )
    assert "--everyday" not in apply  # 普段の版はサンプルなので、試しの版
    assert apply[-2:] == ("--repo-url", REPO_URL)
    stream = [c for c in s.remote.calls if c[0] == "stream"]
    assert stream[0][1] == ("follow", DEPLOY_ID)


def test_入れる版が普段の版なら有効にする印を付ける(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["preflight"] = [preflight_reply(everyday=TAG)]
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.OK
    assert "--everyday" in s.remote.agent_args("apply")


def test_今の版とこれからの版をバックアップより前に表示する(tmp_path: Path) -> None:
    s = make_setup(tmp_path, happy_remote())
    s.run("deploy", "v0.2.0")
    line_pos = s.text.index("今の版:")
    backup_call = next(c for c in s.remote.calls if c[0] == "agent" and c[1][0] == "backup")  # type: ignore[index]
    assert isinstance(backup_call[-1], int) and line_pos < backup_call[-1]


def test_タグとブランチの両方にあればその旨を出す(tmp_path: Path) -> None:
    s = make_setup(tmp_path, happy_remote(), resolution=Resolution(TAG, also_branch=True))
    assert s.run("deploy", "v0.2.0") == ExitCode.OK
    assert "tag と branch の両方にあります。tag を使います" in s.text


def test_バックアップは開発PCの場所と大きさだけを出す(tmp_path: Path) -> None:
    s = make_setup(tmp_path, happy_remote())
    s.run("deploy", "v0.2.0")
    local = tmp_path / "backups" / DEPLOY_ID / f"backup-{DEPLOY_ID}.tar.gz"
    assert local.is_file()
    assert f"バックアップを受け取りました: {local}（3.0 MB）" in s.text
    assert "/home/someone" not in s.text  # 実機の置き場所は出さない
    fetch = [c for c in s.remote.calls if c[0] == "fetch"]
    assert fetch[0][1:3] == (REMOTE_BACKUP, local)


def test_表示に接続先とURLを出さない(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.lines = [
        step("verify", "failed", "self_check:tts"),
        result_line("rolled_back", SAMPLE, ("self_check:tts",)),
    ]
    s = make_setup(tmp_path, remote)
    s.run("deploy", "v0.2.0")
    assert HOST not in s.text
    assert REPO_URL not in s.text
    assert DEPLOY_ID in s.text  # 識別子は出してよい


# --- 初回 ---


def test_初回はサンプルのバックアップも受け取って記録する(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["backup"] = [backup_reply(first=True)]
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.OK
    fetched = [c[1] for c in s.remote.calls if c[0] == "fetch"]
    assert fetched == [REMOTE_BACKUP, REMOTE_SAMPLE]
    assert s.remote.agent_names() == ["status", "preflight", "backup", "mark-sample-saved", "apply"]
    assert s.remote.agent_args("mark-sample-saved") == ("mark-sample-saved", DEPLOY_ID)
    assert "サンプルのバックアップを受け取りました" in s.text


def test_サンプルの記録に失敗したら適用しない(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["backup"] = [backup_reply(first=True)]
    remote.replies["mark-sample-saved"] = [ng("lock_busy")]
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "apply" not in s.remote.agent_names()


def test_初回なのにサンプルのバックアップがなければ中止する(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["backup"] = [
        ok(
            first_deploy=True,
            backup={"id": DEPLOY_ID, "path": REMOTE_BACKUP, "size_bytes": 1},
            sample_backup=None,
        )
    ]
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "apply" not in s.remote.agent_names()


# --- 中止 ---


@pytest.mark.parametrize(
    ("reasons", "extra", "expected"),
    [
        (["driving"], {}, "走行中"),
        (
            ["busy"],
            {"safety": {"ok": False, "reason": "busy", "busy": ["patrol", "talk"]}},
            "作業中の機能: patrol、talk",
        ),
        (["unknown"], {}, "確かめられません"),
        (
            ["sample_running"],
            {},
            "サンプルが動いています。使っていないことを確かめてから"
            " rover-deploy stop で止めてください",
        ),
        (
            ["disk_low"],
            {"disk": {"ok": False, "free_bytes": 100 * 1024**2, "needed_bytes": 600 * 1024**2}},
            "空き: 100.0 MB、要る: 600.0 MB",
        ),
        (["dirty"], {"dirty": 3}, "手の変更があります"),
        (["lock_busy"], {}, "ほかの入れ替えなど"),
        (
            ["interrupted"],
            {},
            "前の入れ替えが途中で終わっています。"
            "rover-deploy status で確かめ、rover-deploy back で戻してください",
        ),
        (["state_broken"], {}, "状態のファイルが読めません"),
    ],
)
def test_事前の確認で中止の理由を出して10で終える(
    tmp_path: Path, reasons: list[str], extra: dict[str, object], expected: str
) -> None:
    remote = happy_remote()
    remote.replies["preflight"] = [preflight_reply(reasons=reasons, extra=extra)]
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert expected in s.text
    assert "今の版: sample / これからの版: v0.2.0" in s.text
    assert s.remote.agent_names() == ["status", "preflight"]


def test_中止の理由が複数あればすべて出す(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["preflight"] = [preflight_reply(reasons=["lock_busy", "driving"])]
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "ほかの入れ替えなど" in s.text and "走行中" in s.text


def test_今の版が分からなければ動いている様子で表す(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["preflight"] = [
        preflight_reply(
            reasons=["unknown"],
            extra={"running": "unknown", "current": None, "current_label": None},
        )
    ]
    s = make_setup(tmp_path, remote)
    s.run("deploy", "v0.2.0")
    assert "今の版: 分かりません / これからの版: v0.2.0" in s.text


def test_実機の道具が思わぬ形で終われば中止する(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["preflight"] = [
        AgentReply(1, {"ok": False, "reason": "unexpected", "error": "OSError"})
    ]
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert s.remote.agent_names() == ["status", "preflight"]


def test_接続できなければ10で終える(tmp_path: Path) -> None:
    s = make_setup(tmp_path, happy_remote(push_error=DeviceUnreachable()))
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "実機に接続できません" in s.text
    assert s.remote.agent_names() == []
    assert HOST not in s.text


def test_設定の誤りは10で終える(tmp_path: Path) -> None:
    s = make_setup(
        tmp_path, happy_remote(), config_error=ConfigError("実機の接続先が設定されていません")
    )
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "実機の接続先が設定されていません" in s.text
    assert s.remote.calls == []


def test_版がなければ10で終える(tmp_path: Path) -> None:
    s = make_setup(tmp_path, happy_remote(), resolution=RefNotFound("v9.9.9"))
    assert s.run("deploy", "v9.9.9") == ExitCode.ABORTED
    assert "見つかりません" in s.text
    assert s.remote.calls == []


def test_バックアップを作れなければ適用しない(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["backup"] = [ng("backup_failed")]
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "バックアップを作れません" in s.text
    assert "apply" not in s.remote.agent_names()


def test_バックアップを受け取れなければ適用しない(tmp_path: Path) -> None:
    s = make_setup(tmp_path, happy_remote(fetch_error=TransferFailed()))
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "ファイルのやり取りに失敗しました" in s.text
    assert "apply" not in s.remote.agent_names()


def test_バックアップの置き場所の形が違えば中止する(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["backup"] = [
        ok(
            first_deploy=False,
            backup={"id": DEPLOY_ID, "path": "/x/..", "size_bytes": 1},
            sample_backup=None,
        )
    ]
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert [c for c in s.remote.calls if c[0] == "fetch"] == []


def test_適用を始められなければ10で終える(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["apply"] = [ng("apply_running")]
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "入れ替えがもう動いています" in s.text
    assert [c for c in s.remote.calls if c[0] == "stream"] == []


def test_適用を頼む途中で切れたら13で終える(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["apply"] = [DeviceUnreachable()]
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.UNKNOWN
    assert "rover-deploy status" in s.text


# --- 追跡と結果 ---


@pytest.mark.parametrize(
    "lines",
    [
        [step("prepare", "ok"), DeviceDisconnected()],
        [step("prepare", "ok"), StreamProtocolError()],
        [step("prepare", "ok"), {"error": "ended_without_result"}],
        [{"error": "timeout"}],
        [{"error": "no_log"}],
        [{"error": "broken_result"}],
        [{"ok": False, "reason": "bad_deploy_id"}],
        [{"t": NOW, "result": {"outcome": "nope"}}],
        [step("prepare", "ok")],  # 結果の行がないまま終わった
    ],
)
def test_追跡の途中で切れたら13で版の確認を案内する(
    tmp_path: Path, lines: list[dict[str, object] | BaseException]
) -> None:
    s = make_setup(tmp_path, happy_remote(lines=lines))
    assert s.run("deploy", "v0.2.0") == ExitCode.UNKNOWN
    assert "接続が切れました。結果は rover-deploy status で確かめてください" in s.text
    assert "結果:" not in s.text


def test_切り戻したら11で失敗の項目を出す(tmp_path: Path) -> None:
    lines: list[dict[str, object] | BaseException] = [
        step("verify", "failed", "self_check:tts"),
        step("rollback", "start"),
        step("rollback.switch", "ok"),
        step("rollback", "ok"),
        result_line("rolled_back", SAMPLE, ("self_check:tts", "alive")),
    ]
    s = make_setup(tmp_path, happy_remote(lines=lines))
    assert s.run("deploy", "v0.2.0") == ExitCode.ROLLED_BACK
    assert "結果: 切り戻した" in s.text
    assert "動いている版: sample" in s.text
    assert "失敗した確認: self_check:tts, alive" in s.text
    assert "  確認: 失敗" in s.text
    assert "  切り戻しの切り替え: よい" in s.text


def test_止めたら12で人の手が要ると出す(tmp_path: Path) -> None:
    lines: list[dict[str, object] | BaseException] = [
        result_line("halted", None, ("start", "rollback:alive")),
    ]
    s = make_setup(tmp_path, happy_remote(lines=lines))
    assert s.run("deploy", "v0.2.0") == ExitCode.HALTED
    assert "結果: 止めた" in s.text
    assert "動いている版: なし" in s.text
    assert "失敗した確認: start, rollback:alive" in s.text
    assert "人の手が要ります。docs/deploy.md の" in s.text


def test_適用の側の中止は10で理由を出す(tmp_path: Path) -> None:
    lines: list[dict[str, object] | BaseException] = [
        result_line("aborted", SAMPLE, ("safety:driving",))
    ]
    s = make_setup(tmp_path, happy_remote(lines=lines))
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "結果: 中止" in s.text
    assert "中止の理由: 実機が走行中です。止まってからやり直してください" in s.text
    assert "safety:driving" not in s.text


def test_適用の側の準備の中止は人向けの文言で出す(tmp_path: Path) -> None:
    lines: list[dict[str, object] | BaseException] = [
        result_line("aborted", SAMPLE, ("prepare:unit",))
    ]
    s = make_setup(tmp_path, happy_remote(lines=lines))
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "中止の理由: 準備: その版には新しい基盤のユニットがありません" in s.text
    assert "prepare:unit" not in s.text


def test_適用の側の知らない中止の理由は名前を一度だけ出す(tmp_path: Path) -> None:
    lines: list[dict[str, object] | BaseException] = [
        result_line("aborted", SAMPLE, ("safety:weird", "odd"))
    ]
    s = make_setup(tmp_path, happy_remote(lines=lines))
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "中止の理由: weird" in s.text
    assert "中止の理由: odd" in s.text
    assert "中止の理由: 中止の理由" not in s.text


def test_適用の側の作業中の中止は機能の名前をまとめて出す(tmp_path: Path) -> None:
    lines: list[dict[str, object] | BaseException] = [
        result_line("aborted", SAMPLE, ("safety:busy", "busy:daruma", "busy:greeting"))
    ]
    s = make_setup(tmp_path, happy_remote(lines=lines))
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "中止の理由: 実機で作業中の機能があります（作業中の機能: daruma、greeting）" in s.text
    assert "busy:" not in s.text


def test_成功しても自動の起動の設定ができなければ警告する(tmp_path: Path) -> None:
    lines: list[dict[str, object] | BaseException] = [result_line("succeeded", TAG, ("enable",))]
    s = make_setup(tmp_path, happy_remote(lines=lines))
    assert s.run("deploy", "v0.2.0") == ExitCode.OK
    assert "自動の起動の設定ができませんでした" in s.text
    assert "失敗した確認" not in s.text


def test_記録の理由は表示に写さない(tmp_path: Path) -> None:
    lines: list[dict[str, object] | BaseException] = [
        step("prepare", "failed", "準備に失敗: SecretError"),
        result_line("aborted", SAMPLE, ("prepare",)),
    ]
    s = make_setup(tmp_path, happy_remote(lines=lines))
    s.run("deploy", "v0.2.0")
    assert "SecretError" not in s.text
    assert "  準備: 失敗" in s.text


# --- 普段の版に戻す ---


def test_普段の版に戻すときは普段の版を入れて有効にする(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["preflight"] = [
        preflight_reply(
            extra={"running": "core", "current": target_to_dict(TAG), "current_label": "v0.2.0"}
        )
    ]
    remote.lines = [result_line("succeeded", SAMPLE)]
    s = make_setup(tmp_path, remote, resolution=AssertionError("back では版を解決しない"))
    assert s.run("back") == ExitCode.OK
    assert s.remote.agent_names() == ["status", "preflight", "backup", "apply"]
    assert json.loads(s.remote.agent_args("preflight")[2]) == target_to_dict(SAMPLE)
    apply = s.remote.agent_args("apply")
    assert json.loads(apply[2]) == target_to_dict(SAMPLE)
    assert "--everyday" in apply
    assert "今の版: v0.2.0 / これからの版: sample" in s.text
    assert "動いている版: sample" in s.text
    # 道具を写してから実機に問い合わせる
    assert s.remote.calls[0][0] == "push"


def test_普段の版が決まっていなければ10で案内する(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["status"] = [ok(everyday=None, everyday_label=None)]
    s = make_setup(tmp_path, remote)
    assert s.run("back") == ExitCode.ABORTED
    assert "普段の版がまだ決まっていません。rover-deploy everyday" in s.text
    assert s.remote.agent_names() == ["status"]


def test_普段の版に戻すときも事前の確認で中止する(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["preflight"] = [
        preflight_reply(
            reasons=["driving"],
            extra={"running": "core", "current": None, "current_label": None},
        )
    ]
    s = make_setup(tmp_path, remote)
    assert s.run("back") == ExitCode.ABORTED
    assert "走行中" in s.text
    assert "backup" not in s.remote.agent_names()


def test_途中で終わった入れ替えは普段の版に戻して片付ける(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["preflight"] = [
        preflight_reply(
            reasons=["interrupted"],
            extra={"running": "core", "current": None, "current_label": None},
        )
    ]
    remote.lines = [result_line("succeeded", SAMPLE)]
    s = make_setup(tmp_path, remote)
    assert s.run("back") == ExitCode.OK
    assert "前の入れ替えが途中で終わっています。普段の版に戻します" in s.text
    assert s.remote.agent_names() == ["status", "preflight", "backup", "apply"]
    assert "--everyday" in s.remote.agent_args("apply")


def test_途中で終わった入れ替えでもほかの理由があれば中止する(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["preflight"] = [
        preflight_reply(
            reasons=["interrupted", "driving"],
            extra={"running": "core", "current": None, "current_label": None},
        )
    ]
    s = make_setup(tmp_path, remote)
    assert s.run("back") == ExitCode.ABORTED
    assert "走行中" in s.text
    assert "普段の版に戻します" not in s.text
    assert "backup" not in s.remote.agent_names()


def test_入れ替えでは途中で終わった入れ替えで中止しbackを案内する(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["preflight"] = [preflight_reply(reasons=["interrupted"])]
    s = make_setup(tmp_path, remote)
    assert s.run("deploy", "v0.2.0") == ExitCode.ABORTED
    assert "rover-deploy back で戻してください" in s.text
    assert "backup" not in s.remote.agent_names()


def test_もうサンプルで動いていれば何も変えずに0で終える(tmp_path: Path) -> None:
    remote = happy_remote()
    remote.replies["preflight"] = [preflight_reply(reasons=["sample_running"])]
    s = make_setup(tmp_path, remote)
    assert s.run("back") == ExitCode.OK
    assert "もう普段の版（サンプル）で動いています" in s.text
    assert s.remote.agent_names() == ["status", "preflight"]
    assert [c for c in s.remote.calls if c[0] in ("fetch", "stream")] == []


def test_普段の版に戻すときも接続できなければ10(tmp_path: Path) -> None:
    s = make_setup(tmp_path, happy_remote(push_error=DeviceUnreachable()))
    assert s.run("back") == ExitCode.ABORTED
    assert "実機に接続できません" in s.text


def test_普段の版に戻すときも設定の誤りは10(tmp_path: Path) -> None:
    s = make_setup(tmp_path, happy_remote(), config_error=ConfigError("設定が読めません"))
    assert s.run("back") == ExitCode.ABORTED
    assert s.remote.calls == []


# --- 入口 ---


def test_サブコマンドがなければ使い方の誤り(capsys: pytest.CaptureFixture[str]) -> None:
    assert devcli.main([]) == 2
    assert "rover-deploy" in capsys.readouterr().err


def test_deployは版の名前が要る() -> None:
    with pytest.raises(SystemExit) as info:
        devcli.main(["deploy"])
    assert info.value.code == 2
