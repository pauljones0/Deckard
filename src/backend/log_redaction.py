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
    file name, a module path or a version than a host. A "(" after the name
    keeps a call such as threading.local() whole, and a path such as
    ~/.local/share never matches, because a "/" before the dot leaves no label.
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
import getpass
import ipaddress
import os
import re
import socket
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loguru import Record

# One redaction rule: a compiled pattern and what re.sub takes in its place.
_Rule = tuple["re.Pattern[str]", "str | Callable[[re.Match[str]], str]"]

_installed = False

_USER_TOKEN = "<user>"
_HOST_TOKEN = "<host>"
_IP_TOKEN = "<ip>"

# The public hosts that stay readable. The store fetches from the github names,
# the app opens the link names, and every module header carries the licence url,
# so each of these is compiled into this app and none of them is user
# infrastructure. A subdomain of a listed name counts as listed, which covers
# api.github.com and raw.githubusercontent.com.
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

# The domain suffixes that only a private network uses. A bare name redacts on
# these alone. Longest first, so a multi-label suffix reads before the label it
# ends with.
_INTERNAL_SUFFIXES = (
    "home.arpa",
    "localdomain",
    "internal",
    "intranet",
    "private",
    "local",
    "corp",
    "home",
    "lan",
    "box",
)

# A hostname that is also ordinary log vocabulary redacts nothing. A machine
# named "deckard" would otherwise rewrite this app's own data paths, one named
# "media" would rewrite a mount path, and neither name identifies anybody.
_GENERIC_HOSTNAMES = frozenset({
    "arch", "archlinux", "computer", "debian", "deck", "deckard", "desktop",
    "fedora", "gentoo", "home", "hostname", "laptop", "linux", "local",
    "localdomain", "localhost", "media", "nixos", "opensuse", "plugin", "pc",
    "python", "root", "server", "store", "streamcontroller", "ubuntu", "user",
})

# A hostname must look like one before it becomes a pattern.
_HOSTNAME_SHAPE = re.compile(r"[a-z0-9][a-z0-9.-]*")

# The characters that follow a complete path in log text, which are a slash,
# whitespace, a quote, and the punctuation that ends a path in prose or in a
# repr. A "." stays out, so "/home/naz.old" does not half-match as home.
_AFTER_PATH = r"[]\s/\"'`:;,()[{}<>|=&]"
# A username path segment may also carry a "." after it. A suffix form such
# as "/home/<user>.old" keeps the suffix and hides the name.
_AFTER_SEGMENT = r"[].\s/\"'`:;,()[{}<>|=&]"

# The key names that name a secret wherever they appear. key, sig and auth
# stay out, because they are deck and debug vocabulary, and the url-query
# rule covers them. The header rule owns authorization, so a scheme word such
# as "Basic" survives rather than reads as part of a value.
#
# The token and api-key families take an optional "x-" or "x_" prefix,
# because an HTTP header commonly carries one, such as X-Api-Key,
# X-Auth-Token or X-Access-Token.
_SECRET_KEYS = (
    r"(?:x[_-])?(?:(?:access|refresh|id|auth)[_-]?token|token|api[_-]?key|apikey)|"
    r"client[_-]?secret|secret|"
    r"password|passwd|pwd|signature"
)


def _home_candidates() -> list[str]:
    """Every spelling of the home directory that a path can carry. That is
    expanduser, $HOME, and the realpath form of each, such as a /home
    symlinked to /var/home on an ostree system. Longest first, so a nested
    variant wins."""
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


def _hostname_candidates() -> list[str]:
    """Every spelling of this machine's own name, longest first. That is the
    name the kernel reports and the name the environment carries, each in its
    full and its short form, so a host called box.example.org redacts under
    either spelling. A name that reads as ordinary log vocabulary, or one under
    three characters, drops out, because a rule on it would eat prose."""
    raw: list[str] = []
    try:
        raw.append(socket.gethostname())
    except OSError:
        pass
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
            if variant not in names:
                names.append(variant)
    return sorted(names, key=len, reverse=True)


