"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

Log redaction. This scrubs PII from every log record before a sink sees it,
so a user shares logs.log without leaking a username, a home directory
layout, or a credential inside a url, a query parameter, an Authorization
header, or a settings or header dict.

This installs a core-level loguru patcher rather than a per-sink wrapper.
Logger._log applies core.patcher to the record before it fans out to
handler.emit (loguru 0.7 _logger.py), so one install covers every sink that
config_logger() adds, which are logs/logs.log, stderr, the gl.logs ring
behind the About dialog and the enqueued plugins.log sink, and it keeps
covering them across a log.remove() and log.add() cycle. A wrapper around the
file sink instead loses loguru's path-sink rotation, because a callable sink
does not rotate, and every sink needs its own wrapper.

A patcher cannot scrub {exception}. Each sink formats the raw (type, value,
tb) tuple itself at emit time, and a traceback frame path is the main leak,
because the central exception hooks route a full traceback into logs.log. So
for a record that carries an exception, redact_record() formats the traceback
itself with the stdlib, including a chained one, scrubs it, folds it into the
message, and clears record["exception"], so no sink formats the raw frames.
That has one side effect. The diagnose=True local-variable dumps of loguru
stop reaching the sinks, and a variable value is the worst PII in a shareable
log.

log_hooks.install_exception_hooks() calls install_log_redaction(). The hooks
route a full traceback into the sinks, so they must never fire without the
scrubbing layer. main()'s boot path gets redaction through that call, and
scenario_log_redaction asserts the pairing, because it installs the hooks
alone, so a removal of the call fails the harness.

What this redacts and what it keeps. scrub() is pure and stdlib-only, so a
unit test runs it without loguru, and every pattern compiles once at import.

The home directory, in its expanduser, realpath and $HOME spellings, becomes
"~". A guard on both sides keeps /home/nazareth, /var/home/naz and
/home/naz.old whole. A path stays readable, so /home/x/dev/App/src/y.py
becomes ~/dev/App/src/y.py.

The username, as a path segment such as /run/media/<user>/.., which includes
a dot-suffix form such as /home/<user>.old, and as the user part of
user@host. Never as a bare word, so a common-word username leaves ordinary
prose whole.

A url credential. scheme://user:pass@host and scheme://user@host become
scheme://***@host, and the scheme, the port and the path stay, so a store-fetch
url stays readable.

A host and an address. LAN topology is the leak a shared log carries most,
because a plugin logs the url it talks to, such as a Home Assistant instance or
an MQTT broker, and that names the machines on the user's network. Five rules
cover it, and each one runs its candidate through the same allowlist.

  * The host of a url, in any scheme, becomes <host>, or <ip> for an address
    literal. mqtt://ha.local:1883/x reads mqtt://<host>:1883/x, so a connection
    failure still names the service and the path.
  * The host of a user@host form, when it carries a dot or is a bracketed
    address. A single-label host such as build-host stays, because a bare word
    after an "@" is as often prose as a machine. These host rules run before the
    username rule, so user@ha.local scrubs whole, to <user>@<host>.
  * An address literal anywhere in the line, IPv4 and IPv6, validated by the
    ipaddress module and not by the pattern alone. A loopback and an unspecified
    address stay, because 127.0.0.1 and 0.0.0.0 name no machine and a support
    answer reads them. A four-part version such as 1.2.3.4 is a valid address
    and does redact; a three-part version, a v-prefixed one and a decimal never
    reach the check.
  * A name in a private-network domain anywhere in the line, which is .local for
    mDNS and the LAN suffixes beside it. Outside a url and a user@host form a
    public name stays, because a dotted word in log prose is far more often a
    file name, a module path or a version than a host. Two guards buy that
    precision and each one costs a case. A "(" after the name keeps a call such
    as threading.local() whole, so a name that a "(" follows stays. A name that
    a "/" leads never matches at all, which keeps ~/.local/share and
    /etc/hosts.local whole, and which also leaves a host inside a path whole,
    such as the one in a protocol-relative //ha.local/x. Two router defaults,
    .box and .home, are out of the suffix list for the same kind of reason: as
    suffixes they rewrite Gtk.Box and Path.home, and a traceback that names a
    type which does not exist is worse than one leaked router name.
  * This machine's own name, as a whole word. Unlike the username, which
    redacts in path and user@ context only, this redacts bare, because an
    internal machine name has no other spelling in a log. A name that is also
    ordinary log vocabulary, such as "deckard" or "desktop", redacts nothing:
    rewriting it would eat the app's own data paths and its prose, and such a
    name identifies nobody.

