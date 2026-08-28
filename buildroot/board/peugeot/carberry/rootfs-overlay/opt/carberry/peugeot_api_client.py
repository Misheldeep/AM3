#!/usr/bin/env python3

import json
import select
import socket
import time
from pathlib import Path


DEFAULT_SOCKET = Path("/run/peugeot-bridge.sock")


class PeugeotApiError(RuntimeError):
    pass


class PeugeotApiClient:
    def __init__(self, path=DEFAULT_SOCKET, timeout=3.0):
        self.path = str(path)
        self.default_timeout = float(timeout)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.default_timeout)
        self.sock.connect(self.path)
        self.sock.setblocking(True)
        self.inbuf = bytearray()

        hello = self.recv(timeout=self.default_timeout)
        if not hello.get("ok") or hello.get("hello") != "peugeot-bridge":
            raise PeugeotApiError(f"unexpected bridge hello: {hello!r}")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def send(self, request):
        raw = (
            json.dumps(request, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        self.sock.sendall(raw)

    def recv(self, timeout=None):
        deadline = None
        if timeout is not None:
            deadline = time.monotonic() + float(timeout)

        while True:
            pos = self.inbuf.find(b"\n")
            if pos >= 0:
                raw = bytes(self.inbuf[:pos])
                del self.inbuf[:pos + 1]
                if not raw.strip():
                    continue
                return json.loads(raw.decode("utf-8"))

            wait = None
            if deadline is not None:
                wait = max(0.0, deadline - time.monotonic())
                if wait <= 0:
                    raise TimeoutError("bridge API receive timeout")

            readable, _, _ = select.select([self.sock], [], [], wait)
            if not readable:
                raise TimeoutError("bridge API receive timeout")

            data = self.sock.recv(4096)
            if not data:
                raise PeugeotApiError("bridge closed API connection")
            self.inbuf.extend(data)

    def request(self, request, timeout=None):
        self.send(request)
        if timeout is None:
            timeout = self.default_timeout
        response = self.recv(timeout=timeout)
        if not response.get("ok"):
            raise PeugeotApiError(response.get("error", "bridge API error"))
        return response

    def subscribe(
        self,
        can=True,
        semantic=True,
        channels=(1, 2),
        directions=("rx", "tx"),
    ):
        return self.request({
            "cmd": "subscribe",
            "can": bool(can),
            "semantic": bool(semantic),
            "channels": list(channels),
            "directions": list(directions),
        })
