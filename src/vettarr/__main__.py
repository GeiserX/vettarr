from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

import uvicorn

from vettarr import dryrun, events, log_mask
from vettarr.config import settings
from vettarr.config.components import components
from vettarr.config.loader import ConfigError
from vettarr.config.runtime import get_config
from vettarr.config.schema import Config
from vettarr.config.schema import warnings as config_warnings
from vettarr.database import init_db

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("vettarr")
# From the first line on, nothing shaped like a bot token reaches a handler;
# the config's own secrets join once it is loaded (_load_config).
log_mask.install()

# How long the other components get to stop by themselves once a stop is asked
# for or one of them has failed, before they are cancelled.
_STOP_GRACE_SECONDS = 30.0


# Components the config switches on or off; everything else always runs.
_GATED = ("userbot", "bot", "web")


def plan(cfg: Config) -> list[tuple[str, str]]:
    """What ``_run`` starts, as ``(component, why)``.

    The userbot, the bot and the web UI start only when the config asks for
    them (``components()``); the scheduler always runs.
    """
    started = [(name, why) for name, why in components(cfg) if name in _GATED]
    started.append(("scheduler", "always"))
    return started


def _load_config() -> Config:
    """The process Config, or exit 1 with the reasons it cannot be read."""
    try:
        cfg = get_config()
    except ConfigError as exc:
        print("vettarr: the config is not valid:", file=sys.stderr)
        for line in exc.errors:
            print(f"  {line}", file=sys.stderr)
        raise SystemExit(1) from None
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"vettarr: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    log_mask.install(cfg)
    return cfg


def boot() -> tuple[Config, list[tuple[str, str]]]:
    """Resolve the config and log what it starts and why. Never logs a value."""
    cfg = _load_config()
    started = plan(cfg)
    if cfg.dry_run:
        log.warning("Dry run: %s. Every write is a would_have event instead", dryrun.SUMMARY)
    for name, why in started:
        log.info("Starting %s: %s", name, why)
    # A gap in `deliver` decides what an approval does, and `config check` is
    # the only other place that says so.
    for loc, text in config_warnings(cfg):
        if loc[:1] == ("deliver",):
            log.warning("Config: %s", text)
    return cfg, started


