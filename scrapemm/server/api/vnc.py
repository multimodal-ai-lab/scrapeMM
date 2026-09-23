"""A WebSocket bridge to the server's VNC display, for the CAPTCHA panel.

noVNC speaks RFB over a WebSocket; x11vnc speaks RFB over a plain TCP socket. Normally
`websockify` sits between them, but that is a whole extra process to supervise, and the
bridge itself is a pair of copy loops. Doing it here instead keeps the deployment to one
port and one auth token: the panel's socket is authenticated exactly like every other
API call.

The token travels as a query parameter because a browser's WebSocket constructor cannot
set an Authorization header.
"""

import asyncio
import logging
import os
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from scrapemm.common.paths import APP_NAME
from ..auth import check_token

logger = logging.getLogger(APP_NAME)

router = APIRouter(tags=["archive.today"])

VNC_HOST = os.getenv("SCRAPEMM_VNC_HOST", "127.0.0.1")
VNC_PORT = int(os.getenv("SCRAPEMM_VNC_PORT", "5900"))

# noVNC offers this subprotocol; accepting it keeps older clients happy, and modern
# ones are fine either way.
SUBPROTOCOL = "binary"

CHUNK = 64 * 1024


@router.websocket("/v1/archive-today/vnc")
async def vnc(websocket: WebSocket, token: Optional[str] = Query(default=None)) -> None:
    if not check_token(token):
        await websocket.close(code=4401, reason="Invalid or missing API key.")
        return

    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol=SUBPROTOCOL if SUBPROTOCOL in offered else None)

    try:
        reader, writer = await asyncio.open_connection(VNC_HOST, VNC_PORT)
    except OSError as e:
        logger.warning(f"The CAPTCHA panel could not reach VNC at "
                       f"{VNC_HOST}:{VNC_PORT}: {e}")
        await websocket.close(code=4502, reason=f"No VNC server at {VNC_HOST}:{VNC_PORT}.")
        return

    try:
        await asyncio.gather(_to_vnc(websocket, writer), _to_client(websocket, reader))
    except (WebSocketDisconnect, ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception:
        logger.debug("The CAPTCHA panel's VNC bridge ended with an error.", exc_info=True)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        if websocket.client_state is WebSocketState.CONNECTED:
            await websocket.close()


async def _to_vnc(websocket: WebSocket, writer: asyncio.StreamWriter) -> None:
    while True:
        data = await websocket.receive_bytes()
        writer.write(data)
        await writer.drain()


async def _to_client(websocket: WebSocket, reader: asyncio.StreamReader) -> None:
    while True:
        data = await reader.read(CHUNK)
        if not data:
            return  # x11vnc hung up
        await websocket.send_bytes(data)
