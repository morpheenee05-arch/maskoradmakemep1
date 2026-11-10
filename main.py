"""
AI Caller - Main Entry Point

Telegram-controlled AI voice calling system.
"""
import warnings
warnings.filterwarnings("ignore", message=".*Pydantic.*", category=UserWarning)
warnings.filterwarnings("ignore", message=".*per_message.*", category=UserWarning)
warnings.filterwarnings("ignore", message=".*WindowsSelectorEventLoopPolicy.*", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*set_event_loop_policy.*", category=DeprecationWarning)

import asyncio
import os
import sys
import threading
from loguru import logger
from telegram.error import Conflict, NetworkError, TimedOut, TelegramError

# Validate config on import
import config
config.validate_config()

from bot.client import create_bot, register_handlers


# Track the background init task so we can cancel it on shutdown.
_init_task: asyncio.Task = None
_dashboard = None


# ── Graceful shutdown helper ───────────────────────────────────────────────────

def _kill_chrome_processes():
    """
    Force-kill leftover chromedriver / headless chrome from this run.
    Windows uses taskkill; Linux uses pkill. Controller nodes usually have none.
    """
    try:
        import subprocess
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/IM", "chromedriver.exe"],
                capture_output=True, check=False,
            )
            # Kill headless Chrome instances only — leave real Chrome open.
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe", "/FI", "WINDOWTITLE eq "],
                capture_output=True, check=False,
            )
        else:
            subprocess.run(
                ["pkill", "-f", "chromedriver"],
                capture_output=True, check=False,
            )
            subprocess.run(
                ["pkill", "-f", "chrome.*--headless"],
                capture_output=True, check=False,
            )
        logger.debug("Orphan chromedriver/chrome cleanup finished")
    except Exception as e:
        logger.debug(f"Process cleanup error (non-fatal): {e}")


def _force_exit():
    """Last-resort exit after a timeout — kills everything."""
    logger.warning("Force-exiting (cleanup timed out)")
    _kill_chrome_processes()
    os._exit(1)


# ── Lifecycle callbacks ──────────────────────────────────────────────────────

async def post_init(application):
    """Called after the Application is initialized, before polling starts."""
    global _init_task, _dashboard

    logger.info("=" * 50)
    logger.info("AI Caller Starting...")
    logger.info("=" * 50)

    # initialize() already called get_me(). If identity is missing, Telegram
    # never answered — say so instead of pretending the bot is @unknown.
    username, bot_id = "unknown", "?"
    try:
        if getattr(application.bot, "_bot_user", None) is None:
            await application.bot.get_me()
        username = application.bot.username or "unknown"
        bot_id = application.bot.id
    except Exception as e:
        logger.error(
            f"Telegram getMe failed — the bot will not answer chats: {e}"
        )
    logger.info(f"Bot is ready! Logged in as @{username} (ID: {bot_id})")

    if _dashboard is not None:
        try:
            _dashboard.start()
        except Exception as e:
            logger.debug(f"Dashboard start skipped: {e}")

    # Controller mode: no local PhonePool — capacity lives on remote agents.
    if config.NODE_ROLE == "controller":
        from infra.redis_bus import RedisBus
        from console_ui import controller_dashboard

        bus = RedisBus()
        application.bot_data["bus"] = bus
        application.bot_data["gv_pool"] = None

        ok = await bus.ping()
        controller_dashboard.set_header(
            bot_username=username, redis_ok=ok
        )
        if ok:
            logger.info("Controller connected to Redis bus")
        else:
            logger.error(
                "Controller cannot reach Redis — check REDIS_URL/TLS/password"
            )

        async def _refresh_controller_panel():
            logger.info("Controller dashboard refresh loop started")
            while True:
                try:
                    redis_ok = await bus.ping()
                    agents = await bus.list_agents() if redis_ok else []
                    controller_dashboard.set_header(
                        bot_username=username, redis_ok=redis_ok
                    )
                    controller_dashboard.update_agents(agents)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug(f"Controller panel refresh error: {e}")
                await asyncio.sleep(5)

        _init_task = asyncio.create_task(
            _refresh_controller_panel(), name="controller_panel_refresh"
        )
        return

    # Standalone only. Imported here so a controller node never needs
    # Selenium / Chrome on disk.
    from infra.remote_settings import apply_settings, load_local_settings

    saved = load_local_settings()
    if saved:
        applied = apply_settings(config, saved)
        if applied:
            logger.info(f"Local bot settings applied: {sorted(applied)}")

    from telephony.google_voice_pool import PhonePool

    # Initialize pool in background — Selenium startup is slow and would timeout post_init.
    gv_pool = PhonePool()
    application.bot_data["gv_pool"] = gv_pool

    async def _init_gv():
        logger.info("Initializing Telephony Pool in background...")
        try:
            attempts = max(1, config.GV_INIT_RETRY_COUNT)
            delay_sec = max(5, config.GV_INIT_RETRY_DELAY_SEC)
            gv_success = False

            for attempt in range(1, attempts + 1):
                gv_success = await gv_pool.initialize()
                if gv_success:
                    logger.info(
                        f"Telephony pool initialization completed on attempt {attempt}/{attempts}"
                    )
                    break
                if attempt < attempts:
                    logger.warning(
                        f"Telephony pool init attempt {attempt}/{attempts} failed. "
                        f"Retrying in {delay_sec}s..."
                    )
                    await asyncio.sleep(delay_sec)

            if not gv_success:
                logger.warning("Telephony pool initialization fully failed - calls will be simulated")
        except asyncio.CancelledError:
            logger.info("GV pool init cancelled (shutdown requested)")
            raise
        except Exception as e:
            logger.error(f"Telephony pool error: {e}")

    _init_task = asyncio.create_task(_init_gv(), name="gv_pool_init")


