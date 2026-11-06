"""Minimal HTTP/1.1 client with optional SOCKS5 and TLS support.

PS38 is driven through its public JSON endpoints. The socket is kept alive
between requests so a 1-second polling loop does not pay a TLS handshake for
every call, while the proxy can still be the AdsPower environment's SOCKS5
exit (same IP as the browser profile).
"""

from __future__ import annotations

import socket
import ssl
import struct
from typing import Any, Dict, List, Optional, Tuple


class HttpTransportError(RuntimeError):
    pass


class HttpResponse:
    def __init__(self, status: int, headers: Dict[str, List[str]], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name: str) -> str:
        values = self.headers.get(name.lower()) or []
        return values[0] if values else ""

    def json(self) -> Any:
        import json

        text = self.body.decode("utf-8", "replace").strip()
        return json.loads(text) if text else None

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise HttpTransportError("SOCKS5 响应中断")
        data.extend(chunk)
    return bytes(data)


def socks5_connect(
    host: str,
    port: int,
    user: Optional[str] = None,
    password: Optional[str] = None,
    timeout: float = 20,
) -> socket.socket:
    sock = socket.create_connection((host, int(port)), timeout=timeout)
    try:
        sock.settimeout(timeout)
        methods = b"\x00\x02" if user else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        version, method = _recv_exact(sock, 2)
        if version != 5:
            raise HttpTransportError("SOCKS5 版本错误")
        if method == 2:
            if not user:
                raise HttpTransportError("SOCKS5 代理需要认证")
            user_bytes = user.encode()
            pass_bytes = (password or "").encode()
            if len(user_bytes) > 255 or len(pass_bytes) > 255:
                raise HttpTransportError("SOCKS5 凭据过长")
            sock.sendall(
                b"\x01"
                + bytes([len(user_bytes)])
                + user_bytes
                + bytes([len(pass_bytes)])
                + pass_bytes
            )
            auth_version, status = _recv_exact(sock, 2)
            if auth_version != 1 or status != 0:
                raise HttpTransportError("SOCKS5 认证失败")
        elif method != 0:
            raise HttpTransportError(f"SOCKS5 不支持的认证方式：{method}")
        return sock
    except Exception:
        sock.close()
        raise


def socks5_connect_host(sock: socket.socket, host: str, port: int) -> None:
    target = host.encode("idna")
    if not target or len(target) > 255:
        raise HttpTransportError("SOCKS5 目标域名无效")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(target)]) + target + struct.pack("!H", port))
    head = _recv_exact(sock, 4)
    if head[0] != 5 or head[2] != 0:
        raise HttpTransportError("SOCKS5 连接响应无效")
    if head[1] != 0:
        raise HttpTransportError(f"SOCKS5 连接被拒绝（{head[1]}）")
    atyp = head[3]
    size = {1: 4, 4: 16}.get(atyp)
    if atyp == 3:
        size = _recv_exact(sock, 1)[0]
    elif size is None:
        raise HttpTransportError("SOCKS5 地址类型无效")
    _recv_exact(sock, size + 2)


