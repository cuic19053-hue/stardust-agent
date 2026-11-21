"""Start the workstation on a loopback socket accessible only to its window."""

import argparse
import faulthandler
import hmac
import os
import socket
import sys
import threading

import uvicorn
from starlette.responses import PlainTextResponse


class DesktopAccess:
    def __init__(self, app, port, token):
        self.app = app
        self.host = f"127.0.0.1:{port}".encode("ascii")
        self.token = token.encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            supplied = headers.get(b"x-stardust-desktop-token", b"")
            if headers.get(b"host") != self.host or not hmac.compare_digest(supplied, self.token):
                await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def serve():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0, choices=range(65536), metavar="0-65535")
    preferred = parser.parse_args().port
    token = os.environ.pop("STARDUST_DESKTOP_TOKEN", "")
    diagnostics = os.environ.pop("STARDUST_DESKTOP_DIAGNOSTICS", "") == "1"
    if len(token) < 32 or not os.environ.get("PROPOSAL_AGENT_DATA_DIR"):
        raise RuntimeError("The desktop launcher must configure the local token and data directory.")

    if diagnostics:
        faulthandler.dump_traceback_later(25, repeat=True)

    from server.app import app

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", preferred))
        except OSError:
            if preferred == 0:
                raise
            listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(128)
        server = uvicorn.Server(uvicorn.Config(
            DesktopAccess(app, port, token),
            host="127.0.0.1", port=port, proxy_headers=False,
            access_log=diagnostics, loop="asyncio", http="h11", ws="none",
        ))

        def watch_launcher():
            sys.stdin.buffer.read()
            server.should_exit = True

        threading.Thread(target=watch_launcher, daemon=True).start()
        print(f"STARDUST_PORT {port}", flush=True)
        server.run(sockets=[listener])


if __name__ == "__main__":
    serve()