async def post_shutdown(application):
    """Called during shutdown — cancel init task, close all browser instances cleanly."""
    global _init_task
    logger.info("Shutting down…")

    # Cancel the background init task if it's still running (e.g. Chrome is still spawning).
    if _init_task and not _init_task.done():
        _init_task.cancel()
        try:
            await asyncio.wait_for(_init_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
    _init_task = None

    # Stop any active call handlers
    active_calls = application.bot_data.get("active_calls", {})
    for call_id, info in list(active_calls.items()):
        handler = info.get("audio_handler")
        if handler:
            try:
                await handler.stop()
            except Exception:
                pass
    active_calls.clear()

    # Shut down pool browsers
    gv_pool = application.bot_data.get("gv_pool")
    if gv_pool:
        try:
            gv_pool.shutdown()
        except Exception as e:
            logger.error(f"Pool shutdown error: {e}")

    # Close the Redis bus (controller mode).
    bus = application.bot_data.get("bus")
    if bus:
        try:
            await bus.close()
        except Exception:
            pass

    # Belt-and-suspenders: forcibly kill any orphaned Chrome/chromedriver processes.
    _kill_chrome_processes()

    logger.info("Shutdown complete")


# ── Main ─────────────────────────────────────────────────────────────────────

async def on_application_error(update, context):
    """Global Telegram error handler for transient network/API failures."""
    err = context.error
    if isinstance(err, Conflict):
        logger.error(
            "Telegram getUpdates conflict — another process is already polling "
            "this bot token. Stop the old controller (or wait for it to die) "
            "or the bot will stay silent."
        )
        return
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"Telegram transient network error: {type(err).__name__}: {err}")
        return

    if isinstance(err, TelegramError):
        logger.error(f"Telegram API error: {type(err).__name__}: {err}")
        return

    logger.exception(f"Unhandled application error: {err}")


def main():
    """Main entry point."""
    global _dashboard
    application = create_bot()

    application.post_init = post_init
    application.post_shutdown = post_shutdown

    register_handlers(application)
    application.add_error_handler(on_application_error)

    logger.info("Starting Telegram bot polling…")

    from console_ui import dashboard, controller_dashboard
    _dashboard = (
        controller_dashboard
        if config.NODE_ROLE == "controller"
        else dashboard
    )
    # Do not dash.start() here: Rich Live before the asyncio loop exists
    # races Telegram's HTTP client. post_init starts it once getMe is done.
    # Do not install signal.signal(SIGINT) here either — PTB already owns
    # SIGINT on Linux via the event loop. Raising KeyboardInterrupt from a
    # raw handler is what produced "no running event loop" / sniffio crashes.

    try:
        application.run_polling(
            drop_pending_updates=True,
            bootstrap_retries=5,
        )
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt caught — shutting down")
    finally:
        timer = threading.Timer(10.0, _force_exit)
        timer.daemon = True
        timer.start()
        try:
            if _dashboard is not None:
                _dashboard.stop()
            _kill_chrome_processes()
        finally:
            timer.cancel()


if __name__ == "__main__":
    # Required for multiprocessing on Windows (audio runs in separate process).
    import multiprocessing
    multiprocessing.freeze_support()

    # Agent nodes have no Telegram bot — delegate to the agent runner.
    if config.NODE_ROLE == "agent":
        from agent.main import main as agent_main
        sys.exit(agent_main())

    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        _kill_chrome_processes()
    except (TimedOut, NetworkError) as e:
        logger.error(
            f"Fatal error: Telegram HTTP failed ({e}). "
            "Ubuntu cannot reach api.telegram.org — usually broken IPv6, "
            "DNS, a stale controller still polling, or a block. "
            "Retry after deploy, or set TELEGRAM_PROXY="
            "socks5://127.0.0.1:1080 (TELEGRAM_FORCE_IPV4=1 is the Linux default)."
        )
        _kill_chrome_processes()
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        _kill_chrome_processes()
        sys.exit(1)
