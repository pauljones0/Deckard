"""deckard_rpyc_guard attributes loopback peers through the socket table.

The guard reads /proc/net/tcp{,6} because loopback TCP carries no peer
credentials. Fixture text pins the decode of the kernel's hex rows; a live
socket pair pins the end-to-end attribution against this process.
"""
import os
import socket

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

from src.backend.PluginManager.backend_guard import deckard_rpyc_guard as guard

# One IPv4 LISTEN row: 127.0.0.1:8080, uid 1000, inode 43210. Column layout:
# sl local rem st tx:rx tr:when retrnsmt uid timeout inode ...
FIXTURE_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 43210 1 0000000000000000 100 0 0 10 0
   1: 0100007F:9C40 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1000        0 43211 1 0000000000000000 20 4 30 10 -1
garbage line that must not raise
   2: 0100007F:BAD
"""

# ::1 and an IPv4-mapped 127.0.0.1, both as the kernel prints them on a
# little-endian host: each 4-byte group in host byte order.
FIXTURE_TCP6 = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:0050 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 55001 1 0000000000000000 100 0 0 10 0
   1: 0000000000000000FFFF00000100007F:0051 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 55002 1 0000000000000000 100 0 0 10 0
"""


def check_fixture_parsing() -> None:
    rows = guard.parse_proc_tcp(FIXTURE_TCP)
    assert len(rows) == 2, rows
    listen, established = rows
    assert listen == guard.TcpRow("127.0.0.1", 8080, "0.0.0.0", 0, "0A", 1000, 43210), listen
    assert established.state == guard.TCP_ESTABLISHED and established.local_port == 40000, established
    print("PASS: IPv4 rows decode (little-endian groups, header and garbage skipped)")

    rows6 = guard.parse_proc_tcp(FIXTURE_TCP6)
    assert rows6[0].local_ip == "::1" and rows6[0].local_port == 80, rows6[0]
    assert rows6[1].local_ip == "127.0.0.1" and rows6[1].local_port == 81, rows6[1]
    print("PASS: IPv6 rows decode; an IPv4-mapped address folds to IPv4")


def check_loopback_predicate() -> None:
    for ip, expected in (
        ("127.0.0.1", True),
        ("127.5.4.3", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("192.168.1.5", False),
        ("::ffff:192.168.1.5", False),
        ("fe80::1", False),
    ):
        assert guard.is_loopback(ip) is expected, ip
    print("PASS: is_loopback accepts 127/8, ::1 and mapped loopback only")


def check_live_attribution() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]

    client = socket.socket()
    client.connect(("127.0.0.1", port))
    server_side, _ = listener.accept()
    try:
        assert guard.uid_of_peer(server_side) == os.getuid(), "peer uid must be ours"
        assert guard.refusal_reason(server_side) is None, guard.refusal_reason(server_side)
        print("PASS: a live loopback peer attributes to this uid and is accepted")

        rows = guard.listen_rows_of_port(port)
        inode = os.fstat(listener.fileno()).st_ino
        assert any(r.inode == inode and r.local_ip == "127.0.0.1" for r in rows), rows
        print("PASS: listen_rows_of_port finds the listener with its inode")

        assert guard.pid_owns_inode(os.getpid(), inode) is True
        assert guard.pid_owns_inode(os.getpid(), 2**31 + 12345) is False
        assert guard.pid_owns_inode(2**22 + 999, inode) is False, "an absent pid owns nothing"
        print("PASS: pid_owns_inode matches /proc/<pid>/fd and nothing else")

        # A foreign uid refuses. The uid lookup is swapped because this suite
        # runs under one real uid.
        real = guard.uid_of_peer
        guard.uid_of_peer = lambda s: os.getuid() + 1
        try:
            reason = guard.refusal_reason(server_side)
        finally:
            guard.uid_of_peer = real
        assert reason is not None and "belongs to uid" in reason, reason

        # An unattributable peer refuses too.
        guard.uid_of_peer = lambda s: None
        try:
            reason = guard.refusal_reason(server_side)
        finally:
            guard.uid_of_peer = real
        assert reason is not None and "attributes" in reason, reason
        print("PASS: a foreign or unattributable peer is refused")
    finally:
        server_side.close()
        client.close()
        listener.close()


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_peer_credentials")
    check_fixture_parsing()
    check_loopback_predicate()
    check_live_attribution()
    print("PASS: scenario_peer_credentials")


if __name__ == "__main__":
    main()
