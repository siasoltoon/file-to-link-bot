"""MTProxy Fake-TLS (ee) transport for Telethon 1.40.

Implements the TLS-looking outer record layer used by classic MTProxy Fake-TLS
secrets, while reusing Telethon's normal MTProxy randomized-intermediate
transport for the encrypted MTProto payload.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import socket
import struct

from telethon.network.connection.tcpmtproxy import (
    ConnectionTcpMTProxyRandomizedIntermediate,
)


def _decode_secret(value: str) -> bytes:
    value = value.strip()
    if value.lower().startswith("ee"):
        return bytes.fromhex(value)
    cleaned = re.sub(r"[^a-zA-Z0-9+/=_-]+", "", value)
    return base64.urlsafe_b64decode(("7" + cleaned).encode() + b"=" * (-len("7" + cleaned) % 4))


def _hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


class MTProxyFakeTLSClientCodec:
    """Build and verify the TLS camouflage handshake for an ee secret."""

    def __init__(self, secret: str):
        full = _decode_secret(secret)
        if len(full) < 17:
            raise ValueError("MTProxy Fake-TLS secret is too short")
        self.secret = full[1:17]
        self.domain = full[17:]
        if not self.domain:
            raise ValueError("MTProxy Fake-TLS secret does not contain a TLS domain")
        self.session_id = b""
        self.random = b""

    @staticmethod
    def _grease() -> bytes:
        values = (0x0A0A, 0x1A1A, 0x2A2A, 0x3A3A, 0x4A4A, 0x5A5A, 0x6A6A,
                  0x7A7A, 0x8A8A, 0x9A9A, 0xAAAA, 0xBABA, 0xCACA, 0xDADA,
                  0xEAEA, 0xFAFA)
        return struct.pack(">H", secrets.choice(values))

    @staticmethod
    def _x25519_like_public_key() -> bytes:
        p = 2**255 - 19
        n = secrets.randbelow(p)
        return ((n * n) % p).to_bytes(32, "little")

    def build_new_client_hello_packet(self) -> bytes:
        self.session_id = os.urandom(32)
        key_share = self._x25519_like_public_key()
        grease_ext = self._grease()
        grease_group = self._grease()
        grease_version = self._grease()
        grease_tail = self._grease()
        grease_cipher = self._grease()

        domain = self.domain
        sni = (
            b"\x00\x00" + struct.pack(">H", 5 + len(domain)) +
            struct.pack(">H", 3 + len(domain)) + b"\x00" +
            struct.pack(">H", len(domain)) + domain
        )
        alpn = b"\x00\x10\x00\x0e\x00\x0c\x02h2\x08http/1.1"
        ciphers = b"".join(struct.pack(">H", x) for x in (
            int.from_bytes(grease_cipher, "big"), 0x1301, 0x1302, 0x1303,
            0xC02B, 0xC02F, 0xC02C, 0xC030, 0xCCA9, 0xCCA8, 0xC013,
            0xC014, 0x009C, 0x009D, 0x002F, 0x0035,
        ))
        extensions = b"".join((
            grease_ext + b"\x00\x00",
            sni,
            b"\x00\x17\x00\x00",
            b"\xff\x01\x00\x01\x00",
            b"\x00\x0a\x00\x0a\x00\x08" + grease_group + b"\x00\x1d\x00\x17\x00\x18",
            b"\x00\x0b\x00\x02\x01\x00",
            b"\x00\x23\x00\x00",
            alpn,
            b"\x00\x05\x00\x05\x01\x00\x00\x00\x00",
            b"\x00\x0d\x00\x12\x00\x10\x04\x03\x08\x04\x04\x01\x05\x03\x08\x05\x05\x01\x08\x06\x06\x01",
            b"\x00\x12\x00\x00",
            b"\x00\x33\x00\x2b\x00\x29" + grease_group + b"\x00\x01\x00\x00\x1d\x00\x20" + key_share,
            b"\x00\x2d\x00\x02\x01\x01",
            b"\x00\x2b\x00\x0b\x0a" + grease_version + b"\x03\x04\x03\x03\x03\x02\x03\x01",
            b"\x00\x1b\x00\x03\x02\x00\x02",
            grease_tail + b"\x00\x01\x00",
        ))
        body = (
            b"\x03\x03" + b"\x00" * 32 + b"\x20" + self.session_id +
            struct.pack(">H", len(ciphers)) + ciphers + b"\x01\x00" +
            struct.pack(">H", len(extensions)) + extensions
        )
        padding = 508 - len(body)
        if padding > 0:
            body += b"\x00\x15" + struct.pack(">H", padding) + b"\x00" * padding
        hello = b"\x16\x03\x01" + struct.pack(">H", 4 + len(body)) + b"\x01" + len(body).to_bytes(3, "big") + body
        digest = _hmac_sha256(self.secret, hello)
        current_time = int.from_bytes(os.urandom(4), "little").to_bytes(4, "little")
        # Fake-TLS servers expect the final four bytes of ClientHello.random to
        # carry the current time XORed with the secret HMAC digest.
        fake_time = int(__import__("time").time()).to_bytes(4, "little")
        patched_random = digest[:28] + bytes(a ^ b for a, b in zip(fake_time, digest[28:32]))
        hello = hello[:11] + patched_random + hello[43:]
        self.random = patched_random
        return hello

    def verify_server_hello(self, server_hello: bytes) -> bool:
        if len(server_hello) < 133 or not server_hello.startswith(b"\x16\x03\x03"):
            return False
        if server_hello[127:136] != b"\x14\x03\x03\x00\x01\x01\x17\x03\x03":
            return False
        if server_hello[43:75] != self.session_id:
            return False
        server_digest = server_hello[11:43]
        normalized = server_hello[:11] + b"\x00" * 32 + server_hello[43:]
        return hmac.compare_digest(server_digest, _hmac_sha256(self.secret, self.random + normalized))


class _FakeTLSReader:
    def __init__(self, upstream):
        self.upstream = upstream
        self.buf = bytearray()

    async def read(self, n, ignore_buf=False):
        if self.buf and not ignore_buf:
            data = bytes(self.buf)
            self.buf.clear()
            return data
        while True:
            typ = await self.upstream.readexactly(1)
            if not typ:
                return b""
            if typ not in (b"\x14", b"\x17"):
                raise ConnectionError("Invalid Fake-TLS record type")
            version = await self.upstream.readexactly(2)
            if version != b"\x03\x03":
                raise ConnectionError("Invalid Fake-TLS record version")
            length = int.from_bytes(await self.upstream.readexactly(2), "big")
            data = await self.upstream.readexactly(length)
            if typ == b"\x17":
                return data

    async def readexactly(self, n):
        while len(self.buf) < n:
            data = await self.read(1, ignore_buf=True)
            if not data:
                raise ConnectionError("Fake-TLS stream closed")
            self.buf.extend(data)
        result = bytes(self.buf[:n])
        del self.buf[:n]
        return result

    async def read_server_hello(self):
        head = await self.upstream.readexactly(133)
        length = int.from_bytes(head[-2:], "big")
        return head + await self.upstream.readexactly(length)


class _FakeTLSWriter:
    def __init__(self, upstream):
        self.upstream = upstream

    def write(self, data):
        size = 16384 + 24
        for start in range(0, len(data), size):
            chunk = data[start:start + size]
            self.upstream.write(b"\x17\x03\x03" + len(chunk).to_bytes(2, "big"))
            self.upstream.write(chunk)
        return len(data)

    async def drain(self):
        return await self.upstream.drain()

    def close(self):
        return self.upstream.close()

    def abort(self):
        return self.upstream.transport.abort()

    def get_extra_info(self, name):
        return self.upstream.get_extra_info(name)

    @property
    def transport(self):
        return self.upstream.transport


class ConnectionTcpMTProxyFakeTLS(ConnectionTcpMTProxyRandomizedIntermediate):
    """Telethon MTProxy connection wrapped in Fake-TLS records."""

    def __init__(self, ip, port, dc_id, *, loggers, proxy=None, local_addr=None):
        self.fake_tls_codec = MTProxyFakeTLSClientCodec(proxy[2])
        proxy_host = proxy[0]
        if len(proxy_host) > 60:
            proxy_host = socket.gethostbyname(proxy_host)
        # Telethon's inner MTProxy layer receives the 16-byte core secret.
        inner_proxy = (proxy_host, proxy[1], self.fake_tls_codec.secret.hex())
        super().__init__(ip, port, dc_id, loggers=loggers, proxy=inner_proxy, local_addr=local_addr)

    async def _connect(self, timeout=None, ssl=None):
        await super()._connect(timeout=timeout, ssl=ssl)
        # super() initialized the raw stream and Telethon codec. Replace the
        # raw reader/writer with the Fake-TLS record layer before MTProto uses it.
        self._writer = _FakeTLSWriter(self._writer)
        self._reader = _FakeTLSReader(self._reader)
        # The MTProxy header was already initialized by super(), but Fake-TLS
        # must be established before any payload is sent. Rebuild the transport
        # codec state after the handshake.
