"""Accept only current-UID loopback peers and bind child rpyc servers to loopback.
Use /proc UIDs; app imports do not patch, child patches fail open, and top-level imports use stdlib.
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
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple, cast, override

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


def unmap_ipv4_address(ip: str) -> str:
    """Fold an IPv4-mapped IPv6 address to its IPv4 form."""
    if ip.startswith("::ffff:") and "." in ip:
        return ip[len("::ffff:"):]
    return ip


def is_loopback(ip: str) -> bool:
    ip = unmap_ipv4_address(ip)
    return ip == "::1" or ip.startswith("127.")


def _decode_proc_endpoint(field: str) -> tuple[str, int]:
    """Decode a /proc/net/tcp address field such as "0100007F:1F90".
    The kernel prints each four-byte address group in host byte order."""
    ip_hex, _, port_hex = field.partition(":")
    raw = bytes.fromhex(ip_hex)
    if sys.byteorder == "little":
        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    family = socket.AF_INET if len(raw) == 4 else socket.AF_INET6
    return unmap_ipv4_address(socket.inet_ntop(family, raw)), int(port_hex, 16)


def parse_proc_tcp(text: str) -> list[TcpRow]:
    """Parse /proc/net/tcp{,6}: sl, local, remote, state, tx:rx, tr:when,
    retrnsmt, uid, timeout, and inode; skip malformed lines without hiding valid rows.
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


def _endpoint_of(addr: "tuple[str, int] | tuple[str, int, int, int]") -> tuple[str, int]:
    # AF_INET gives (ip, port); AF_INET6 gives (ip, port, flowinfo, scope_id).
    return unmap_ipv4_address(addr[0]), addr[1]


def uid_of_peer(sock: socket.socket) -> int | None:
    """Return the UID for the established row whose local endpoint is the peer.
    Match both full endpoints to reject TIME_WAIT ghosts; return None when no row matches.
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


def listening_rows_for_port(port: int) -> list[TcpRow]:
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
    """Return why an accepted socket is not a same-UID loopback peer, or None.
    Refuse unattributable peers because the table cannot distinguish them from foreign peers.
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


def loopback_uid_authenticator(sock: socket.socket) -> "tuple[socket.socket, None]":
    """Accept only same-UID loopback peers before the rpyc protocol starts.
    Unmodified clients pass without cooperation."""
    reason = refusal_reason(sock)
    if reason is not None:
        from rpyc.utils.authenticators import AuthenticationError
        _logger.error("refused rpyc connection: %s", reason)
        raise AuthenticationError(reason)
    return sock, None


# Child hook replaces rpyc's hostname-less wildcard bind with loopback and
# enforces current-UID peers; it fails open because app checks remain active.

_PATCHED_MARK = "_deckard_rpyc_guard_patched"
_TARGET_MODULE = "rpyc.utils.server"


def _compose_authenticator(existing: Callable[..., Any] | None) -> Callable[..., Any]:
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
        # Compute arguments before the single constructor call so bind failures
        # cannot trigger an unhardened retry that leaks a wildcard socket.
        call_args: tuple[Any, ...] = (self, *args)
        call_kwargs = kwargs
        try:
            bound = signature.bind(self, *args, **kwargs)
            if "authenticator" in params and not bound.arguments.get("socket_path"):
                # Authenticate every TCP bind because wildcard and loopback
                # listeners remain reachable by other local UIDs.
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

    @override
    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType | None:
        create = getattr(self._loader, "create_module", None)
        return cast("types.ModuleType | None", create(spec) if create is not None else None)

    @override
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

    @override
    def find_spec(self, fullname: str, path: "Sequence[str] | None" = None, target: "types.ModuleType | None" = None) -> importlib.machinery.ModuleSpec | None:
        # Skip this finder during its re-entrant sys.meta_path scan so a regular
        # finder supplies the real spec.
        if fullname != _TARGET_MODULE or self._resolving:
            return None
        self._resolving = True
        try:
            spec = importlib.util.find_spec(fullname)
        except Exception:
            # Fail open so a finder error does not abort the child's import;
            # regular finders can resolve the server module unpatched.
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