The allowlist keeps a host whole. It holds the github names the store fetches
from, the sites the app links to, the licence url every module header carries,
and the loopback names. Each is compiled into this app, so none of them is user
infrastructure, and a store failure stays diagnosable.

A secret assignment, and a dict, JSON or YAML field, for an unambiguous key
vocabulary of token, access_token, api_key, password, secret and the like,
which also covers an X- header prefix such as X-Api-Key or X-Auth-Token.
token=v, token = v, token: v, 'token': 'v' and "token": "v" all lose the
value. An unquoted colon value that carries an HTTP scheme word, such as
token: Token <secret> or api_key: Basic <cred>, keeps the scheme word and
loses the credential after it. An ambiguous name, which is key=, sig= or
auth=, redacts in url-query position alone, anchored to a ? or an &. "key" is
deck vocabulary here, so key=3 and {'key': 3} stay whole.

An Authorization header. That covers Authorization: Basic <b64>, where Basic
decodes straight to user:pass, Bearer <token> in any case, a quoted JSON
header dump, and a raw Authorization: <value> form.

Like log_hooks, this module imports stdlib and loguru only, and it imports
loguru inside install_log_redaction() alone. It imports nothing from src/ or
globals.py, so it stays importable before globals, which the fixtures.py
contract needs, and importable by log_hooks without weakening the import
contract of log_hooks.
"""
import contextlib
import getpass
import ipaddress
import os
import re
import socket
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from loguru import Record

# One redaction rule: a compiled pattern and what re.sub takes in its place.
_Rule = tuple["re.Pattern[str]", "str | Callable[[re.Match[str]], str]"]

_installed = False

_USER_TOKEN = "<user>"
_HOST_TOKEN = "<host>"
_IP_TOKEN = "<ip>"

# Keep compiled-in public service and licence hosts readable.
# Every subdomain of a listed host is also public.
_PUBLIC_HOSTS = (
    "github.com",
    "githubusercontent.com",
    "github.io",
    "core447.com",
    "ko-fi.com",
    "discord.com",
    "discord.gg",
    "gnu.org",
)

# The loopback names. The address spellings need no list, because the ipaddress
# module classifies them.
_LOOPBACK_NAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
})

# Redact bare private-domain names, testing longest suffixes first.
# Exclude box and home because they would corrupt Gtk.Box and Path.home text.
_INTERNAL_SUFFIXES = (
    "home.arpa",
    "localdomain",
    "internal",
    "intranet",
    "private",
    "local",
    "corp",
    "lan",
)

# Keep generic hostnames because redaction would corrupt application prose and paths.
_GENERIC_HOSTNAMES = frozenset({
    "arch", "archlinux", "computer", "debian", "deck", "deckard", "desktop",
    "fedora", "gentoo", "home", "hostname", "laptop", "linux", "local",
    "localdomain", "localhost", "media", "nixos", "opensuse", "plugin", "pc",
    "python", "root", "server", "store", "streamcontroller", "ubuntu", "user",
})

# A hostname must look like one before it becomes a pattern.
_HOSTNAME_SHAPE = re.compile(r"[a-z0-9][a-z0-9.-]*")

# Match complete-path delimiters but not dot, which prevents partial home matches.
_AFTER_PATH = r"[]\s/\"'`:;,()[{}<>|=&]"
# A username path segment may also carry a "." after it. A suffix form such
# as "/home/<user>.old" keeps the suffix and hides the name.
_AFTER_SEGMENT = r"[].\s/\"'`:;,()[{}<>|=&]"

# Exclude ambiguous key, sig, auth, and authorization from generic secret keys.
# Accept x- and x_ prefixes for token and API-key header families.
_SECRET_KEYS = (
    r"(?:x[_-])?(?:(?:access|refresh|id|auth)[_-]?token|token|api[_-]?key|apikey)|"
    r"client[_-]?secret|secret|"
    r"password|passwd|pwd|signature"
)


def _home_candidates() -> list[str]:
    """Return expanduser, HOME, and realpath home spellings longest first.
    This covers systems where /home resolves through another path."""
    homes: list[str] = []
    for candidate in (os.path.expanduser("~"), os.environ.get("HOME")):
        if not candidate:
            continue
        candidate = candidate.rstrip("/")
        # A "/" or an "" turns every absolute path into "~...". Refuse both.
        if len(candidate) < 2:
            continue
        for variant in (candidate, os.path.realpath(candidate)):
            if len(variant) >= 2 and variant not in homes:
                homes.append(variant)
    return sorted(homes, key=len, reverse=True)


def _username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def _host_token(host: str) -> str | None:
    """Return <ip> or <host>, or None for loopback, unspecified, and public hosts."""
    name = host.strip("[]").rstrip(".").lower()
    if not name:
        return None
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        if address.is_loopback or address.is_unspecified:
            return None
        return _IP_TOKEN
    if name in _LOOPBACK_NAMES or name.endswith(".localhost"):
        return None
    for public in _PUBLIC_HOSTS:
        if name == public or name.endswith("." + public):
            return None
    return _HOST_TOKEN


def _hostname_candidates() -> list[str]:
    """Return kernel and environment hostnames in full and short forms, longest first.
    Exclude names under three characters, generic vocabulary, invalid shapes, and allowlisted hosts."""
    raw: list[str] = []
    with contextlib.suppress(OSError):
        raw.append(socket.gethostname())
    environment_name = os.environ.get("HOSTNAME")
    if environment_name:
        raw.append(environment_name)

    names: list[str] = []
    for candidate in raw:
        cleaned = candidate.strip().rstrip(".").lower()
        for variant in (cleaned, cleaned.split(".")[0]):
            if len(variant) < 3 or variant in _GENERIC_HOSTNAMES:
                continue
            if not _HOSTNAME_SHAPE.fullmatch(variant):
                continue
            if _host_token(variant) is None:
                continue
            if variant not in names:
                names.append(variant)
    return sorted(names, key=len, reverse=True)


def _url_host_replacement(match: re.Match[str]) -> str:
    """Replace a URL host while preserving scheme, userinfo, port, and IPv6 brackets."""
    host = match.group(2)
    token = _host_token(host)
    if token is None:
        return match.group(0)
    if host.startswith("["):
        token = f"[{token}]"
    return cast(str, match.group(1) + token)


def _at_host_replacement(match: re.Match[str]) -> str:
    """Replace the host of a bare user@host form, and keep the "@"."""
    token = _host_token(match.group(1))
    return match.group(0) if token is None else "@" + token


def _name_replacement(match: re.Match[str]) -> str:
    """Replace a whole match that is a host name on its own."""
    token = _host_token(match.group(0))
    return match.group(0) if token is None else token


def _ip_replacement(match: re.Match[str]) -> str:
    """Replace only candidates that ipaddress confirms, preserving clocks and MAC addresses.
    Keep loopback, unspecified, invalid, and non-address text whole."""
    text = match.group(0)
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return text
    if address.is_loopback or address.is_unspecified:
        return text
    return _IP_TOKEN


def _colon_replacement(match: re.Match[str]) -> str:
    """Rebuild 'name': 'value' as 'name': '***', and keep the quoting the
    original used, from a dict repr, JSON, YAML or none."""
    value_quote = match.group(4) or ""
    return (
        f"{match.group(1)}{match.group(2)}{match.group(1)}"
        f"{match.group(3)}{value_quote}***{value_quote}"
    )


# _compile_rules builds this probe with the same hostnames as the rules.
# It matches nothing until then so no bare hostname can bypass redaction.
_FAST_PROBE: "re.Pattern[str]" = re.compile(r"(?!)")


def _compile_rules() -> "list[_Rule]":
    """Compile the rule list, in the order scrub() applies it, and refresh the
    fast-path probe alongside it."""
    rules: "list[_Rule]" = []

    # Url userinfo, which is scheme://user:pass@host and scheme://user@host.
    # The bounded quantifiers keep a wall of text without an "@" cheap.
    rules.append((re.compile(r"(?<=://)[^/\s:@]{1,128}:[^/\s@]{0,256}@"), "***@"))
    rules.append((re.compile(r"(?<=://)[^/\s:@]{1,128}@"), "***@"))

    # Split scheme and raw Authorization forms to preserve scheme words and idempotence.
    # One optional scheme group backtracks and changes a second scrub to "*** ***".
    _AUTH_HEADER = r"(?i)\b((?:proxy-)?authorization[\"']?[ \t]*[:=][ \t]*[\"']?"
    _AUTH_SCHEME = r"(?:basic|bearer|digest|token)"
    _AUTH_VALUE = r"[a-z0-9._~+/=-]{4,}"

    # With a scheme word the scheme stays and the credential after it goes.
    rules.append((
        re.compile(_AUTH_HEADER + _AUTH_SCHEME + r"[ \t]+)" + _AUTH_VALUE),
        r"\1***",
    ))
    # Exclude a bare scheme word, but redact credentials that start with one.
    rules.append((
        re.compile(
            _AUTH_HEADER + r")"
            + r"(?!" + _AUTH_SCHEME + r"(?![a-z0-9._~+/=-]))"
            + _AUTH_VALUE
        ),
        r"\1***",
    ))

    # A bearer token outside an Authorization header. "bearer" is no prose
    # vocabulary, and "basic" is.
    rules.append((re.compile(r"(?i)\b(bearer[ \t]+)[a-z0-9._~+/=-]{4,}"), r"\1***"))

    # secret=value. Whitespace may surround the "=", and the value may carry
    # quotes.
    rules.append((
        re.compile(
            r"(?i)(?<![\w-])(" + _SECRET_KEYS + r"|authorization)"
            r"[ \t]*=[ \t]*"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^&\s\"'<>)\]]+)"
        ),
        r"\1=***",
    ))

    # An ambiguous name, in url-query position only.
    rules.append((
        re.compile(r"(?i)([?&](?:key|sig|auth))=[^&\s\"'<>)\]]+"),
        r"\1=***",
    ))

    # Split unquoted scheme and schemeless colon values to avoid leaking either credential shape.
    # Quoted dict, JSON, and YAML values redact the scheme and secret together.
    _COLON_KEY = r"(?<![\w-])['\"]?(?:" + _SECRET_KEYS + r")['\"]?[ \t]*:[ \t]*"
    _COLON_VALUE = r"[^&\s,'\"()\[\]{}<>]+"

    # Group 1 preserves the key, colon, and scheme while replacing its credential.
    rules.append((
        re.compile(r"(?i)(" + _COLON_KEY + _AUTH_SCHEME + r"[ \t]+)" + _COLON_VALUE),
        r"\1***",
    ))

    # Exclude only scheme words followed by whitespace to keep a second scrub idempotent.
    # Values such as token-abc123 remain one schemeless credential and redact whole.
    rules.append((
        re.compile(
            r"(?i)(?<![\w-])(['\"]?)(" + _SECRET_KEYS + r")\1"
            r"([ \t]*:[ \t]*)"
            r"(?:(['\"])[^'\"\r\n]*\4|(?!" + _AUTH_SCHEME + r"[ \t])" + _COLON_VALUE + r")"
        ),
        _colon_replacement,
    ))

    # Replace complete home paths with ~; mid-path and longer-name matches fall through.
    # The username-segment rule still redacts their matching segment.
    for home in _home_candidates():
        rules.append((
            re.compile(
                r"(?<![\w.-])" + re.escape(home) + r"(?=" + _AFTER_PATH + r"|$)"
            ),
            "~",
        ))

    # Preserve URL scheme, userinfo, port, and path; run after userinfo redaction for idempotence.
    # Bound schemes at 32 characters to prevent quadratic scans of attacker-controlled log lines.
    rules.append((
        re.compile(
            r"(?i)(?<![\w+.-])([a-z][a-z0-9+.-]{0,31}://(?:[^/\s@]{1,256}@)?)"
            # One unbroken run of host characters, uncapped, so a run longer
            # than any real host redacts whole and never in part.
            r"(\[[0-9a-f:.]{2,45}\]|[a-z0-9._-]+)"
        ),
        _url_host_replacement,
    ))

    # Outside URLs, redact only dotted or bracketed hosts after a user part.
    # Keep single labels and decorators; reject longer labels but allow terminal dot.
    rules.append((
        re.compile(
            r"(?i)(?<=[\w.+-])@(\[[0-9a-f:.]{2,45}\]|[a-z0-9-]+(?:\.[a-z0-9-]+)+)"
            r"(?![\w-])(?!\.\w)"
        ),
        _at_host_replacement,
    ))

    # Redact private domains before own-host rules so full names become one token.
    # Guards preserve paths such as ~/.local/share and calls such as threading.local().
    rules.append((
        re.compile(
            r"(?i)(?<![\w./@-])"
            r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
            r"(?:" + "|".join(re.escape(suffix) for suffix in _INTERNAL_SUFFIXES) + r")"
            r"(?![\w(-])(?!\.\w)"
        ),
        _name_replacement,
    ))

    # This machine's own name, as a whole word. A following "." is allowed, so
    # a name under a public domain loses the machine and keeps the domain.
    hostnames = _hostname_candidates()
    for hostname in hostnames:
        rules.append((
            re.compile(r"(?i)(?<![\w.-])" + re.escape(hostname) + r"(?![\w-])"),
            _HOST_TOKEN,
        ))

    # Let ipaddress validate candidate literals so clocks and slices remain whole.
    # IPv4 guards reject fifth octets; IPv6 requires a leading hex group.
    rules.append((
        re.compile(r"(?<![\w.-])(?:\d{1,3}\.){3}\d{1,3}(?![\w-])(?!\.\d)"),
        _ip_replacement,
    ))
    rules.append((
        re.compile(r"(?<![\w:.])[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])"),
        _ip_replacement,
    ))

    # Redact username path segments, dot suffixes, and user parts before raw or scrubbed hosts.
    user = _username()
    if user:
        escaped = re.escape(user)
        rules.append((
            re.compile(r"(?<=/)" + escaped + r"(?=" + _AFTER_SEGMENT + r"|$)"),
            _USER_TOKEN,
        ))
        rules.append((
            re.compile(r"(?<![\w.-])" + escaped + r"(?=@[\w[<])"),
            _USER_TOKEN,
        ))

    # Probe dotted host/address shapes plus each dotless machine-name candidate.
    global _FAST_PROBE
    _FAST_PROBE = re.compile(
        "|".join([r"[a-z0-9]\.[a-z0-9]", *(re.escape(name) for name in hostnames)]),
        re.IGNORECASE,
    )

    return rules


_RULES = _compile_rules()


def scrub(text: str) -> str:
    """Return text with home paths, usernames, hosts, addresses and credentials
    redacted. Pure, thread-safe, and free of loguru."""
    if not text:
        return text
    # Skip rules unless delimiters, bearer text, or a bare address or host can match.
    if (
        "/" not in text and "@" not in text and "=" not in text
        and ":" not in text and not _FAST_PROBE.search(text)
        and "bearer" not in text.lower()
    ):
        return text
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def redact_record(record: "Record") -> None:
    """Scrub messages and fold a scrubbed stdlib traceback into exception records.
    Clear raw exceptions before formatting and never raise into logging call sites."""
    try:
        record["message"] = scrub(record["message"])
        exc = record.get("exception")
        if exc is not None:
            record["exception"] = None
            try:
                text = "".join(
                    traceback.format_exception(exc.type, exc.value, exc.traceback)
                )
            except Exception:
                name = getattr(exc.type, "__name__", None) or repr(exc.type)
                text = f"<traceback unavailable: formatting failed for {name}>"
            record["message"] = (
                record["message"].rstrip("\n") + "\n" + scrub(text).rstrip("\n")
            )
    except Exception:
        # Preserve an unusual record rather than lose it or crash the caller.
        pass


def install_log_redaction() -> None:
    """Idempotently configure redact_record as the core loguru patcher.
    This replaces any patcher, so future patchers must compose rather than stack installs."""
    global _installed
    if _installed:
        return
    from loguru import logger
    logger.configure(patcher=redact_record)
    _installed = True
