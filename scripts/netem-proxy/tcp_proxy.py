#!/usr/bin/env python3
"""Small RTMP TCP relay for isolated tc-netem integration scenarios."""

from __future__ import annotations

import asyncio
import os


async def close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=2)
    except (asyncio.TimeoutError, OSError):
        pass


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(64 * 1024):
        writer.write(data)
        await writer.drain()


async def handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
) -> None:
    target_writer: asyncio.StreamWriter | None = None
    tasks: set[asyncio.Task] = set()
    try:
        target_reader, target_writer = await asyncio.open_connection(target_host, target_port)
        tasks = {
            asyncio.create_task(pipe(client_reader, target_writer)),
            asyncio.create_task(pipe(target_reader, client_writer)),
        }
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, asyncio.CancelledError):
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(close_writer(client_writer), close_writer(target_writer))


async def main() -> None:
    target_host = os.environ.get("RTMP_TARGET_HOST", "srs")
    target_port = int(os.environ.get("RTMP_TARGET_PORT", "1935"))
    listen_port = int(os.environ.get("RTMP_PROXY_PORT", "1935"))
    server = await asyncio.start_server(
        lambda reader, writer: handle(reader, writer, target_host, target_port),
        "0.0.0.0",
        listen_port,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
