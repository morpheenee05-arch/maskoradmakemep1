"""
Telegram Bot Client Setup.
Handles bot initialization and handler registration.
"""
from typing import Any

import httpx
from telegram.ext import Application
from telegram.request import HTTPXRequest
from loguru import logger

import config


class _PlaceholderClient:
    """Stand-in so HTTPXRequest.__init__ does not bind httpx to a sync context."""

    def __init__(self, timeout: httpx.Timeout):
        self.timeout = timeout
        self.is_closed = True

    async def aclose(self) -> None:
        return


class LazyHTTPXRequest(HTTPXRequest):
    """HTTPXRequest that builds AsyncClient only after the asyncio loop is running.

    Creating ``httpx.AsyncClient`` / ``AsyncHTTPTransport`` in sync ``main()``
    makes sniffio report "unknown async library". Later getMe/getUpdates/close
    then raise AsyncLibraryNotFoundError, which kills the controller and leaves
    the bot as @unknown with no Telegram replies.
    """

    def __init__(self, *args, force_ipv4: bool = False, **kwargs):
        self._force_ipv4 = force_ipv4
        self._building_placeholder = True
        try:
            super().__init__(*args, **kwargs)
        finally:
            self._building_placeholder = False

    def _build_client(self) -> httpx.AsyncClient:
        if getattr(self, "_building_placeholder", False):
            return _PlaceholderClient(self._client_kwargs["timeout"])  # type: ignore[return-value]
        kwargs = dict(self._client_kwargs)
        if self._force_ipv4:
            transport_kw: dict[str, Any] = {"local_address": "0.0.0.0"}
            proxy = kwargs.get("proxy")
            if proxy:
                transport_kw["proxy"] = proxy
            kwargs["transport"] = httpx.AsyncHTTPTransport(**transport_kw)
        return httpx.AsyncClient(**kwargs)


def _telegram_request_kwargs() -> dict[str, Any]:
    """Proxy only — IPv4 is applied when the async client is actually built."""
    proxy = config.TELEGRAM_PROXY or None
    if proxy:
        return {"proxy": proxy}
    return {}


def create_bot() -> Application:
    """Create and return the Telegram Application instance."""
    extra = _telegram_request_kwargs()
    request = LazyHTTPXRequest(
        connection_pool_size=config.TELEGRAM_REQUEST_POOL_SIZE,
        connect_timeout=config.TELEGRAM_CONNECT_TIMEOUT_SEC,
        read_timeout=config.TELEGRAM_READ_TIMEOUT_SEC,
        write_timeout=config.TELEGRAM_WRITE_TIMEOUT_SEC,
        pool_timeout=config.TELEGRAM_POOL_TIMEOUT_SEC,
        media_write_timeout=config.TELEGRAM_MEDIA_WRITE_TIMEOUT_SEC,
        force_ipv4=config.TELEGRAM_FORCE_IPV4,
        **extra,
    )
    get_updates_request = LazyHTTPXRequest(
        connection_pool_size=config.TELEGRAM_UPDATES_POOL_SIZE,
        connect_timeout=config.TELEGRAM_CONNECT_TIMEOUT_SEC,
        read_timeout=max(30.0, config.TELEGRAM_READ_TIMEOUT_SEC),
        write_timeout=config.TELEGRAM_WRITE_TIMEOUT_SEC,
        pool_timeout=config.TELEGRAM_POOL_TIMEOUT_SEC,
        force_ipv4=config.TELEGRAM_FORCE_IPV4,
        **extra,
    )

    builder = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
    )
    if config.TELEGRAM_API_BASE:
        builder = builder.base_url(config.TELEGRAM_API_BASE)
        logger.info(f"Telegram API base override: {config.TELEGRAM_API_BASE}")
    if config.TELEGRAM_PROXY:
        logger.info(f"Telegram proxy enabled: {config.TELEGRAM_PROXY}")
    if config.TELEGRAM_FORCE_IPV4:
        logger.info("Telegram HTTP client forced to IPv4")

    application = builder.build()

    # Shared state storage
    application.bot_data["active_calls"] = {}
    application.bot_data["gv_pool"] = None
    application.bot_data["p1_queue"] = None
    application.bot_data["p1file_pending"] = None
    try:
        from storage.script_templates import bundle_for, get_active_key_sync
        active = get_active_key_sync()
    except Exception:
        active = "suspicious"
        bundle_for = None
    application.bot_data["script_profile"] = active
    application.bot_data["script_bundle"] = (
        bundle_for(active) if callable(bundle_for) else None
    )

    return application


def register_handlers(application: Application):
    """Register all command and conversation handlers."""
    from bot.commands.call import register_call_handlers
    from bot.commands.menu import register_menu_handlers
    from bot.commands.prompt import register_prompt_handlers
    from bot.commands.status import register_status_handlers
    from bot.commands.scripts import register_script_handlers
    from bot.commands.spamcheck import register_spamcheck_handlers
    from bot.commands.p1_live import register_p1live_handlers
    from bot.commands.p1_hangups import register_p1_hangups_handlers
    from bot.commands.accounts import register_accounts_handlers
    from bot.commands.wipes import register_wipes_handlers

    register_call_handlers(application)
    register_menu_handlers(application)
    register_prompt_handlers(application)
    register_status_handlers(application)
    register_script_handlers(application)
    register_spamcheck_handlers(application)
    register_p1live_handlers(application)
    register_p1_hangups_handlers(application)
    register_accounts_handlers(application)
    register_wipes_handlers(application)

    logger.info("All Telegram handlers registered")
