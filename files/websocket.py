import logging
import asyncio
import aiohttp
import traceback
from typing import Any, Coroutine, Text, Callable

_LOGGER: logging.Logger = logging.getLogger(__package__)

class EufySecurityWebSocket:
    def __init__(
        self,
        host: str,
        port: int,
        session: aiohttp.ClientSession,
        open_callback: Callable[[], Coroutine[Any, Any, None]],
        message_callback: Callable[[Any], Coroutine[Any, Any, None]],  # Adjusted typing to accept message
        close_callback: Callable[[], Coroutine[Any, Any, None]],
        error_callback: Callable[[Text], Coroutine[Any, Any, None]],
    ):
        self.host = host
        self.port = port
        self.session = session
        self.open_callback = open_callback
        self.message_callback = message_callback
        self.close_callback = close_callback
        self.error_callback = error_callback

        self.base = f"ws://{self.host}:{self.port}"
        self.ws: aiohttp.ClientWebSocketResponse = None
        self.loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()

    async def connect(self):
        retries = 5
        delay = 5  # seconds

        for attempt in range(retries):
            try:
                print(f"Attempting to connect to WebSocket: {self.base} (Attempt {attempt+1})")
                self.ws = await self.session.ws_connect(
                    self.base, autoclose=False, autoping=True, heartbeat=60
                )
                task = self.loop.create_task(self.process_messages())
                task.add_done_callback(self.on_close)
                await self.async_on_open()
                return  # Exit the loop if connection is successful
            except aiohttp.client_exceptions.ClientConnectorError as e:
                _LOGGER.error(f"Attempt {attempt+1} failed: {e}. Retrying in {delay} seconds...")
                await asyncio.sleep(delay)
                continue
        _LOGGER.error("Failed to connect after several attempts. Exiting.")
        self.on_error("Failed to connect after several attempts.")

    async def async_on_open(self) -> None:
        if not self.ws.closed:
            if self.open_callback is not None:
                await self.open_callback()

    async def process_messages(self):
        print(f" - process_messages started")
        async for msg in self.ws:
            try:
                await self.on_message(msg)
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error(
                    f" - Exception - process_messages: %s - traceback: %s - message: %s",
                    ex,
                    traceback.format_exc(),
                    msg,
                )

    async def on_message(self, message):
        if self.message_callback is not None:
            await self.message_callback(message)

    def on_error(self, error: Text = "Unspecified") -> None:
        _LOGGER.error(f" - WebSocket Error: %s", error)
        if self.error_callback is not None:
            asyncio.run_coroutine_threadsafe(
                self.error_callback(error), self.loop
            ).result()

    def on_close(self, future="") -> None:
        _LOGGER.info(f" - WebSocket Connection Closed. %s", future)
        if self.close_callback is not None:
            self.ws = None
            asyncio.run_coroutine_threadsafe(self.close_callback(), self.loop).result()

    async def send_message(self, message):
        if self.ws and not self.ws.closed:
            await self.ws.send_str(message)
        else:
            _LOGGER.warning(f"Attempted to send message but WebSocket is closed: {message}")

