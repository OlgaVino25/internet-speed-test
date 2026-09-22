from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import requests

log = logging.getLogger(__name__)

DEFAULT_URL = "https://speed.cloudflare.com/__down?bytes=10000000"
DEFAULT_RUNS = 10
CHUNK_SIZE = 64 * 1024
BYTES_IN_MEGABYTE = 1024 * 1024

ProgressCallback = Callable[[int, int, "RunResult | None"], None]


class _StdoutFilter(logging.Filter):
    """Пропускает в stdout только записи ниже WARNING."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.WARNING


def _force_utf8_streams() -> None:
    """Пишем UTF-8 во все потоки, независимо от локали ОС.

    На русской Windows sys.stderr по умолчанию пишет в cp1251, что даёт
    нечитаемые файлы при редиректе в CI/лог-агрегаторы, ожидающие UTF-8.
    На Linux/Mac вызов — no-op.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _setup_logging(verbose: bool = False) -> None:
    """INFO/DEBUG -> stdout, WARNING+ -> stderr."""
    _force_utf8_streams()
    logger = logging.getLogger("internet_speed_test")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    fmt = logging.Formatter("%(message)s")

    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.DEBUG if verbose else logging.INFO)
    out.addFilter(_StdoutFilter())
    out.setFormatter(fmt)

    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    err.setFormatter(fmt)

    logger.addHandler(out)
    logger.addHandler(err)


@dataclass
class RunResult:
    index: int
    seconds: float
    bytes_downloaded: int

    @property
    def speed_mb_s(self) -> float:
        if self.seconds <= 0:
            return 0.0
        return (self.bytes_downloaded / BYTES_IN_MEGABYTE) / self.seconds


@dataclass
class Summary:
    runs_ok: int
    runs_total: int
    avg_time: float
    min_time: float
    max_time: float
    total_bytes: int
    total_time: float
    avg_speed_mb_s: float


def download_once(
    url: str,
    timeout: float,
    session: requests.Session | None = None,
) -> RunResult | None:
    """Скачивает URL один раз потоком и возвращает результат замера.

    Если запрос не удался — пишет warning и возвращает None.
    """
    own_session = session is None
    session = session or requests.Session()
    started = time.perf_counter()
    try:
        with session.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            total_bytes = 0
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    total_bytes += len(chunk)
        elapsed = time.perf_counter() - started
        return RunResult(index=0, seconds=elapsed, bytes_downloaded=total_bytes)
    except requests.RequestException as error:
        log.warning("Ошибка запроса: %s", error)
        return None
    finally:
        if own_session:
            session.close()


def measure_speed(
    url: str,
    runs: int,
    timeout: float,
    warmup: bool = True,
    on_run_done: ProgressCallback | None = None,
) -> list[RunResult]:
    """Выполняет N последовательных замеров скорости.

    Ничего не печатает: весь ввод-вывод — забота вызывающего кода.
    Если передан on_run_done, он вызывается после каждого замера
    с аргументами (номер_замера, всего_замеров, результат_или_None).
    """
    results: list[RunResult] = []

    with requests.Session() as session:
        if warmup:
            log.info("Прогрев соединения...")
            download_once(url, timeout, session)

        for run_number in range(1, runs + 1):
            result = download_once(url, timeout, session)
            if result is not None:
                result.index = run_number
                results.append(result)
            if on_run_done is not None:
                on_run_done(run_number, runs, result)

    return results


def summarize(results: Iterable[RunResult], runs_total: int) -> Summary | None:
    """Сводит список результатов в итоговую статистику.

    Возвращает None, если замеров не было.
    """
    results = list(results)
    if not results:
        return None
    times = [result.seconds for result in results]
    total_bytes = sum(result.bytes_downloaded for result in results)
    total_time = sum(times)
    avg_speed = (
        (total_bytes / BYTES_IN_MEGABYTE) / total_time if total_time > 0 else 0.0
    )
    return Summary(
        runs_ok=len(results),
        runs_total=runs_total,
        avg_time=statistics.mean(times),
        min_time=min(times),
        max_time=max(times),
        total_bytes=total_bytes,
        total_time=total_time,
        avg_speed_mb_s=avg_speed,
    )


def human_mb(num_bytes: int) -> str:
    """Форматирует байты в строку вида '1.50 MB'."""
    return f"{num_bytes / BYTES_IN_MEGABYTE:.2f} MB"


def print_summary(summary: Summary) -> None:
    """Печатает итоговую сводку в stdout."""
    print("\n" + "=" * 48)
    print(f"Успешных запросов:        {summary.runs_ok}/{summary.runs_total}")
    print(f"Среднее время запроса:    {summary.avg_time:.3f} s "
          f"(min {summary.min_time:.3f}, max {summary.max_time:.3f})")
    print(f"Суммарно скачано:         {human_mb(summary.total_bytes)}")
    print(f"Суммарное время:          {summary.total_time:.3f} s")
    print(f"Средняя скорость:         {summary.avg_speed_mb_s:.2f} MB/s")
    print("=" * 48)


def _print_run_progress(
    run_number: int,
    runs_total: int,
    result: RunResult | None,
) -> None:
    """Печатает строку прогресса после каждого замера."""
    if result is None:
        print(f"[{run_number}/{runs_total}] не удалось")
        return
    print(f"[{run_number}/{runs_total}] {result.seconds:.3f}s  "
          f"{human_mb(result.bytes_downloaded)}  "
          f"({result.speed_mb_s:.2f} MB/s)")


def run(url: str, runs: int, timeout: float, warmup: bool = True) -> int:
    """Запускает замеры и печатает результат. Возвращает код выхода."""
    print(f"URL: {url}")
    print(f"Запросов: {runs}, timeout: {timeout}s\n")

    results = measure_speed(
        url=url,
        runs=runs,
        timeout=timeout,
        warmup=warmup,
        on_run_done=_print_run_progress,
    )

    summary = summarize(results, runs)
    if summary is None:
        log.error("Ни один запрос не удался. Проверь URL и сеть.")
        return 1

    print_summary(summary)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        prog="speedtest",
        description="Замер скорости интернета: N последовательных запросов к URL.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_URL,
        help=f"Адрес ресурса для скачивания (по умолчанию: {DEFAULT_URL})",
    )
    parser.add_argument("-n", "--runs", type=int, default=DEFAULT_RUNS,
                        help=f"Количество запросов (по умолчанию: {DEFAULT_RUNS})")
    parser.add_argument("-t", "--timeout", type=float, default=30.0,
                        help="Таймаут одного запроса в секундах (по умолчанию: 30)")
    parser.add_argument("--no-warmup", action="store_true",
                        help="Отключить прогревочный запрос")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Подробный вывод (DEBUG)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI. Возвращает код выхода процесса."""
    args = parse_args(argv)
    _setup_logging(verbose=args.verbose)

    if args.runs <= 0:
        log.error("Количество запросов должно быть > 0")
        return 2
    return run(args.url, args.runs, args.timeout, warmup=not args.no_warmup)