class HttpClient:
    """One keep-alive connection per (host, scheme, proxy) instance."""

    def __init__(
        self,
        host: str,
        *,
        scheme: str = "https",
        port: Optional[int] = None,
        proxy: Optional[Dict[str, str]] = None,
        timeout: float = 30,
        user_agent: str = "",
        verify: bool = True,
    ) -> None:
        self.host = str(host).strip()
        self.scheme = "http" if str(scheme).lower() == "http" else "https"
        self.port = int(port or (80 if self.scheme == "http" else 443))
        self.proxy = proxy or {}
        self.timeout = max(3.0, float(timeout))
        self.verify = bool(verify)
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
        )
        self._sock: Optional[socket.socket] = None
        self._buffer = b""
        self._closed = True
        self._host_header = self.host
        if self.port not in (80, 443):
            self._host_header = f"{self.host}:{self.port}"

    # ── connection ────────────────────────────────────────────────────────

    def _connect(self) -> None:
        host = self.proxy.get("host") if self.proxy else None
        if host:
            sock = socks5_connect(
                host,
                int(self.proxy.get("port") or 443),
                self.proxy.get("username") or None,
                self.proxy.get("password") or None,
                timeout=self.timeout,
            )
            try:
                socks5_connect_host(sock, self.host, self.port)
            except Exception:
                sock.close()
                raise
        else:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            sock.settimeout(self.timeout)
            if self.scheme == "https":
                context = ssl.create_default_context()
                if not self.verify:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                sock = context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise
        self._sock = sock
        self._buffer = b""
        self._closed = False

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buffer = b""
        self._closed = True

    # ── request ───────────────────────────────────────────────────────────

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        cookies: Optional[Dict[str, str]] = None,
    ) -> HttpResponse:
        if self._sock is None:
            self._connect()
        request_headers = {
            "Host": self._host_header,
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
        }
        if cookies:
            request_headers["Cookie"] = "; ".join(
                f"{key}={value}" for key, value in cookies.items()
            )
        request_headers.update(headers or {})
        payload = body or b""
        if payload:
            request_headers["Content-Length"] = str(len(payload))
        raw = f"{method.upper()} {path} HTTP/1.1\r\n"
        raw += "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
        raw += "\r\n"
        try:
            assert self._sock is not None
            self._sock.sendall(raw.encode("utf-8") + payload)
            return self._read_response(method.upper())
        except (OSError, HttpTransportError, ssl.SSLError):
            self.close()
            raise
        except Exception:
            self.close()
            raise

    # ── response parsing ──────────────────────────────────────────────────

    def _read_until(self, marker: bytes) -> bytes:
        assert self._sock is not None
        while marker not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise HttpTransportError("HTTP 响应中断")
            self._buffer += chunk
        head, _, rest = self._buffer.partition(marker)
        self._buffer = rest
        return head

    def _read_exact(self, size: int) -> bytes:
        assert self._sock is not None
        while len(self._buffer) < size:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise HttpTransportError("HTTP 响应体不完整")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def _read_response(self, method: str = "GET") -> HttpResponse:
        head = self._read_until(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        if not lines or not lines[0]:
            raise HttpTransportError("HTTP 响应为空")
        parts = lines[0].split()
        if (
            len(parts) < 2
            or not parts[0].startswith("HTTP/")
            or not parts[1].isdigit()
            or len(parts[1]) != 3
        ):
            raise HttpTransportError("HTTP 响应状态行无效")
        status = int(parts[1])
        headers: Dict[str, List[str]] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers.setdefault(key.strip().lower(), []).append(value.strip())
        connection = (headers.get("connection") or [""])[0].lower()
        transfer = (headers.get("transfer-encoding") or [""])[0].lower()
        length_text = (headers.get("content-length") or [""])[0]
        if 100 <= status < 200:
            if status == 101:
                raise HttpTransportError("HTTP 协议升级不受支持")
            return self._read_response(method)
        if method == "HEAD" or status in (204, 304):
            body = b""
        elif "chunked" in transfer:
            body = self._read_chunked()
        elif length_text.isdigit():
            body = self._read_exact(int(length_text))
        elif length_text:
            raise HttpTransportError("HTTP Content-Length 无效")
        else:
            body = self._read_until_close()
        if "close" in connection or self._sock is None:
            self.close()
        return HttpResponse(status, headers, body)

    def _read_chunked(self) -> bytes:
        out = b""
        while True:
            line = self._read_until(b"\r\n")
            size_text = line.split(b";")[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise HttpTransportError("HTTP 分块长度无效") from exc
            if size < 0:
                raise HttpTransportError("HTTP 分块长度无效")
            if size == 0:
                while True:
                    trailer = self._read_until(b"\r\n")
                    if not trailer:
                        break
                break
            out += self._read_exact(size)
            if self._read_exact(2) != b"\r\n":
                raise HttpTransportError("HTTP 分块分隔符无效")
        return out

    def _read_until_close(self) -> bytes:
        assert self._sock is not None
        data = self._buffer
        self._buffer = b""
        while True:
            chunk = self._sock.recv(65536)
            if not chunk:
                break
            data += chunk
        self.close()
        return data


def parse_set_cookies(headers: Dict[str, List[str]]) -> List[Tuple[str, str, str]]:
    """Return ``(name, value, domain)`` for each Set-Cookie header."""
    result: List[Tuple[str, str, str]] = []
    for raw in headers.get("set-cookie") or []:
        parts = raw.split(";")
        if "=" not in parts[0]:
            continue
        name, value = parts[0].split("=", 1)
        domain = ""
        for attr in parts[1:]:
            key, _, attr_value = attr.strip().partition("=")
            if key.lower() == "domain":
                domain = attr_value.strip()
        result.append((name.strip(), value.strip(), domain))
    return result