def _host_token(host: str) -> str | None:
    """The replacement for one host, or None when the host must stay whole.

    An address literal becomes "<ip>" and a name becomes "<host>". A loopback
    and an unspecified address stay, and so do the loopback names and the
    allowlisted public hosts."""
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


def _url_host_replacement(match: re.Match[str]) -> str:
    """Keep the scheme and any already scrubbed userinfo, replace the host. A
    bracketed IPv6 host keeps its brackets, so the url stays parseable and the
    port after it stays readable."""
    host = match.group(2)
    token = _host_token(host)
    if token is None:
        return match.group(0)
    if host.startswith("["):
        token = f"[{token}]"
    return match.group(1) + token


def _at_host_replacement(match: re.Match[str]) -> str:
    """Replace the host of a bare user@host form, and keep the "@"."""
    token = _host_token(match.group(1))
    return match.group(0) if token is None else "@" + token


def _name_replacement(match: re.Match[str]) -> str:
    """Replace a whole match that is a host name on its own."""
    token = _host_token(match.group(0))
    return match.group(0) if token is None else token


def _ip_replacement(match: re.Match[str]) -> str:
    """Replace a whole match that the ipaddress module confirms is an address.

    The patterns give a candidate shape and this decides, so a clock time such
    as 12:34:56 and a mac address such as aa:bb:cc:dd:ee:ff read as IPv6
    candidates and come back whole. A name must never reach here: a refused
    candidate is not a host, it is ordinary text."""
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


# The fast-path probe of scrub(). _compile_rules() builds it, because it must
# know the same hostnames the rules do: a recompile that left the probe behind
# would let a bare hostname skip every rule. It matches nothing until then.
_FAST_PROBE: "re.Pattern[str]" = re.compile(r"(?!)")


