"""A minimal WebSocket client in pure standard library.

The Chrome DevTools Protocol runs over a WebSocket, and Python's
standard library has no WebSocket client — so this is the smallest
honest one: RFC 6455 client handshake, masked text frames out,
fragmentation / ping / close handled coming in. It exists so Forge
Browser keeps its zero-dependency rule; it speaks only to a local
browser engine (127.0.0.1), never to the open internet.
"""

import base64
import os
import socket
import struct
from urllib.parse import urlparse

from .errors import BrowserError

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# A5: no single WebSocket frame or reassembled message may exceed
# this. The length comes from the peer, and an uncapped 64-bit
# length lets one frame OOM the 8 GB machine before anything
# notices. 64 MiB is far above any real CDP message (a 4K
# screenshot is a few MiB); anything bigger is refused before a
# byte of payload is read.
MAX_WS_MESSAGE_BYTES = 64 * 1024 * 1024


def _xor_mask(data, mask):
    """XOR data with the repeating 4-byte mask, in 8-byte blocks.

    G-P5: the old per-byte loop cost one Python iteration per byte
    (millions for a big CDP message on 2 cores). 8-byte int XOR
    keeps the mask aligned (8 is a multiple of 4), so the tail —
    always under 8 bytes — can fall back to the byte loop from a
    fresh mask boundary.
    """
    mask8 = int.from_bytes(mask * 2, "big")
    out = bytearray()
    size = len(data)
    index = 0
    while index + 8 <= size:
        out.extend((int.from_bytes(data[index:index + 8], "big") ^ mask8)
                   .to_bytes(8, "big"))
        index += 8
    if index < size:
        out.extend(byte ^ mask[i % 4] for i, byte in
                   enumerate(data[index:]))
    return bytes(out)


class WebSocket:
    def __init__(self, sock):
        self._sock = sock
        self._buffer = bytearray()
        self._fragments = []
        self._fragment_bytes = 0

    # -- connection ---------------------------------------------------
    @classmethod
    def connect(cls, url, timeout=15):
        parsed = urlparse(url)
        if parsed.scheme != "ws":
            raise BrowserError(f"expected a ws:// address, got {url!r}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise BrowserError(f"could not connect to the browser engine at {url} ({exc})") from exc
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        ws = cls(sock)
        try:
            sock.sendall(request.encode())
            response = ws._read_until(b"\r\n\r\n", limit=65536).decode("utf-8", errors="replace")
            if " 101" not in response.split("\r\n", 1)[0]:
                raise BrowserError(f"the browser engine refused the connection: {response.splitlines()[0] if response else 'no answer'}")
        except Exception:
            sock.close()
            raise
        return ws

    # -- framing --------------------------------------------------------
    def _read_until(self, marker, limit):
        while marker not in self._buffer:
            if len(self._buffer) > limit:
                raise BrowserError("the browser engine sent an oversized handshake")
            chunk = self._sock.recv(4096)
            if not chunk:
                raise BrowserError("the browser engine closed the connection during handshake")
            self._buffer.extend(chunk)
        end = self._buffer.index(marker) + len(marker)
        head = bytes(self._buffer[:end])
        del self._buffer[:end]
        return head

    def _read_exact(self, count):
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise BrowserError("the browser engine closed the connection")
            self._buffer.extend(chunk)
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        header.extend(mask)
        masked = _xor_mask(payload, mask)
        self._sock.sendall(bytes(header) + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_message(self):
        """Return the next complete text message, answering pings."""
        while True:
            first, second = self._read_exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            if length > MAX_WS_MESSAGE_BYTES:
                # A5: refuse before reading — the length is the
                # peer's word, and _read_exact would grow the
                # buffer to it without ever asking.
                raise BrowserError(
                    "the browser engine sent a message too big to read "
                    f"({length} bytes, over the {MAX_WS_MESSAGE_BYTES} "
                    "byte limit) — it was refused, not read")
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length) if length else b""
            if mask:
                payload = _xor_mask(payload, mask)
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:  # close
                raise BrowserError("the browser engine closed the connection")
            if opcode in (0x1, 0x2, 0x0):
                self._fragments.append(payload)
                self._fragment_bytes += len(payload)
                # A5: fragments accumulate too — a stream of small
                # frames must not add up past the cap either.
                if self._fragment_bytes > MAX_WS_MESSAGE_BYTES:
                    self._fragments = []
                    self._fragment_bytes = 0
                    raise BrowserError(
                        "the browser engine sent a message too big to read "
                        f"(over the {MAX_WS_MESSAGE_BYTES} byte limit) — "
                        "it was refused, not read")
                if fin:
                    whole = b"".join(self._fragments)
                    self._fragments = []
                    self._fragment_bytes = 0
                    return whole.decode("utf-8", errors="replace")
            # other opcodes (pong) are ignored

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