async def supervise(
    tasks: list[asyncio.Task], stop: asyncio.Event, *, grace: float = _STOP_GRACE_SECONDS
) -> list[str]:
    """Run the components until a stop is asked for or one ends. The names that failed.

    A component has failed when it raises, or when it returns while nobody
    asked for a stop. Either way the rest are told to stop and get ``grace``
    seconds to do it their own way (the bot closes its session, the web server
    finishes its requests); only one that does not is cancelled. Nothing here
    raises, so the caller decides the exit code from the answer.
    """
    failed: list[str] = []

    def report(done: set[asyncio.Task], *, stop_asked: bool) -> None:
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                log.error(
                    "%s failed: %s: %s", task.get_name(), type(exc).__name__, exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            elif not stop_asked:
                log.error("%s failed: it stopped by itself", task.get_name())
            else:
                continue
            failed.append(task.get_name())

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    report(done, stop_asked=stop.is_set())
    if failed:
        log.error("Stopping everything else: %s cannot run", ", ".join(failed))
    stop.set()
    if pending:
        done, late = await asyncio.wait(pending, timeout=grace)
        report(done, stop_asked=True)
        for task in late:
            log.warning("%s did not stop within %.0f s; cancelling it", task.get_name(), grace)
            task.cancel()
        await asyncio.gather(*late, return_exceptions=True)
    return failed


async def run_bot(bot_app, cfg: Config, stop: asyncio.Event) -> None:
    """The bot, from its first call to Telegram until the stop."""
    await bot_app.initialize()
    try:
        await bot_app.start()
        if bot_app.updater:
            # A bot source's messages sent while the app was down are posts
            # to ingest; without one, a backlog is only stale commands.
            has_bot_source = any(s.kind == "bot" for s in cfg.sources)
            await bot_app.updater.start_polling(drop_pending_updates=not has_bot_source)
        await stop.wait()
    finally:
        if bot_app.updater and bot_app.updater.running:
            await bot_app.updater.stop()
        if bot_app.running:
            await bot_app.stop()
        await bot_app.shutdown()


async def run_web(server: uvicorn.Server, stop: asyncio.Event) -> None:
    """The web server until the stop. uvicorn watches its own flag, not ours."""

    async def stop_with_the_rest() -> None:
        await stop.wait()
        server.should_exit = True

    watcher = asyncio.create_task(stop_with_the_rest())
    try:
        await server.serve()
    except SystemExit:
        # uvicorn logs why it cannot listen and then exits the process itself,
        # which would skip the other components' own shutdown.
        raise RuntimeError("the web server could not start; its error is in the log above") from None
    finally:
        watcher.cancel()


def web_server(web_app, cfg: Config) -> uvicorn.Server:
    uvi_config = uvicorn.Config(
        web_app, host=cfg.web.bind, port=cfg.web.port, log_level="warning"
    )
    # uvicorn has just built handlers of its own.
    log_mask.install()
    return uvicorn.Server(uvi_config)


async def _run(cfg: Config, started: list[tuple[str, str]]) -> list[str]:
    """Run until a signal or a failure. The components that failed; none on a clean stop."""
    names = {name for name, _ in started}
    log.info("Initializing database...")
    await init_db()

    from vettarr import ingest
    from vettarr.bot.handlers import create_bot_app
    from vettarr.ingester.telegram_reader import TelegramIngester
    from vettarr.pipeline import process_queue
    from vettarr.scheduler import Scheduler
    from vettarr.search_jobs import run_search_jobs
    from vettarr.web.app import create_web_app

    pipeline_queue: asyncio.Queue[int] = asyncio.Queue()
    ingest.set_queue(pipeline_queue)

    ingester: TelegramIngester | None = None
    if "userbot" in names:
        settings.session_dir.mkdir(parents=True, exist_ok=True)
        ingester = TelegramIngester(cfg.sources)
    # What on_download and on_reject do to a post, through whichever client reads its source.
    posts = ingest.PostActions(userbot=ingester, config=cfg)
    bot_app = create_bot_app(posts) if "bot" in names else None
    if bot_app is not None:
        posts.bot = bot_app.bot
    web_app = create_web_app(posts) if "web" in names else None
    from vettarr.searcher.search_service import init_trackers
    init_trackers()
    scheduler = Scheduler(posts)

    uvi_server = None
    if web_app is not None and cfg.web is not None:
        uvi_server = web_server(web_app, cfg)

    stop = asyncio.Event()

    def _signal_handler() -> None:
        log.info("Shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    async def run_ingester() -> None:
        # New messages are taken before the histories are read: see TelegramIngester.
        await ingester.run(stop)

    async def run_pipeline() -> None:
        workers = [asyncio.create_task(process_queue(pipeline_queue)) for _ in range(3)]
        await stop.wait()
        for w in workers:
            w.cancel()

    async def run_download_jobs() -> None:
        await run_search_jobs(posts, stop)

    async def run_scheduler() -> None:
        scheduler.start()
        await stop.wait()
        scheduler.shutdown()

    log.info("Starting vettarr: %s", ", ".join(name for name, _ in started))

    tasks = []
    if ingester is not None:
        tasks.append(asyncio.create_task(run_ingester(), name="userbot"))
    tasks += [
        asyncio.create_task(run_pipeline(), name="pipeline"),
        asyncio.create_task(run_download_jobs(), name="download-jobs"),
    ]
    if bot_app is not None:
        tasks.append(asyncio.create_task(run_bot(bot_app, cfg, stop), name="bot"))
    if uvi_server is not None:
        tasks.append(asyncio.create_task(run_web(uvi_server, stop), name="web"))
    tasks.append(asyncio.create_task(run_scheduler(), name="scheduler"))

    failed = await supervise(tasks, stop)
    if ingester is not None:
        try:
            await ingester.stop()
        except Exception as exc:
            log.warning("The userbot session did not close cleanly: %s", type(exc).__name__)
    await _last_flush()
    return failed


async def _last_flush(timeout: float = 15.0) -> None:
    """Write the events still queued once everything has stopped.

    The scheduler's flush job is gone by now, and it ran every five seconds,
    so without this every stop loses the events of its last seconds.
    """
    try:
        await asyncio.wait_for(events.flush_all(), timeout)
    except Exception as exc:
        log.warning("The last events could not be written: %s", type(exc).__name__)


async def _rerun() -> None:
    from vettarr.database import engine
    from vettarr.pipeline import rerun_pending_reviews

    try:
        summary = await rerun_pending_reviews()
    finally:
        # No scheduler runs here, so nothing else writes the events the
        # re-run raised before the process exits.
        try:
            await events.flush_all()
        finally:
            await engine.dispose()
    print(summary.text())


def main(argv: list[str] | None = None) -> None:
    raw = sys.argv[1:] if argv is None else argv
    if raw[:1] == ["config"]:
        from vettarr.config.cli import main as config_main
        raise SystemExit(config_main(raw[1:]))
    if raw[:1] == ["web"]:
        from vettarr.web.cli import main as web_main
        raise SystemExit(web_main(raw[1:], _load_config))
    if raw[:1] == ["demo-seed"]:
        from vettarr.demo import main as demo_main
        raise SystemExit(demo_main(raw[1:]))
    parser = argparse.ArgumentParser(prog="vettarr")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("config", help="check, import or print the config file (config --help)")
    commands.add_parser("web", help="hash the web password, or run the web UI alone (web --help)")
    commands.add_parser("demo-seed", help="fill the database with fake items for screenshots and UI work")
    commands.add_parser(
        "rerun",
        help="re-decide items the policy sent to review without a flag, then exit",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "rerun":
            _load_config()
            asyncio.run(_rerun())
        else:
            failed = asyncio.run(_run(*boot()))
            if failed:
                # A component that cannot run is not a clean stop: exit 1, so
                # a restart policy acts instead of leaving half an app up.
                log.error("Exiting with status 1: %s failed", ", ".join(failed))
                raise SystemExit(1)
    except KeyboardInterrupt:
        pass


def legacy_main(argv: list[str] | None = None) -> None:
    """The ``media-triage`` command: the name before the rename, kept so an
    existing compose file still starts. Says so once, then runs ``main``."""
    print("media-triage: this command is now `vettarr`; the old name will be removed in a later release",
          file=sys.stderr)
    main(argv)


if __name__ == "__main__":
    main()
