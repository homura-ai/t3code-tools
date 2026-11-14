#!/usr/bin/env python3
"""Forward TCP connections from BIND_ADDRESS:PORT to 127.0.0.1:PORT.

Usage: tcp-relay.py BIND_ADDRESS PORT

Used to reach a T3 server that listens only on loopback from another machine,
for example over a tailnet: `tcp-relay.py <tailnet IP> 3773`. Anyone who can
reach BIND_ADDRESS can then reach the port, and the relay adds no
authentication of its own, so bind it only to a private network address.
"""
import asyncio
import sys


async def pipe(r, w):
    try:
        while (d := await r.read(65536)):
            w.write(d)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def handle(r, w, port):
    try:
        r2, w2 = await asyncio.open_connection('127.0.0.1', port)
    except Exception:
        w.close()
        return
    await asyncio.gather(pipe(r, w2), pipe(r2, w))


async def main(bind, port):
    server = await asyncio.start_server(lambda r, w: handle(r, w, port), bind, port)
    await server.serve_forever()


if __name__ == '__main__':
    if len(sys.argv) != 3 or not sys.argv[2].isdigit():
        sys.exit(__doc__.strip())
    asyncio.run(main(sys.argv[1], int(sys.argv[2])))
