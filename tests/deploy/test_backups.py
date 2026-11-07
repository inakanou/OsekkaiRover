"""バックアップの作成・世代の削除・一覧・復元のテスト（3.1、3.2、3.4、3.5、3.7、9.2）。"""

import io
import os
import stat
import tarfile
from pathlib import Path

import pytest
from fake_runner import FakeRunner

from rover_deploy.backups import (
    KEEP_BACKUPS,
    BackupFailed,
    RestoreFailed,
    backup_path,
    core_data_dir,
    create_backup,
    create_sample_backup,
    estimate_backup_bytes,
    list_backups,
    prune,
    recover_interrupted_restore,
    restore_backup,
)
from rover_deploy.layout import Layout
from rover_deploy.system import CommandFailed

ID_A = "20261104T210000Z"
ID_B = "20261105T090000Z"
HIDDEN = "ひみつのキー"  # ファイルの中身。例外の文言に出てはいけない
FREEZE = b"osekkai==0.1.0\nnumpy==1.26.4\n"


def make_layout(home: Path) -> Layout:
    return Layout({}, home=home, uid=1000)


def write(path: Path, text: str, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def populate(home: Path) -> Layout:
    """実機に近い置き場所を作る（research.md の調べ）。"""
    layout = make_layout(home)
    write(layout.core_config_dir / "env", f"API_KEY={HIDDEN}\n", 0o600)
    write(layout.core_config_dir / "config.yaml", "neck: {}\n")
    write(layout.core_state_dir / "kids/records.jsonl", "{}\n")
    data = core_data_dir(layout, {})
    write(data / "faces/embeddings.npy", "face")
    write(data / "voices/a.wav", "voice")
    write(data / "models/yunet.onnx", "model")
    write(data / "jtalk/dic/sys.dic", "dic")
    sample = layout.sample_dir
    write(sample / ".git/HEAD", "ref: refs/heads/main\n")
    write(sample / ".git/refs/heads/pi-only", "0" * 40 + "\n")
    write(sample / "src/main.py", "print()\n")
    write(sample / "data/picar-x.conf", "# 注釈\n")
    write(sample / "data/records.json", "{}\n")
    write(sample / "data/tts_cache/x.wav", "cache")
    write(layout.sample_unit_file, "[Service]\n")
    write(layout.sample_venv / "bin/pip", "#!/bin/sh\n", 0o755)
    return layout


def names(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as tar:
        return set(tar.getnames())


def snapshot(root: Path) -> dict[str, bytes]:
    """ディレクトリの下のファイルの中身（比べる用）。"""
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def freeze_runner(layout: Layout, *, returncode: int = 0) -> FakeRunner:
    fake = FakeRunner()
    fake.script(
        [str(layout.sample_venv / "bin/pip"), "freeze"], returncode=returncode, stdout=FREEZE
    )
    return fake


# --- 作成（3.1、3.4） ---


def test_中身は設定と記録とデータとサンプルのデータ(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    info = create_backup(layout, ID_A)
    assert info.backup_id == ID_A
    assert info.path == backup_path(layout, ID_A)
    assert info.path.parent == layout.backups_dir
    assert info.size_bytes == info.path.stat().st_size
    members = names(info.path)
    assert "config/env" in members
    assert "config/config.yaml" in members
    assert "state/kids/records.jsonl" in members
    assert "data/faces/embeddings.npy" in members
    assert "sample-data/picar-x.conf" in members
    assert "sample-data/records.json" in members


def test_取り直せる素材と声のキャッシュは入れない(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    members = names(create_backup(layout, ID_A).path)
    for excluded in ("voices", "models", "jtalk"):
        assert not any(
            m == f"data/{excluded}" or m.startswith(f"data/{excluded}/") for m in members
        )
    assert not any(m.startswith("sample-data/tts_cache") for m in members)
    # サンプルのコードは普段のバックアップに入れない
    assert not any(m.startswith("sample-data/../") or "src/main.py" in m for m in members)


def test_権限はファイル_0600_ディレクトリ_0700(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    info = create_backup(layout, ID_A)
    assert stat.S_IMODE(info.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(layout.backups_dir.stat().st_mode) == 0o700
    # 一時ファイルは残さない
    assert [p.name for p in layout.backups_dir.iterdir()] == [info.path.name]


def test_ない置き場所は飛ばす(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    write(layout.core_config_dir / "env", "x", 0o600)
    members = names(create_backup(layout, ID_A).path)
    assert "config/env" in members
    assert not any(m.split("/")[0] in {"state", "data", "sample-data"} for m in members)


def test_XDG_DATA_HOME_を使う(tmp_path: Path) -> None:
    layout = make_layout(tmp_path / "home")
    assert core_data_dir(layout, {}) == tmp_path / "home/.local/share/osekkai"
    assert core_data_dir(layout, {"XDG_DATA_HOME": str(tmp_path / "d")}) == tmp_path / "d/osekkai"
    assert core_data_dir(layout, {"XDG_DATA_HOME": "rel"}) == tmp_path / "home/.local/share/osekkai"
    write(tmp_path / "d/osekkai/faces/f", "x")
    info = create_backup(layout, ID_A, env={"XDG_DATA_HOME": str(tmp_path / "d")})
    assert "data/faces/f" in names(info.path)


def test_記号のリンクは入れない(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    outside = write(tmp_path / "outside/hidden.txt", HIDDEN)
    (layout.core_config_dir / "link").symlink_to(outside)
    members = names(create_backup(layout, ID_A).path)
    assert "config/link" not in members


def test_作れなければ専用の例外で中身を出さない(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    layout.state_dir.mkdir(parents=True)
    write(layout.backups_dir, "ディレクトリでなくファイル")  # 置き場所を作れない
    with pytest.raises(BackupFailed) as caught:
        create_backup(layout, ID_A)
    assert HIDDEN not in str(caught.value)


def test_読めないファイルがあれば失敗して一時ファイルを残さない(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root では読めないファイルを作れない")
    layout = populate(tmp_path)
    unreadable = write(layout.core_state_dir / "locked.jsonl", HIDDEN, 0o000)
    try:
        with pytest.raises(BackupFailed):
            create_backup(layout, ID_A)
    finally:
        os.chmod(unreadable, 0o600)
    assert list(layout.backups_dir.iterdir()) == []


def test_識別子の形を確かめる(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    with pytest.raises(ValueError):
        create_backup(layout, "../x")


# --- サンプルのバックアップ（9.2） ---


def test_サンプルのバックアップは作業コピー全体とユニットと_pip_freeze(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    before = snapshot(layout.sample_dir)
    fake = freeze_runner(layout)
    info = create_sample_backup(layout, ID_A, fake)
    assert info is not None
    assert info.backup_id == ID_A
    assert info.path.parent == layout.backups_dir
    assert stat.S_IMODE(info.path.stat().st_mode) == 0o600
    members = names(info.path)
    assert "sample/.git/HEAD" in members
    assert "sample/.git/refs/heads/pi-only" in members
    assert "sample/src/main.py" in members
    assert "sample/data/records.json" in members
    assert "sample/data/tts_cache/x.wav" in members  # 作業コピー全体
    assert "sample-unit/osekkai-rover.service" in members
    assert "pip-freeze.txt" in members
    with tarfile.open(info.path, "r:gz") as tar:
        extracted = tar.extractfile("pip-freeze.txt")
        assert extracted is not None
        assert extracted.read() == FREEZE
    # 仮想環境そのものは写さない
    assert not any(m.startswith("sample-venv") or ".venvs" in m for m in members)
    # サンプルの場所には書かない（9.1）
    assert snapshot(layout.sample_dir) == before
    assert fake.argvs == [(str(layout.sample_venv / "bin/pip"), "freeze")]
    # 普段のバックアップの一覧には入らない
    assert list_backups(layout) == []


def test_サンプルがなければ作らない(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    fake = FakeRunner()
    assert create_sample_backup(layout, ID_A, fake) is None
    assert fake.calls == []
    assert not layout.backups_dir.exists()


def test_pip_freeze_が失敗すればサンプルのバックアップも失敗(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    fake = FakeRunner()
    fake.raise_on([str(layout.sample_venv / "bin/pip")], CommandFailed("pip", 1))
    with pytest.raises(BackupFailed):
        create_sample_backup(layout, ID_A, fake)
    assert list(layout.backups_dir.iterdir()) == []


def test_仮想環境がなければ_pip_freeze_を飛ばす(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    (layout.sample_venv / "bin/pip").unlink()
    fake = FakeRunner()
    info = create_sample_backup(layout, ID_A, fake)
    assert info is not None
    assert "pip-freeze.txt" not in names(info.path)
    assert fake.calls == []


# --- 一覧と世代の削除（3.5） ---


def make_ids(count: int) -> list[str]:
    return [f"202611{day:02d}T000000Z" for day in range(1, count + 1)]


def test_一覧は新しい順(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    for backup_id in (ID_B, ID_A):
        create_backup(layout, backup_id)
    write(layout.backups_dir / "unrelated.txt", "x")
    write(layout.backups_dir / "backup-bad.tar.gz", "x")
    listed = list_backups(layout)
    assert [b.backup_id for b in listed] == [ID_B, ID_A]
    assert listed[0].size_bytes == listed[0].path.stat().st_size


def test_一覧は置き場所がなければ空(tmp_path: Path) -> None:
    assert list_backups(make_layout(tmp_path)) == []


def test_決めた世代だけ残して古いものを消す(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    ids = make_ids(KEEP_BACKUPS + 2)
    for backup_id in ids:
        create_backup(layout, backup_id)
    sample = create_sample_backup(layout, ids[0], freeze_runner(layout))
    assert sample is not None
    prune(layout)
    assert KEEP_BACKUPS == 5
    assert [b.backup_id for b in list_backups(layout)] == list(reversed(ids))[:KEEP_BACKUPS]
    # サンプルのバックアップは自動では消さない
    assert sample.path.exists()


def test_残す数を指定できる(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    for backup_id in make_ids(3):
        create_backup(layout, backup_id)
    prune(layout, keep=1)
    assert [b.backup_id for b in list_backups(layout)] == [make_ids(3)[-1]]
    with pytest.raises(ValueError):
        prune(layout, keep=0)


# --- 復元（3.7） ---


def test_元の場所に戻す_サンプルのデータは指定のときだけ(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    data = core_data_dir(layout, {})
    saved_config = snapshot(layout.core_config_dir)
    saved_state = snapshot(layout.core_state_dir)
    saved_sample_data = snapshot(layout.sample_dir / "data")
    info = create_backup(layout, ID_A)

    # バックアップの後に変わった
    write(layout.core_config_dir / "config.yaml", "changed\n")
    write(layout.core_config_dir / "new.yaml", "new\n")
    write(layout.core_state_dir / "kids/records.jsonl", "changed\n")
    write(data / "faces/embeddings.npy", "changed")
    write(layout.sample_dir / "data/records.json", "changed\n")

    restore_backup(layout, info.path, include_sample=False)
    assert snapshot(layout.core_config_dir) == saved_config
    assert snapshot(layout.core_state_dir) == saved_state
    assert (data / "faces/embeddings.npy").read_text() == "face"
    # 取り直せる素材は残る
    assert (data / "voices/a.wav").read_text() == "voice"
    assert (data / "models/yunet.onnx").read_text() == "model"
    assert (data / "jtalk/dic/sys.dic").read_text() == "dic"
    # サンプルのデータは指定がないので変えない
    assert (layout.sample_dir / "data/records.json").read_text() == "changed\n"

    restore_backup(layout, info.path, include_sample=True)
    assert snapshot(layout.sample_dir / "data") == saved_sample_data  # tts_cache も残る
    # 権限も戻る
    assert stat.S_IMODE((layout.core_config_dir / "env").stat().st_mode) == 0o600
    # 一時のディレクトリを残さない
    assert sorted(p.name for p in layout.core_config_dir.parent.iterdir()) == ["osekkai", "systemd"]
    assert sorted(p.name for p in layout.sample_dir.iterdir()) == [".git", "data", "src"]


def test_消えていた置き場所も作り直す(tmp_path: Path) -> None:
    import shutil

    layout = populate(tmp_path)
    saved = snapshot(layout.core_state_dir)
    info = create_backup(layout, ID_A)
    shutil.rmtree(layout.core_state_dir)
    restore_backup(layout, info.path, include_sample=False)
    assert snapshot(layout.core_state_dir) == saved
    assert stat.S_IMODE(layout.core_state_dir.stat().st_mode) == 0o700


def test_バックアップになかった置き場所は触らない(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    write(layout.core_config_dir / "env", "x", 0o600)
    info = create_backup(layout, ID_A)
    write(layout.core_state_dir / "later.jsonl", "later")
    restore_backup(layout, info.path, include_sample=False)
    assert (layout.core_state_dir / "later.jsonl").read_text() == "later"


def test_サンプルがなければサンプルのデータは戻さない(tmp_path: Path) -> None:
    import shutil

    layout = populate(tmp_path)
    info = create_backup(layout, ID_A)
    shutil.rmtree(layout.sample_dir)
    restore_backup(layout, info.path, include_sample=True)
    assert not layout.sample_dir.exists()


def evil_archive(path: Path, build: "list[tuple[tarfile.TarInfo, bytes | None]]") -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for info, content in build:
            tar.addfile(info, io.BytesIO(content) if content is not None else None)
    return path


def file_member(name: str, content: bytes = b"x") -> "tuple[tarfile.TarInfo, bytes]":
    info = tarfile.TarInfo(name)
    info.size = len(content)
    return info, content


@pytest.mark.parametrize(
    "name",
    ["/etc/evil", "config/../../evil", "../evil", "unknown/x", "config/a/../../evil"],
)
def test_外に出るパスは拒んで何も書かない(tmp_path: Path, name: str) -> None:
    layout = populate(tmp_path)
    saved = snapshot(layout.core_config_dir)
    archive = evil_archive(
        tmp_path / "evil.tar.gz",
        [file_member("config/config.yaml", b"evil"), file_member(name)],
    )
    with pytest.raises(RestoreFailed):
        restore_backup(layout, archive, include_sample=False)
    assert snapshot(layout.core_config_dir) == saved
    assert not (tmp_path / "evil").exists()


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE]
)
def test_リンクや特別なファイルは拒む(tmp_path: Path, kind: bytes) -> None:
    layout = populate(tmp_path)
    saved = snapshot(layout.core_config_dir)
    link = tarfile.TarInfo("config/link")
    link.type = kind
    link.linkname = "/etc/passwd"
    archive = evil_archive(tmp_path / "evil.tar.gz", [(link, None)])
    with pytest.raises(RestoreFailed):
        restore_backup(layout, archive, include_sample=False)
    assert snapshot(layout.core_config_dir) == saved


def test_壊れたファイルは拒む(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    saved = snapshot(layout.core_config_dir)
    broken = write(tmp_path / "broken.tar.gz", "これは tar ではない")
    with pytest.raises(RestoreFailed):
        restore_backup(layout, broken, include_sample=False)
    with pytest.raises(RestoreFailed):
        restore_backup(layout, tmp_path / "missing.tar.gz", include_sample=False)
    assert snapshot(layout.core_config_dir) == saved


def test_サンプルのバックアップは普段の復元に使えない(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    sample = create_sample_backup(layout, ID_A, freeze_runner(layout))
    assert sample is not None
    with pytest.raises(RestoreFailed):
        restore_backup(layout, sample.path, include_sample=True)


# --- 空き容量の見積もり（2.4） ---


def test_見積もりはバックアップに入るファイルの大きさの合計(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    regular = estimate_backup_bytes(layout)
    expected = sum(
        len(text)
        for text in (
            f"API_KEY={HIDDEN}\n".encode(),
            b"neck: {}\n",
            b"{}\n",
            b"face",
            "# 注釈\n".encode(),
            b"{}\n",
        )
    )
    assert regular == expected
    with_sample = estimate_backup_bytes(layout, include_sample=True)
    assert with_sample == regular + sum(len(v) for v in snapshot(layout.sample_dir).values()) + len(
        b"[Service]\n"
    )
    assert estimate_backup_bytes(make_layout(tmp_path / "empty")) == 0


def test_途中で失敗したら入れ替えずに一時のディレクトリを消す(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rover_deploy.backups as backups

    layout = populate(tmp_path)
    info = create_backup(layout, ID_A)
    write(layout.core_config_dir / "config.yaml", "changed\n")
    current = snapshot(layout.core_config_dir)
    real_extract = backups._extract

    def failing(
        tar: tarfile.TarFile, members: list[tarfile.TarInfo], root: str, dest: Path
    ) -> None:
        if root == "data":
            raise OSError("書けない")
        real_extract(tar, members, root, dest)

    monkeypatch.setattr(backups, "_extract", failing)
    with pytest.raises(RestoreFailed):
        restore_backup(layout, info.path, include_sample=False)
    # 書き終える前に失敗したので、どこも入れ替えていない
    assert snapshot(layout.core_config_dir) == current
    for parent in (layout.core_config_dir.parent, layout.core_state_dir.parent):
        assert not any(p.name.endswith((".restore-new", ".restore-old")) for p in parent.iterdir())
    assert not any(
        p.name.endswith(".restore-new") for p in core_data_dir(layout, {}).parent.iterdir()
    )


# --- 取り直せる素材を復元でなくさない ---


def assets_intact(layout: Layout) -> None:
    data = core_data_dir(layout, {})
    assert (data / "voices/a.wav").read_text() == "voice"
    assert (data / "models/yunet.onnx").read_text() == "model"
    assert (data / "jtalk/dic/sys.dic").read_text() == "dic"


def no_leftovers(layout: Layout) -> None:
    data = core_data_dir(layout, {})
    for parent in (layout.core_config_dir.parent, layout.core_state_dir.parent, data.parent):
        assert not any(p.name.endswith((".restore-new", ".restore-old")) for p in parent.iterdir())


@pytest.mark.parametrize(
    "name",
    ["data/voices/b", "data/models", "data/jtalk/x", "sample-data/tts_cache/y"],
)
def test_除いた素材の名前を含むバックアップは拒む(tmp_path: Path, name: str) -> None:
    layout = populate(tmp_path)
    saved = snapshot(core_data_dir(layout, {}))
    archive = evil_archive(
        tmp_path / "evil.tar.gz",
        [file_member("data/faces/embeddings.npy", b"evil"), file_member(name)],
    )
    with pytest.raises(RestoreFailed):
        restore_backup(layout, archive, include_sample=True)
    assert snapshot(core_data_dir(layout, {})) == saved
    assert (layout.sample_dir / "data/tts_cache/x.wav").read_text() == "cache"
    no_leftovers(layout)


def test_前の復元の残りから_元の場所がなければ戻す(tmp_path: Path) -> None:
    # 元の場所を残りの名前に移した直後に電源が切れた形
    layout = populate(tmp_path)
    info = create_backup(layout, ID_A)
    data = core_data_dir(layout, {})
    write(data / "faces/embeddings.npy", "changed")
    data.rename(data.with_name("osekkai.restore-old"))
    restore_backup(layout, info.path, include_sample=False)
    assert (data / "faces/embeddings.npy").read_text() == "face"
    assets_intact(layout)
    no_leftovers(layout)


def test_前の復元の残りから_元の場所があれば素材を移してから消す(tmp_path: Path) -> None:
    # 入れ替えた後、素材を移す前に止まった形
    layout = populate(tmp_path)
    info = create_backup(layout, ID_A)
    data = core_data_dir(layout, {})
    old = data.with_name("osekkai.restore-old")
    data.rename(old)
    write(data / "faces/embeddings.npy", "restored")
    restore_backup(layout, info.path, include_sample=False)
    assets_intact(layout)
    no_leftovers(layout)


def test_途中で止まった復元を片付けられる(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    data = core_data_dir(layout, {})
    data.rename(data.with_name("osekkai.restore-old"))
    recover_interrupted_restore(layout, env={})
    assets_intact(layout)
    assert (data / "faces/embeddings.npy").read_text() == "face"
    no_leftovers(layout)
    recover_interrupted_restore(layout, env={})  # 残りがなければ何もしない
    assets_intact(layout)


def test_素材を移せなければ残りに置いたままにする(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = populate(tmp_path)
    info = create_backup(layout, ID_A)
    data = core_data_dir(layout, {})
    old = data.with_name("osekkai.restore-old")
    real_rename = os.rename

    def failing(src: "str | os.PathLike[str]", dst: "str | os.PathLike[str]") -> None:
        if Path(src).parent == old and Path(src).name == "voices":
            raise OSError("移せない")
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", failing)
    with pytest.raises(RestoreFailed):
        restore_backup(layout, info.path, include_sample=False)
    assert (old / "voices/a.wav").read_text() == "voice"  # 消していない
    monkeypatch.setattr(os, "rename", real_rename)
    # 次の復元の始めに、残りから素材を移す
    restore_backup(layout, info.path, include_sample=False)
    assets_intact(layout)
    no_leftovers(layout)


def test_書けないディレクトリも戻せる(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    readonly = layout.core_state_dir / "ro"
    write(readonly / "inner/f.txt", "f")
    os.chmod(readonly / "inner", 0o500)
    os.chmod(readonly, 0o500)
    info = create_backup(layout, ID_A)
    try:
        restore_backup(layout, info.path, include_sample=False)
        assert (readonly / "inner/f.txt").read_text() == "f"
        assert stat.S_IMODE(readonly.stat().st_mode) == 0o500
        assert stat.S_IMODE((readonly / "inner").stat().st_mode) == 0o500
        no_leftovers(layout)
    finally:
        for directory in (readonly, readonly / "inner"):
            if directory.exists():
                os.chmod(directory, 0o700)


def test_置き場所そのものが記号のリンクなら作らない(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    real = tmp_path / "real-state"
    layout.core_state_dir.rename(real)
    layout.core_state_dir.symlink_to(real)
    with pytest.raises(BackupFailed):
        create_backup(layout, ID_A)
    assert list(layout.backups_dir.iterdir()) == []


def test_サンプルのユニットがリンクなら先のファイルを入れる(tmp_path: Path) -> None:
    # systemctl --user link で作ったユニットは、作業コピーの中のファイルへのリンク
    layout = populate(tmp_path)
    real = write(tmp_path / "units/osekkai-rover.service", "[Service]\nExecStart=x\n")
    layout.sample_unit_file.unlink()
    layout.sample_unit_file.symlink_to(real)
    info = create_sample_backup(layout, ID_A, freeze_runner(layout))
    assert info is not None
    with tarfile.open(info.path, "r:gz") as tar:
        member = tar.getmember("sample-unit/osekkai-rover.service")
        assert member.isreg()
        extracted = tar.extractfile(member)
        assert extracted is not None
        assert extracted.read() == real.read_bytes()
    sample_total = sum(len(v) for v in snapshot(layout.sample_dir).values())
    assert estimate_backup_bytes(layout, include_sample=True) == (
        estimate_backup_bytes(layout) + sample_total + real.stat().st_size
    )


@pytest.mark.parametrize("dangling", [True, False])
def test_サンプルのユニットのリンクの先がふつうのファイルでなければ作らない(
    tmp_path: Path, dangling: bool
) -> None:
    layout = populate(tmp_path)
    layout.sample_unit_file.unlink()
    target = tmp_path / ("missing" if dangling else "a-directory")
    if not dangling:
        target.mkdir()
    layout.sample_unit_file.symlink_to(target)
    with pytest.raises(BackupFailed):
        create_sample_backup(layout, ID_A, freeze_runner(layout))
    assert list(layout.backups_dir.iterdir()) == []


def test_サンプルの作業コピーやデータがリンクなら作らない(tmp_path: Path) -> None:
    layout = populate(tmp_path)
    real = tmp_path / "real-data"
    (layout.sample_dir / "data").rename(real)
    (layout.sample_dir / "data").symlink_to(real)
    with pytest.raises(BackupFailed):
        create_backup(layout, ID_A)