def _compile_rules() -> "list[_Rule]":
    """Compile the rule list, in the order scrub() applies it, and refresh the
    fast-path probe alongside it."""
    rules: "list[_Rule]" = []

    # Url userinfo, which is scheme://user:pass@host and scheme://user@host.
    # The bounded quantifiers keep a wall of text without an "@" cheap.
    rules.append((re.compile(r"(?<=://)[^/\s:@]{1,128}:[^/\s@]{0,256}@"), "***@"))
    rules.append((re.compile(r"(?<=://)[^/\s:@]{1,128}@"), "***@"))

    # Authorization headers, quoted or bare, with a scheme word or without.
    #   Authorization: Basic dXNlcjpwYXNz   -> Authorization: Basic ***
    #   "Authorization": "Bearer eyJ..."    -> "Authorization": "Bearer ***"
    #   Proxy-Authorization: rawtokenvalue  -> Proxy-Authorization: ***
    # These run before the generic rules, so the scheme word survives.
    # "Basic" alone is common prose, and only this header context matches it.
    #
    # Two rules, and not one rule with an optional scheme group. The raw-value
    # form needs the scheme optional, and an optional group backtracks. As one
    # pattern, a second scrub of "Authorization: Basic ***" fails to match the
    # value class against "***", backtracks past the scheme group, and takes
    # the word "Basic" as the value, which gives "Authorization: *** ***".
    # scrub() then is not idempotent, and a pipeline that scrubs twice, such
    # as the boot scrub over an already scrubbed file, or a line that passes
    # the loguru patcher and a later scrub, mangles its headers. Split in two,
    # the no-scheme rule carries a guard that the with-scheme rule must not
    # have.
    _AUTH_HEADER = r"(?i)\b((?:proxy-)?authorization[\"']?[ \t]*[:=][ \t]*[\"']?"
    _AUTH_SCHEME = r"(?:basic|bearer|digest|token)"
    _AUTH_VALUE = r"[a-z0-9._~+/=-]{4,}"

    # With a scheme word the scheme stays and the credential after it goes.
    rules.append((
        re.compile(_AUTH_HEADER + _AUTH_SCHEME + r"[ \t]+)" + _AUTH_VALUE),
        r"\1***",
    ))
    # Without a scheme word the value must not be a bare scheme word. Bare
    # means that no further value character follows, so a real credential that
    # starts with those letters, such as "tokenvalue" or "basicauth123", still
    # redacts.
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

    # secret: value, in a dict repr, a JSON dump or a YAML config dump, such
    # as {'access_token': 'eyJ...'}, {"api_key": "sk-..."} or token: abc, which
    # the HomeAssistant settings and headers dump produces.
    #
    # An unquoted value can carry an HTTP scheme word, such as
    # "token: Token abc123" or "api_key: Basic dXNlcjpwYXNz", from a header
    # dump that pairs a scheme word with a credential. That needs the same
    # mandatory-scheme/schemeless split the Authorization header rules use.
    # Two rules, and not one. With no split the schemeless rule stars the
    # scheme word and leaves the secret behind it. With the scheme word merely
    # added to its guard the schemeless rule fails outright and leaks the whole
    # value. A quoted value needs no split, because its quoted branch redacts
    # the scheme word and the secret together inside the quotes.
    _COLON_KEY = r"(?<![\w-])['\"]?(?:" + _SECRET_KEYS + r")['\"]?[ \t]*:[ \t]*"
    _COLON_VALUE = r"[^&\s,'\"()\[\]{}<>]+"

    # With a scheme word the scheme stays and the credential after it goes.
    # Group 1 spans the key, the colon and the scheme word, so "\1***" keeps
    # them and drops the credential.
    rules.append((
        re.compile(r"(?i)(" + _COLON_KEY + _AUTH_SCHEME + r"[ \t]+)" + _COLON_VALUE),
        r"\1***",
    ))

    # The schemeless colon form. The value-branch lookahead bails only when a
    # scheme word is followed by whitespace, which is the mandatory-scheme form
    # the rule above owns and the already scrubbed "token: Bearer ***" and
    # "token: Token ***" forms it produces, so a second pass keeps them out of
    # "token: *** ***". The guard tests the delimiter after the scheme word, not
    # a bare word boundary. A secret value that merely starts with a scheme word
    # and a non-space delimiter, such as "token: token-abc123", is one whole
    # credential, not a scheme word with a credential after it, so it must still
    # redact whole. A word-boundary guard here stopped redacting those and
    # leaked them.
    rules.append((
        re.compile(
            r"(?i)(?<![\w-])(['\"]?)(" + _SECRET_KEYS + r")\1"
            r"([ \t]*:[ \t]*)"
            r"(?:(['\"])[^'\"\r\n]*\4|(?!" + _AUTH_SCHEME + r"[ \t])" + _COLON_VALUE + r")"
        ),
        _colon_replacement,
    ))

    # The home directory becomes "~", with a guard on both sides. The
    # lookbehind stops a mid-path match, so "/var/home/naz" and
    # "/mnt/backup/home/naz" fall through to the username-segment rule. The
    # lookahead needs a real path terminator, so "/home/nazareth" and
    # "/home/naz.old" never clip to "~...", and the segment rule below hides
    # their username.
    for home in _home_candidates():
        rules.append((
            re.compile(
                r"(?<![\w.-])" + re.escape(home) + r"(?=" + _AFTER_PATH + r"|$)"
            ),
            "~",
        ))

    # A url host, in any scheme. The scheme, the port and the path stay, so a
    # store fetch and a broker connection both stay diagnosable. This runs
    # after the userinfo rules, so the optional group absorbs the "***@" they
    # leave behind, and it also takes a raw "user@" that no userinfo rule
    # reached. The host class carries no "<", so a second scrub finds no host
    # in the "<host>" this leaves.
    rules.append((
        re.compile(
            r"(?i)\b([a-z][a-z0-9+.-]*://(?:[^/\s@]{1,256}@)?)"
            # One unbroken run of host characters, uncapped, so a run longer
            # than any real host redacts whole and never in part.
            r"(\[[0-9a-f:.]{2,45}\]|[a-z0-9._-]+)"
        ),
        _url_host_replacement,
    ))

    # user@host outside a url. The host must carry a dot or be a bracketed
    # address, so a single-label host such as "build-host" stays: a bare word
    # after an "@" is as often prose as a machine. The lookbehind demands a
    # user part, which keeps a decorator line such as "@log.catch" whole. The
    # trailing guards refuse a longer label and accept a sentence-final ".".
    rules.append((
        re.compile(
            r"(?i)(?<=[\w.+-])@(\[[0-9a-f:.]{2,45}\]|[a-z0-9-]+(?:\.[a-z0-9-]+)+)"
            r"(?![\w-])(?!\.\w)"
        ),
        _at_host_replacement,
    ))

    # A name in a private-network domain, anywhere in the line. The lookbehind
    # keeps a path such as "~/.local/share" whole, because a "/" before the dot
    # leaves no label, and the "(" in the trailing guard keeps a call such as
    # "threading.local()" whole. This runs before the machine-name rule, so an
    # own hostname under such a suffix redacts as one host and never as a token
    # with a suffix left beside it.
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

    # An address literal anywhere in the line. The pattern gives a candidate
    # shape and the ipaddress module decides, so a clock time such as 12:34:56
    # reads as an IPv6 candidate and comes back whole. The IPv4 guards refuse a
    # fifth octet and accept a sentence-final "."; the mandatory leading hex
    # group of the IPv6 pattern keeps a slice such as "x[::2]" whole.
    rules.append((
        re.compile(r"(?<![\w.-])(?:\d{1,3}\.){3}\d{1,3}(?![\w-])(?!\.\d)"),
        _ip_replacement,
    ))
    rules.append((
        re.compile(r"(?<![\w:.])[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])"),
        _ip_replacement,
    ))

    # The username, as a full path segment, which includes a dot-suffix form,
    # or as the user part of user@host. The user@ lookahead accepts a "<" as
    # well as a host character, because the host rules run first and a scrubbed
    # host reads "<host>". Without it "user@ha.local" would keep its username.
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

    # The fast-path probe. A dot between two alphanumerics is the shape of every
    # host and address the rules above look for, and a machine name needs no dot
    # at all, so each one joins the probe by name.
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
    # A fast path. Every rule needs one of these characters, except three that
    # carry none of them: a bare bearer form, a bare address or host, and this
    # machine's own name. The probe scan and the case-folded check for those
    # run after every cheap character probe misses.
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
    """The loguru patcher. It scrubs the message. An exception can ride
    along, from opt(exception=...), from @log.catch or from the central
    exception hooks. It replaces that exception with a scrubbed traceback,
    formatted by the stdlib and folded into the message. It clears
    record["exception"] first, so no sink formats the raw frames even when the
    traceback formatting fails. It must never raise, because a patcher
    exception reaches every logging call site."""
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
        # Log the record unredacted rather than lose it or crash the caller.
        # scrub() on a str does not raise, and this guards an unusual record
        # shape.
        pass


def install_log_redaction() -> None:
    """Install redact_record as loguru's core patcher. Idempotent. It calls
    logger.configure(patcher=...), which replaces an earlier core patcher.
    Nothing else in this codebase sets one. If something sets one later,
    compose the two there rather than stack installs here.

    log_hooks.install_exception_hooks() calls this, which is how main()'s boot
    path gets redaction. A direct call stays safe and idempotent."""
    global _installed
    if _installed:
        return
    from loguru import logger
    logger.configure(patcher=redact_record)
    _installed = True
