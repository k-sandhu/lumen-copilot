"""Local-only proof of SDK SSE completion versus HTTP/1 keepalive reuse."""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
from openai import AsyncOpenAI

seen = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        seen.append(self.client_address)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        payload = {
            "id": "local",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "synthetic",
            "choices": [
                {"index": 0, "delta": {"content": "ok"}, "finish_reason": None}
            ],
        }
        chunks = [
            ("data: " + json.dumps(payload) + "\n\n").encode(),
            b"data: [DONE]\n\n",
        ]
        try:
            for chunk in chunks:
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            # A legal chunked HTTP tail arrives after the SSE protocol terminal.
            time.sleep(0.03)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


async def run(url):
    async with AsyncOpenAI(api_key="synthetic-local-only", base_url=url) as client:
        for _ in range(2):
            stream = await client.chat.completions.create(
                model="synthetic",
                messages=[{"role": "user", "content": "local"}],
                stream=True,
            )
            async for _ in stream:
                pass
    sdk_count = len(set(seen))
    seen.clear()
    async with httpx.AsyncClient() as client:
        for _ in range(2):
            async with client.stream(
                "POST", url + "/chat/completions", json={}
            ) as response:
                async for _ in response.aiter_bytes():
                    pass
    return {
        "requests_per_arm": 2,
        "sdk_distinct_connections": sdk_count,
        "fully_consumed_control_distinct_connections": len(set(seen)),
        "tail_delay_seconds": 0.03,
    }


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    print(json.dumps(asyncio.run(run(f"http://127.0.0.1:{server.server_port}/v1"))))
finally:
    server.shutdown()
    server.server_close()
    thread.join()
