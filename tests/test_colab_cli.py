import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "cli_run", Path(__file__).resolve().parent.parent / "deploy" / "colab" / "cli_run.py")
assert spec is not None and spec.loader is not None
cli_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli_run)

LOG = """\
2026-11-10 22:00:01 process INFO VOD 1/2: v111
2026-11-10 22:00:02 process INFO v111: 2.0 h of audio in 2 chunks
2026-11-10 22:00:03 process INFO v111 chunk 1/2: transcribing
Progress: 45.0%...
2026-11-10 22:04:00 process INFO v111 chunk 1/2: aligning
2026-11-10 22:05:00 process INFO v111 chunk 1/2: separating speakers
2026-11-10 22:06:00 process INFO v111 chunk 1/2 done
2026-11-10 22:06:01 process INFO v111 chunk 2/2: transcribing
2026-11-10 22:11:00 process INFO v111 chunk 2/2 done
2026-11-10 22:11:05 process INFO v111: 2.0 h in 11 min (10.9x realtime)
2026-11-10 22:11:06 process INFO VOD 2/2: v222
2026-11-10 22:11:07 process INFO v222 chunk 1/4: separating speakers
"""


def test_parse_progress_follows_vod_chunk_and_stage():
    p = cli_run.parse_progress(LOG)
    assert (p["vod_k"], p["vod_n"], p["vid"]) == (2, 2, "v222")
    assert (p["chunk_i"], p["chunk_n"], p["stage"]) == (1, 4, "separating speakers")
    assert p["finished"] == ["v111"]


def test_batch_fraction_counts_finished_vods_and_done_chunks():
    hours = {"v111": 2.0, "v222": 6.0}
    p = cli_run.parse_progress(LOG)
    assert cli_run.batch_fraction(p, hours) == 0.25                 # v111 done, v222 in its first chunk
    p2 = cli_run.parse_progress(LOG + "x INFO v222 chunk 2/4 done\n")
    assert cli_run.batch_fraction(p2, hours) == (2 + 3) / 8          # half of v222's chunks done
    assert cli_run.batch_fraction(cli_run.parse_progress(""), hours) == 0


def test_bar_and_hm():
    assert cli_run.bar(0.25) == "[##--------] 25%" and cli_run.bar(0.99).startswith("[#########-]")
    assert cli_run.bar(1.7).startswith("[##########] ")
    assert cli_run.hm(3 * 3600 + 5 * 60 + 9) == "3h05m"


def test_redact_hides_proxy_tokens():
    msg = "400 for url: https://x.colab.dev/api/contents/a.opus?authuser=0&colab-runtime-proxy-token=eyJh.eyJ.ZUmk-_x"
    out = cli_run.redact(msg)
    assert "eyJh" not in out and out.endswith("colab-runtime-proxy-token=<redacted>")


def test_every_snippet_sent_to_the_runtime_compiles(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(cli_run, "colab", lambda *a, **k: sent.append(k.get("stdin")) or '@@ {"pid": 1, "exit": null, '
                        '"log": "", "done": [], "sha": "x", "size": 0}')
    cli_run.start_job("job", "python pipeline/process.py")
    cli_run.job_state("setup", log_tail=300)
    cli_run.job_state("job")
    f = tmp_path / "a.opus"
    f.write_bytes(b"abc")
    try:
        cli_run.upload(f, "/content/vod/data/audio/a.opus")
    except RuntimeError:
        pass                                    # the fake runtime's sha never matches
    sent = [c for c in sent if c is not None]
    assert len(sent) == 4
    for code in sent:
        compile(code, "<runtime>", "exec")


def test_a_crashed_cli_call_is_retried(monkeypatch):
    calls = []

    def flaky(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            raise cli_run.CliCrashed("colab exec -s crashed (signal 11)")
        return '@@ {"exit": null, "log": "", "done": []}'
    monkeypatch.setattr(cli_run, "colab", flaky)
    monkeypatch.setattr(cli_run.time, "sleep", lambda s: None)
    assert cli_run.job_state("job")["exit"] is None and len(calls) == 2
