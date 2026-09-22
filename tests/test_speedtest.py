from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

import internet_speed_test as speedtest
from internet_speed_test import (
    RunResult,
    Summary,
    download_once,
    human_mb,
    main,
    parse_args,
    run,
    summarize,
)


class _FakeResponse:
    """Мини-мок контекст-менеджера и стриминга ответа."""

    def __init__(self, chunks: list[bytes], status: int = 200):
        self._chunks = chunks
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1):
        for c in self._chunks:
            yield c


def _make_session(response=None, exc=None):
    session = MagicMock(spec=requests.Session)
    if exc is not None:
        session.get.side_effect = exc
    else:
        session.get.return_value = response
    return session


def test_speed_mb_s_normal():
    r = RunResult(index=1, seconds=2.0, bytes_downloaded=2 * 1024 * 1024)
    assert r.speed_mb_s == pytest.approx(1.0)


def test_speed_mb_s_zero_time_returns_zero():
    r = RunResult(index=1, seconds=0.0, bytes_downloaded=123)
    assert r.speed_mb_s == 0.0


def test_speed_mb_s_negative_time_returns_zero():
    r = RunResult(index=1, seconds=-1.0, bytes_downloaded=123)
    assert r.speed_mb_s == 0.0


def test_summarize_empty_returns_none():
    assert summarize([], runs_total=10) is None


def test_summarize_avg_speed_is_total_over_total():
    results = [
        RunResult(1, 1.0, 1 * 1024 * 1024),
        RunResult(2, 1.0, 3 * 1024 * 1024),
    ]
    s = summarize(results, runs_total=2)
    assert isinstance(s, Summary)
    assert s.runs_ok == 2
    assert s.runs_total == 2
    assert s.avg_time == pytest.approx(1.0)
    assert s.min_time == 1.0
    assert s.max_time == 1.0
    assert s.total_bytes == 4 * 1024 * 1024
    assert s.total_time == pytest.approx(2.0)
    assert s.avg_speed_mb_s == pytest.approx(2.0)


def test_summarize_partial_failures_counted_in_total():
    results = [RunResult(1, 1.0, 1024 * 1024)]
    s = summarize(results, runs_total=10)
    assert s is not None
    assert s.runs_ok == 1
    assert s.runs_total == 10


def test_download_once_counts_bytes_from_stream():
    session = _make_session(_FakeResponse([b"a" * 1024, b"b" * 2048]))
    r = download_once("http://x", timeout=5, session=session)
    assert r is not None
    assert r.bytes_downloaded == 3072
    assert r.seconds >= 0


def test_download_once_http_error_returns_none(caplog):
    session = _make_session(_FakeResponse([], status=500))
    with caplog.at_level("WARNING", logger="internet_speed_test.cli"):
        r = download_once("http://x", timeout=5, session=session)
    assert r is None
    assert "Ошибка запроса" in caplog.text


def test_download_once_network_error_returns_none(caplog):
    session = _make_session(exc=requests.ConnectionError("boom"))
    with caplog.at_level("WARNING", logger="internet_speed_test.cli"):
        r = download_once("http://x", timeout=5, session=session)
    assert r is None
    assert "Ошибка запроса" in caplog.text


def test_download_once_skips_empty_chunks():
    session = _make_session(_FakeResponse([b"", b"x" * 100, b""]))
    r = download_once("http://x", timeout=5, session=session)
    assert r is not None
    assert r.bytes_downloaded == 100


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0.00 MB"),
        (1024 * 1024, "1.00 MB"),
        (1536 * 1024, "1.50 MB"),
    ],
)
def test_human_mb(n, expected):
    assert human_mb(n) == expected


def test_parse_args_defaults():
    a = parse_args([])
    assert a.url == speedtest.DEFAULT_URL
    assert a.runs == speedtest.DEFAULT_RUNS
    assert a.timeout == 30.0
    assert a.no_warmup is False
    assert a.verbose is False


def test_parse_args_custom():
    a = parse_args(["http://img", "-n", "3", "-t", "10", "--no-warmup"])
    assert a.url == "http://img"
    assert a.runs == 3
    assert a.timeout == 10.0
    assert a.no_warmup is True


def test_main_rejects_non_positive_runs(caplog):
    with caplog.at_level("ERROR", logger="internet_speed_test.cli"):
        rc = main(["http://x", "-n", "0"])
    assert rc == 2
    assert "должно быть > 0" in caplog.text


def test_run_all_success(capsys):
    ok = RunResult(index=0, seconds=0.5, bytes_downloaded=1024 * 1024)

    with patch("internet_speed_test.cli.download_once", return_value=ok) as m:
        rc = run("http://x", runs=3, timeout=5, warmup=True)

    assert m.call_count == 4
    out = capsys.readouterr().out
    assert rc == 0
    assert "Успешных запросов:        3/3" in out
    assert "Средняя скорость:" in out


def test_run_returns_1_on_total_failure(caplog):
    with patch("internet_speed_test.cli.download_once", return_value=None):
        with caplog.at_level("ERROR", logger="internet_speed_test.cli"):
            rc = run("http://x", runs=2, timeout=5, warmup=False)

    assert rc == 1
    assert "Ни один запрос не удался" in caplog.text