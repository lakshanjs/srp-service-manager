"""Tiny dependency-free Redis/Memurai client (raw RESP over a socket).

Only what the Memurai "Lists" panel needs: enumerate list-type keys, read their
lengths, and delete them. Avoids pulling in redis-py (and bundling it into the exe).
"""

import socket

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6379
MAX_LISTS = 2000  # safety cap so a huge keyspace can't flood the UI


class RedisError(Exception):
    pass


class _Resp:
    """Minimal synchronous RESP connection."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, password=None, timeout=5):
        self.host, self.port, self.password, self.timeout = host, int(port), password, timeout
        self.sock = None
        self.buf = b""

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise RedisError(f"could not connect to {self.host}:{self.port} ({e})")
        self.buf = b""
        if self.password:
            self.command("AUTH", self.password)
        return self

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # --- wire encoding / decoding ---

    @staticmethod
    def _encode(*args):
        parts = [f"*{len(args)}\r\n".encode()]
        for a in args:
            b = str(a).encode("utf-8")
            parts.append(b"$" + str(len(b)).encode() + b"\r\n" + b + b"\r\n")
        return b"".join(parts)

    def _read_line(self):
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RedisError("connection closed by server")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _read_bytes(self, n):
        while len(self.buf) < n + 2:  # payload + trailing CRLF
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RedisError("connection closed by server")
            self.buf += chunk
        data = self.buf[:n]
        self.buf = self.buf[n + 2:]
        return data

    def _read_reply(self):
        line = self._read_line()
        kind, rest = line[:1], line[1:]
        if kind == b"+":
            return rest.decode("utf-8", "replace")
        if kind == b"-":
            raise RedisError(rest.decode("utf-8", "replace"))
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            return None if n < 0 else self._read_bytes(n).decode("utf-8", "replace")
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._read_reply() for _ in range(n)]
        raise RedisError(f"unexpected reply: {line!r}")

    def command(self, *args):
        self.sock.sendall(self._encode(*args))
        return self._read_reply()

    def pipeline(self, commands):
        """Send many commands at once and collect all replies (one round-trip)."""
        if not commands:
            return []
        self.sock.sendall(b"".join(self._encode(*c) for c in commands))
        return [self._read_reply() for _ in commands]


def _scan_lists(conn):
    """Return all list-type keys (server-side filtered via SCAN ... TYPE list)."""
    cursor, keys = "0", []
    while True:
        cursor, batch = conn.command("SCAN", cursor, "COUNT", 500, "TYPE", "list")
        keys.extend(batch)
        if str(cursor) == "0" or len(keys) >= MAX_LISTS:
            break
    return keys[:MAX_LISTS]


def list_lists(host=DEFAULT_HOST, port=DEFAULT_PORT, password=None):
    """Return [{'key': name, 'len': length}, ...] for every list in the DB."""
    with _Resp(host, port, password) as conn:
        keys = _scan_lists(conn)
        lengths = conn.pipeline([("LLEN", k) for k in keys])
        return [{"key": k, "len": n} for k, n in zip(keys, lengths)]


def delete_keys(host, port, keys, password=None):
    """Delete the given keys; returns the number actually removed."""
    keys = list(keys)
    if not keys:
        return 0
    with _Resp(host, port, password) as conn:
        return int(conn.command("DEL", *keys))


def empty_all_lists(host=DEFAULT_HOST, port=DEFAULT_PORT, password=None):
    """Delete every list-type key; returns the number removed."""
    with _Resp(host, port, password) as conn:
        keys = _scan_lists(conn)
        if not keys:
            return 0
        removed = 0
        for i in range(0, len(keys), 200):  # delete in batches
            removed += int(conn.command("DEL", *keys[i:i + 200]))
        return removed


def ping(host=DEFAULT_HOST, port=DEFAULT_PORT, password=None):
    try:
        with _Resp(host, port, password) as conn:
            return conn.command("PING") == "PONG"
    except RedisError:
        return False
