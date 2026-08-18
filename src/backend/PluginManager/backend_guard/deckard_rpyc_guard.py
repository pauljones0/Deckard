"""Peer checks and a loopback rewrite for the plugin-backend rpyc sockets.

The app imports this module as library code: the frontend servers gate
accepted connections with refusal_reason, and register_backend pins the
offered port to the launched process with listen_rows_of_port and
pid_owns_inode. launch_backend also copies this file into every plugin venv
next to a .pth line, and puts its directory on the child's PYTHONPATH, where
sitecustomize.py imports it. Inside a child it runs as top-level
"deckard_rpyc_guard" and installs an import hook that rebinds a
hostname-less rpyc server to 127.0.0.1 and adds the peer-UID authenticator.
The app-side import under src.backend... installs no hook.

Loopback TCP carries no peer credentials, so /proc/net/tcp{,6} supplies
them: the row whose local endpoint is the peer's endpoint names the UID that
owns the peer socket. This file must stay stdlib-only: inside a plugin venv
it has no app code and no packages beyond the plugin's own.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import logging
import os
import socket
import sys
import types
from collections.abc import Callable
from typing import Any, NamedTuple

TCP_ESTABLISHED = "01"
TCP_LISTEN = "0A"

_PROC_TCP_PATHS = ("/proc/net/tcp", "/proc/net/tcp6")

_logger = logging.getLogger("deckard_rpyc_guard")


class TcpRow(NamedTuple):
    local_ip: str
    local_port: int
    remote_ip: str
    remote_port: int
    state: str
    uid: int
    inode: int


def normalize_ip(ip: str) -> str:
    """Fold an IPv4-mapped IPv6 address to its IPv4 form."""
    if ip.startswith("::ffff:") and "." in ip:
        return ip[len("::ffff:"):]
    return ip


def is_loopback(ip: str) -> bool:
    ip = normalize_ip(ip)
    return ip == "::1" or ip.startswith("127.")


def _decode_proc_endpoint(field: str) -> tuple[str, int]:
    """Decode a /proc/net/tcp address field such as "0100007F:1F90".

    The kernel prints each 4-byte group of the address in host byte order.
    """
    ip_hex, _, port_hex = field.partition(":")
    raw = bytes.fromhex(ip_hex)
    if sys.byteorder == "little":
        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    family = socket.AF_INET if len(raw) == 4 else socket.AF_INET6
    return normalize_ip(socket.inet_ntop(family, raw)), int(port_hex, 16)


def parse_proc_tcp(text: str) -> list[TcpRow]:
    """Parse the body of /proc/net/tcp or /proc/net/tcp6.

    Columns: sl, local, remote, state, tx:rx, tr:when, retrnsmt, uid,
    timeout, inode. A malformed line is skipped, not fatal: one bad row must
    not blind the caller to the others.
    """
    rows: list[TcpRow] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 10 or not parts[0].endswith(":"):
            continue
        try:
            local_ip, local_port = _decode_proc_endpoint(parts[1])
            remote_ip, remote_port = _decode_proc_endpoint(parts[2])
            rows.append(TcpRow(local_ip, local_port, remote_ip, remote_port,
                               parts[3], int(parts[7]), int(parts[9])))
        except (ValueError, OSError):
            continue
    return rows


def _read_rows() -> list[TcpRow]:
    rows: list[TcpRow] = []
    for path in _PROC_TCP_PATHS:
        try:
            with open(path, encoding="ascii") as f:
                rows.extend(parse_proc_tcp(f.read()))
        except OSError:
            continue
    return rows


def _endpoint_of(addr: tuple) -> tuple[str, int]:
    # AF_INET gives (ip, port); AF_INET6 gives (ip, port, flowinfo, scope_id).
    return normalize_ip(addr[0]), addr[1]


def uid_of_peer(sock: socket.socket) -> int | None:
    """UID that owns the peer end of an established loopback connection.

    The peer's socket appears in the table as the row whose local endpoint is
    the peer's and whose remote endpoint is this socket's; the full 4-tuple
    match keeps a TIME_WAIT ghost of an earlier connection out. None when no
    row matches; the caller decides the fail direction.
    """
    try:
        peer = _endpoint_of(sock.getpeername())
        local = _endpoint_of(sock.getsockname())
    except OSError:
        return None
    for row in _read_rows():
        if (row.state == TCP_ESTABLISHED
                and (row.local_ip, row.local_port) == peer
                and (row.remote_ip, row.remote_port) == local):
            return row.uid
    return None


def listen_rows_of_port(port: int) -> list[TcpRow]:
    return [row for row in _read_rows()
            if row.state == TCP_LISTEN and row.local_port == port]


def pid_owns_inode(pid: int, inode: int) -> bool:
    """True when /proc/<pid>/fd holds the socket with this inode."""
    fd_dir = f"/proc/{pid}/fd"
    target = f"socket:[{inode}]"
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return False
    for name in names:
        try:
            if os.readlink(os.path.join(fd_dir, name)) == target:
                return True
        except OSError:
            continue
    return False


def refusal_reason(sock: socket.socket) -> str | None:
    """Why this accepted socket must be refused, or None to accept.

    Accepts only a loopback peer that the current UID owns. A peer the
    socket table cannot attribute refuses too, because an unattributable
    peer and a foreign one are indistinguishable here.
    """
    try:
        peer_ip, peer_port = _endpoint_of(sock.getpeername())
    except OSError as e:
        return f"getpeername failed: {e}"
    if not is_loopback(peer_ip):
        return f"peer {peer_ip}:{peer_port} is not loopback"
    uid = uid_of_peer(sock)
    if uid is None:
        return f"no socket-table row attributes peer {peer_ip}:{peer_port}"
    if uid != os.getuid():
        return f"peer {peer_ip}:{peer_port} belongs to uid {uid}, not uid {os.getuid()}"
    return None


def loopback_uid_authenticator(sock: socket.socket) -> tuple[socket.socket, Any]:
    """rpyc authenticator: accept same-UID loopback peers only.

    rpyc runs this on the accepted socket before the protocol starts, so an
    unmodified client passes with no cooperation.
    """
    reason = refusal_reason(sock)
    if reason is not None:
        from rpyc.utils.authenticators import AuthenticationError
        _logger.error("refused rpyc connection: %s", reason)
        raise AuthenticationError(reason)
    return sock, None


# Child-side import hook. rpyc resolves a missing server hostname with
# AI_PASSIVE, which binds the wildcard address, so an unmodified backend
# serves its netref surface to any host that reaches the port. The hook
# rewrites that one case to loopback and injects the authenticator above.
# Every path fails open: a broken guard must never stop a backend from
# starting, and the app-side checks still hold without it.

_PATCHED_MARK = "_deckard_rpyc_guard_patched"
_TARGET_MODULE = "rpyc.utils.server"


def _compose_authenticator(existing: Callable | None) -> Callable:
    if existing is None:
        return loopback_uid_authenticator

    def composed_authenticator(sock: socket.socket) -> Any:
        # The peer-UID check raises on refusal; a plugin's own authenticator
        # then sees the socket it expects and supplies the return value.
        loopback_uid_authenticator(sock)
        return existing(sock)

    return composed_authenticator


def _patch_server_class(module: types.ModuleType) -> None:
    import functools
    import inspect

    server_cls = module.Server
    original = server_cls.__init__
    if getattr(original, _PATCHED_MARK, False):
        return
    signature = inspect.signature(original)
    params = signature.parameters

    @functools.wraps(original)
    def guarded_init(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Compute the rewrite here, and call the constructor exactly once
        # below. The constructor binds and listens, so a call inside the try
        # would let a bind failure fall through to a second, unhardened
        # construction that leaks the first socket and serves the wildcard.
        call_args: tuple = (self, *args)
        call_kwargs = kwargs
        try:
            bound = signature.bind(self, *args, **kwargs)
            if "authenticator" in params and not bound.arguments.get("socket_path"):
                # The peer-UID gate goes on every TCP server, whatever hostname
                # the caller chose: an explicit wildcard or a loopback bind is
                # still reachable by another local UID without it.
                bound.arguments["authenticator"] = _compose_authenticator(
                    bound.arguments.get("authenticator"))
                if "hostname" in params and bound.arguments.get("hostname") in (None, ""):
                    # rpyc resolves a missing hostname to the wildcard address.
                    # Pin it to loopback of the family the server will bind.
                    bound.arguments["hostname"] = "::1" if bound.arguments.get("ipv6") else "127.0.0.1"
                call_args, call_kwargs = bound.args, bound.kwargs
        except Exception:
            # The argument rewrite failed, so the server keeps its own
            # arguments. The single construction below still runs.
            _logger.exception("rpyc loopback guard could not rewrite the server arguments")
        return original(*call_args, **call_kwargs)

    setattr(guarded_init, _PATCHED_MARK, True)
    server_cls.__init__ = guarded_init


class _PatchingLoader(importlib.abc.Loader):
    def __init__(self, loader: importlib.abc.Loader) -> None:
        self._loader = loader

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType | None:
        create = getattr(self._loader, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module: types.ModuleType) -> None:
        self._loader.exec_module(module)
        try:
            _patch_server_class(module)
        except Exception:
            _logger.exception("rpyc loopback guard failed to arm; the server keeps its default bind")


class _RpycServerFinder(importlib.abc.MetaPathFinder):
    """Meta-path finder that patches rpyc.utils.server as it loads."""

    def __init__(self) -> None:
        self._resolving = False

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> importlib.machinery.ModuleSpec | None:
        # The re-entrant find_spec call below scans sys.meta_path again; the
        # flag makes this finder answer None on that inner pass, so the
        # regular path finder supplies the real spec.
        if fullname != _TARGET_MODULE or self._resolving:
            return None
        self._resolving = True
        try:
            spec = importlib.util.find_spec(fullname)
        except Exception:
            # Fail open: a finder that raises here would abort the child's
            # own `import rpyc.utils.server`, so the backend must never start
            # a server it needs. Let the regular finders resolve it unpatched.
            _logger.exception("rpyc loopback guard finder failed; the module loads unpatched")
            return None
        finally:
            self._resolving = False
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PatchingLoader(spec.loader)
        return spec


def _install() -> None:
    try:
        existing = sys.modules.get(_TARGET_MODULE)
        if existing is not None:
            _patch_server_class(existing)
            return
        sys.meta_path.insert(0, _RpycServerFinder())
    except Exception:
        _logger.exception("rpyc loopback guard failed to install")


if __name__ == "deckard_rpyc_guard":
    _install()
