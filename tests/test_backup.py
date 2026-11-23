import json
import shutil
import sqlite3
import subprocess

import pytest

import backup

pytestmark = pytest.mark.skipif(not shutil.which("restic"), reason="restic not installed")


@pytest.fixture()
def bproject(project, tmp_path):
    from common import load_config
    root, _ = project
    alt = tmp_path / "proj"
    shutil.copytree(root, alt)
    (alt / ".env").write_text("ANTHROPIC_API_KEY=sk-secret\n")
    (alt / "site.env").write_text("SITE_PASSWORD=hunter2\n")
    (alt / "data" / "audio" / "v1001.opus").write_bytes(b"audio")
    cfg = load_config(alt / "config.yaml")
    cfg["backup"] = {"password_file": str(tmp_path / "pw" / "restic-password"), "repos": []}
    return alt, cfg, tmp_path


def snapshot_files(cfg, repo):
    r = backup.restic(backup.settings(cfg), repo, "ls", "latest", "--json")
    return [json.loads(l)["path"] for l in r.stdout.splitlines() if l.startswith("{") and '"path"' in l]


def test_backup_end_to_end(bproject, capsys):
    root, cfg, tmp = bproject
    assert backup.run(cfg) == "not_configured"
    repo = str(tmp / "repo")
    assert backup.init(cfg, repo) == 0
    assert "SAVE THIS PASSWORD" in capsys.readouterr().out
    assert (tmp / "pw" / "restic-password").stat().st_mode & 0o777 == 0o600
    assert backup.init(cfg, repo) == 0                                        # idempotent
    cfg["backup"]["repos"] = [repo]
    assert backup.run(cfg, now=1_000_000) == "ok"
    files = snapshot_files(cfg, repo)
    assert any(f.endswith("archive-snapshot/archive.db") for f in files)
    assert any(f.endswith("data/transcripts/v1001.json") for f in files)
    assert any(f.endswith("data/chat/ytABCDEFGH1.jsonl") for f in files)
    assert not any(f.endswith((".env", "site.env", ".opus")) for f in files)    # secrets and audio stay out
    assert not any(f.endswith("archive/archive.db") for f in files)            # the live db: the snapshot stands in
    assert not (root / "archive" / "archive-snapshot").exists()                # staging removed
    st = json.loads((root / "logs" / "backup-status.json").read_text())
    assert st["last_ok"] == 1_000_000 and st["repos"][repo]["error"] is None and st["repos"][repo]["files"] > 5
    assert backup.run(cfg, now=1_000_000 + 3600) == "not_due"
    # new chat arrives, forced run: only the change is added
    con = sqlite3.connect(root / "archive" / "archive.db")
    con.execute("INSERT INTO live_msgs(ts_ms, channel, text) VALUES(1, 'teststreamer', 'new message')")
    con.commit()
    con.close()
    assert backup.run(cfg, force=True, now=1_000_000 + 7200) == "ok"
    st = json.loads((root / "logs" / "backup-status.json").read_text())
    assert st["repos"][repo]["added_bytes"] < 2_000_000
    n_live = sqlite3.connect(root / "archive" / "archive.db").execute("SELECT COUNT(*) FROM live_msgs").fetchone()[0]
    capsys.readouterr()
    assert backup.restore_test(cfg) == 0
    out = capsys.readouterr().out
    assert "restore-test OK" in out and f"live_msgs {n_live}" in out          # includes the message just added
    backup.status(cfg)
    assert "last successful backup: " in capsys.readouterr().out


def test_include_audio_and_a_failing_repo(bproject):
    root, cfg, tmp = bproject
    good = str(tmp / "good")
    backup.init(cfg, good)
    cfg["backup"].update(repos=[str(tmp / "missing-not-initialised"), good], include_audio=True)
    assert backup.run(cfg, now=5_000_000) == "partial"                         # one bad destination doesn't stop the rest
    st = json.loads((root / "logs" / "backup-status.json").read_text())
    assert st["repos"][good]["error"] is None and st["repos"][str(tmp / "missing-not-initialised")]["error"]
    assert any(f.endswith("data/audio/v1001.opus") for f in snapshot_files(cfg, good))
