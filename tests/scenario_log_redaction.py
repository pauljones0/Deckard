"""Require the core patcher to scrub messages and tracebacks before all sinks."""
import fixtures  # must be first; isolates DATA_PATH before any src import

import getpass
import os
import threading
import time

from loguru import logger

from src.backend import log_hooks
from src.backend import log_redaction
from src.backend.log_redaction import redact_record, scrub

UT = "<user>"  # the token scrub() substitutes for the username

# Capture the real account before overrides so end-to-end traceback paths redact.
REAL_HOME = os.path.expanduser("~")
REAL_USER = getpass.getuser()

# Compile unit checks against a fixed identity instead of the runner's account.
# This also avoids root-home and short-username boundary differences.
INJ_HOME = "/home/deckard_ci_user"
INJ_USER = "deckard_ci_user"
INJ_HOST = "deckard-ci-box"  # this machine's name, injected, never the runner's
HOME = INJ_HOME  # the unit checks assert on the injected identity
USER = INJ_USER
HOST = INJ_HOST

# The real machine-name reader, kept so the checks below can exercise it
# directly while every rule compile runs against the injected name.
_REAL_HOSTNAME_CANDIDATES = log_redaction._hostname_candidates


def _compile_rules_for(home: str, user: str, hostname: str = INJ_HOST) -> None:
    """Recompile scrub rules for a selected home, user, and injected hostname.
    Unit checks need fixed values; end-to-end checks need the real account paths."""
    os.environ["HOME"] = home
    os.environ["USER"] = user
    os.environ["LOGNAME"] = user
    log_redaction._hostname_candidates = lambda: [hostname]
    log_redaction._RULES = log_redaction._compile_rules()


def check_scrub_unit() -> None:
    # The home directory becomes a tilde, and the project-relative tail stays.
    assert scrub(f"{HOME}/dev/StreamController/src/app.py") == "~/dev/StreamController/src/app.py"
    assert scrub(f'File "{HOME}/.config/x.json", line 3') == 'File "~/.config/x.json", line 3'
    assert scrub(HOME) == "~", "bare home path (end of string) must redact"
    # Boundary guards. A longer username sharing the prefix must not be
    # clipped, and dot-suffix siblings must not collapse into the tilde form.
    assert scrub(HOME + "ette/f") == HOME + "ette/f", "prefix-sharing sibling user must survive"
    # The injected home has a real parent and a username basename.
    # A sibling directory must keep its suffix while hiding that username.
    assert os.path.basename(HOME) == USER
    parent = os.path.dirname(HOME)
    assert scrub(HOME + ".old/f") == f"{parent}/{UT}.old/f", (
        "sibling dir of home must keep its suffix, hide the username"
    )
    assert scrub(f"logs in {HOME}.") == f"logs in {parent}/{UT}.", (
        "sentence-final home path must still hide the username"
    )

    # The username matches in path segments and in user@host, never bare.
    assert scrub(f"/run/media/{USER}/stick") == f"/run/media/{UT}/stick"
    assert scrub(f"/var/home/{USER}") == f"/var/home/{UT}"
    assert scrub(f"ssh {USER}@build-host: refused") == f"ssh {UT}@build-host: refused"
    prose = f"the {USER}xyz option"
    assert scrub(prose) == prose, "username as a word prefix must not be touched"

    # URL credentials collapse to a mask. The host redacts under its own rule,
    # so what stays readable is the scheme, the path and the query.
    assert scrub("https://alice:hunter2@example.com/a/b") == "https://***@<host>/a/b"
    assert scrub("https://alice@example.com/a") == "https://***@<host>/a"
    assert "<host>/a/b" in scrub("https://alice:hunter2@example.com/a/b?x=1")

    # Unambiguous secret names match equals forms anywhere and tolerate spaces.
    # Bare ``key=`` matches only in queries because deck debug fields must survive.
    assert scrub("GET /repo?access_token=abc123&x=1") == "GET /repo?access_token=***&x=1"
    assert scrub("retry with token=tok-9") == "retry with token=***"
    assert scrub("retry with token = tok-9") == "retry with token=***", (
        "whitespace around '=' must not defeat redaction (round 1)"
    )
    assert scrub("https://h/p?key=sekrit&b=2") == "https://<host>/p?key=***&b=2"
    assert scrub("painting key=3 gen=7") == "painting key=3 gen=7"

    # Secret params in the colon form, from a dict repr or a JSON dump, such as
    # the HomeAssistant plugin logging its settings dict on error.
    assert scrub("{'access_token': 'eyJabc.def'}") == "{'access_token': '***'}"
    assert scrub('{"api_key": "sk-12345"}') == '{"api_key": "***"}'
    assert scrub("headers token: abc.def") == "headers token: ***"
    assert scrub("{'key': 3, 'gen': 7}") == "{'key': 3, 'gen': 7}", (
        "deck 'key' dict field must survive the colon rule"
    )

    # An unquoted colon value can contain a scheme word followed by a credential.
    # Keep the scheme word and remove the credential, including non-Authorization keys.
    assert scrub("token: Token abc123") == "token: Token ***", (
        "a scheme word in a colon value must keep the scheme and drop the secret"
    )
    assert "abc123" not in scrub("token: Token abc123"), (
        "the credential after the scheme word must be gone"
    )
    assert scrub("api_key: Basic dXNlcjpwYXNz") == "api_key: Basic ***", (
        "a basic|digest|token scheme word must not make the whole value leak"
    )
    assert "dXNlcjpwYXNz" not in scrub("api_key: Basic dXNlcjpwYXNz")

    # A scheme prefix followed by a non-space delimiter is one whole credential.
    # Preserve a scheme word only when whitespace separates it from the secret.
    assert scrub("token: token-abc123") == "token: ***", (
        "a value that starts with a scheme word plus a delimiter is a whole "
        "secret and must redact, not leak"
    )
    assert "token-abc123" not in scrub("token: token-abc123"), (
        "the whole secret must be gone, not just masked around the scheme word"
    )
    assert scrub("token: bearer.reset.jwt") == "token: ***", (
        "a dot after the scheme word does not make it a bare scheme word"
    )
    assert scrub("api_key: basic/creds99") == "api_key: ***", (
        "a slash after the scheme word does not make it a bare scheme word"
    )
    assert scrub("access_token: token-9-xyz") == "access_token: ***"

    # With no scheme word the value still redacts whole.
    assert scrub("token: plainsecret9") == "token: ***"
    assert scrub("api_key: plainsecret9") == "api_key: ***"
    # An X- header prefix on the token and api-key families redacts too, with
    # and without a scheme word.
    assert scrub("x-api-key: sk-plainsecret9") == "x-api-key: ***"
    assert "sk-123abc" not in scrub("x-api-key: Bearer sk-123abc"), (
        "an X- prefixed key must redact a scheme-word value too"
    )
    assert scrub("X-Auth-Token: Token deadbeef99") == "X-Auth-Token: Token ***"
    # authorization keeps its scheme word through the header rule, colon form
    # included, and its credential is gone.
    assert scrub("authorization: Token abc123") == "authorization: Token ***"
    assert "abc123" not in scrub("authorization: Token abc123")
    # The deck 'key' field must still survive next to a scheme-shaped value.
    assert scrub("{'key': 'basic'}") == "{'key': 'basic'}", (
        "deck 'key' field must survive even when its value looks like a scheme"
    )

    # Authorization headers. A Basic b64 value decodes straight to user and
    # pass, and BEARER in any case must not slip the fast path.
    assert scrub("Authorization: Basic dXNlcjpwYXNz") == "Authorization: Basic ***"
    assert scrub('"Authorization": "Bearer eyJhbGciOi"') == '"Authorization": "Bearer ***"'
    assert scrub("Authorization: Bearer eyJhbGciOi.payload") == "Authorization: Bearer ***"
    assert scrub("Proxy-Authorization: Digest sometokenvalue") == "Proxy-Authorization: Digest ***"
    assert scrub("Authorization: rawtokenvalue") == "Authorization: ***", (
        "the schemeless header form must still redact its raw value"
    )
    assert scrub("auth hdr BEARER SECRETTOKEN123") == "auth hdr BEARER ***", (
        "fast-path bearer probe must be case-folded (round 1)"
    )
    prose_basic = "covers the basic setup steps"
    assert scrub(prose_basic) == prose_basic, (
        "'basic' is prose vocabulary -- only redact it in header context"
    )

    # The no-scheme branch must not consume a bare scheme word.
    # A longer credential with that prefix must still redact.
    assert scrub("Authorization: basicauthvalue123") == "Authorization: ***"
    assert scrub("Authorization: tokenvalue99") == "Authorization: ***"
    assert scrub("Authorization: bearertoken.abc") == "Authorization: ***"


def check_host_unit() -> None:
    """Check invented documentation, private, and local host values only."""
    # A url host, in any scheme. The scheme, the port and the path stay, so a
    # broker failure still names the service.
    assert scrub("mqtt://ha.local:1883/topic") == "mqtt://<host>:1883/topic"
    assert scrub("http://192.168.1.50:8123/api/states") == "http://<ip>:8123/api/states"
    assert scrub("connecting to ws://homeassistant.lan/api/websocket") == (
        "connecting to ws://<host>/api/websocket"
    )
    assert scrub("http://[fd12:3456::9]:1883/x") == "http://[<ip>]:1883/x", (
        "a bracketed IPv6 url host must redact"
    )
    # A url credential and a url host redact together, in one pass.
    assert scrub("https://alice:hunter2@ha.local:8123/api") == "https://***@<host>:8123/api"

    # A bare address anywhere in the line, private and documentation ranges
    # alike. A sentence-final "." stays outside the address.
    assert scrub("connecting to 192.168.1.50") == "connecting to <ip>"
    assert scrub("broker 10.0.0.8:1883 refused") == "broker <ip>:1883 refused"
    assert scrub("route via 172.16.4.9") == "route via <ip>"
    assert scrub("ping 192.0.2.44.") == "ping <ip>.", (
        "a sentence-final address must redact and keep the full stop"
    )
    assert scrub("fd12:3456::9 unreachable") == "<ip> unreachable"
    assert scrub("neighbour fe80::1cad") == "neighbour <ip>"
    assert scrub("[fd12:3456::9]:1883 timeout") == "[<ip>]:1883 timeout"

    # An mDNS or LAN name in bare text, quoted or not. This is the leak that
    # motivates the rule: a plugin logs its settings dict on error.
    assert scrub("{'host': 'ha.local', 'port': 8123}") == "{'host': '<host>', 'port': 8123}", (
        "an mDNS host must redact and the port must stay"
    )
    assert scrub("mount nas.internal:445") == "mount <host>:445"
    assert scrub("broker mosquitto.lan reachable") == "broker <host> reachable"

    # Host rules run before username rules so both user@host parts redact in one pass.
    # A single-label host stays because the bare word can be prose.
    assert scrub(f"{USER}@ha.local") == f"{UT}@<host>", (
        "user@host must scrub whole, not leave the username behind the token"
    )
    assert scrub("admin@198.51.100.7") == "admin@<ip>"
    assert scrub(f"ssh {USER}@build-host: refused") == f"ssh {UT}@build-host: refused", (
        "a single-label host must stay"
    )
    decorator = "    @log.catch"
    assert scrub(decorator) == decorator, (
        "a decorator line has no user part before the '@' and must stay whole"
    )

    # The allowlist. These hosts are compiled into the app, so a store failure
    # stays diagnosable.
    for kept in (
        "https://raw.githubusercontent.com/StreamController/Store/main/x.json",
        "https://api.github.com/repos/user/repo/commits?sha=main",
        "https://github.com/nazbert/Deckard/issues",
        "https://streamcontroller.github.io/docs/latest/",
        "https://www.gnu.org/licenses/",
    ):
        assert scrub(kept) == kept, f"allowlisted host must stay: {kept}"

    # Loopback. 127.0.0.1 and 0.0.0.0 name no machine, and a support answer
    # reads them.
    for kept in (
        "http://localhost:8080/status",
        "http://127.0.0.1:5000/x",
        "http://[::1]:9000/ok",
        "backend bound 0.0.0.0 unguarded",
        "listening on ::1",
        "localhost.localdomain resolved",
    ):
        assert scrub(kept) == kept, f"loopback must stay: {kept}"

    # This machine's own name, as a whole word, in prose and in a path. A
    # longer word that merely starts with it stays.
    assert scrub(f"renaming deck on {HOST} now") == "renaming deck on <host> now"
    assert scrub(f"/srv/{HOST}/spool") == "/srv/<host>/spool"
    assert scrub(f"{HOST}.example.org reported") == "<host>.example.org reported", (
        "the machine name goes, the public domain beside it stays"
    )
    assert scrub(f"{HOST}.lan reported") == "<host> reported", (
        "an own name under a LAN suffix redacts as one host"
    )
    prose_host = f"the {HOST}y option"
    assert scrub(prose_host) == prose_host, "the machine name as a word prefix must not match"

    # False positives. A dotted or colon-separated word in log prose is far
    # more often a version, a file name, a module path or a clock than a host.
    for kept in (
        "app version 1.2.3",
        "plugin 0.7.2 loaded",
        "v1.2.3.4 build",
        "fps 29.9 avg 3.14",
        "2026-07-01 23:06:44 INFO",
        "mac aa:bb:cc:dd:ee:ff",
        "_thread_state = threading.local()",
        "~/.local/share/deckard/data/plugins",
        "logs.log rotated, settings.json written",
        "io.github.nazbert.Deckard action",
        "plugin com.core447.OSPlugin loaded",
        "cache hit for a.b.c.d.e",
        "reversed = frames[::-1]",
        "stride = frames[::2]",
        "File \"~/dev/Deckard/src/backend/log_redaction.py\", line 12 in scrub",
        # Tracebacks include source and repr text, so .box and .home are not host suffixes.
        # Treating them as suffixes would rewrite application types and attributes.
        "<Gtk.Box object at 0x7f0a1c2b3c00>",
        "class DeckStack(Gtk.Box):",
        "children: list[Gtk.Box] = []",
        "value = settings.home",
        "printer.fritz.box offline",
        "nas.home reachable",
    ):
        assert scrub(kept) == kept, f"must not redact: {kept}"

    # A bare four-part version is indistinguishable from an address and redacts.
    # Three-part versions and ``v``-prefixed four-part versions stay unchanged.
    assert scrub("plugin version 1.2.3.4") == "plugin version <ip>"


def check_scrub_bounded_cost() -> None:
    """Require scrub cost to scale linearly for a 32 KB adversarial line.
    The 0.2-second bound allows runner load but rejects repeated suffix rescans."""
    adversarial = "a." * 16384  # 32 KB of scheme characters, and no "://"
    start = time.perf_counter()
    scrubbed = scrub(adversarial)
    elapsed = time.perf_counter() - start
    assert scrubbed == adversarial, "the probe line holds no host and must not redact"
    assert elapsed < 0.2, (
        f"scrub() took {elapsed * 1000:.0f} ms on a 32 KB line: a rule that "
        "rescans the line from every position is back"
    )


def check_hostname_candidates() -> None:
    """The machine-name reader itself, driven through $HOSTNAME. The assertions
    name only the injected value, so the runner's own name cannot skew them."""
    previous = os.environ.get("HOSTNAME")
    try:
        os.environ["HOSTNAME"] = INJ_HOST
        assert INJ_HOST in _REAL_HOSTNAME_CANDIDATES(), "$HOSTNAME must reach the rules"

        os.environ["HOSTNAME"] = f"{INJ_HOST}.example.org"
        names = _REAL_HOSTNAME_CANDIDATES()
        assert f"{INJ_HOST}.example.org" in names and INJ_HOST in names, (
            "a fully qualified machine name must contribute both spellings"
        )
        assert names == sorted(names, key=len, reverse=True), (
            "longest first, so a full name matches before its short form"
        )

        # A name that is also ordinary log vocabulary builds no rule. One on
        # this list would otherwise rewrite the app's own data paths.
        for generic in ("deckard", "localhost", "media", "desktop"):
            os.environ["HOSTNAME"] = generic
            assert generic not in _REAL_HOSTNAME_CANDIDATES(), (
                f"a machine named {generic} must build no rule"
            )
        # A name the allowlist keeps builds no rule either, so a machine called
        # after a loopback name reads like any other loopback name in a log.
        for kept in ("localhost.localdomain", "ip6-localhost", "github.com"):
            os.environ["HOSTNAME"] = kept
            assert kept not in _REAL_HOSTNAME_CANDIDATES(), (
                f"an allowlisted machine name must build no rule: {kept}"
            )
        # Too short to be worth a rule.
        os.environ["HOSTNAME"] = "pi"
        assert "pi" not in _REAL_HOSTNAME_CANDIDATES()
    finally:
        if previous is None:
            os.environ.pop("HOSTNAME", None)
        else:
            os.environ["HOSTNAME"] = previous


def check_scrub_idempotent() -> None:
    """Require scrub to be idempotent across the full test corpus.
    A second pass must not consume an already-redacted authorization scheme."""
    corpus = [
        # Auth headers, every scheme, both delimiters, quoted and bare.
        "Authorization: Basic dXNlcjpwYXNz",
        "Authorization: Bearer eyJhbGciOi.payload",
        "authorization: Token abc123def",
        "Proxy-Authorization: Digest sometokenvalue",
        '{"Authorization": "Bearer eyJhbGciOi.abc"}',
        "Authorization: rawtokenvalue",
        "Authorization: basicauthvalue123",
        "Authorization: Digest username=x, nonce=abcdef",
        "Authorization: Basic dXNlcjpwYXNz\nProxy-Authorization: Bearer secretvalue",
        "auth hdr BEARER SECRETTOKEN123",
        # The rest of the corpus, for a real property test rather than an
        # auth-header-only one.
        f"{HOME}/dev/Deckard/src/app.py",
        f'File "{HOME}/.config/x.json", line 3',
        HOME,
        f"/run/media/{USER}/stick",
        f"ssh {USER}@build-host: refused",
        "https://alice:hunter2@example.com/a/b?x=1",
        "https://alice@example.com/a",
        "GET /repo?access_token=abc123&x=1",
        "retry with token = tok-9",
        "https://h/p?key=sekrit&b=2",
        "{'access_token': 'eyJabc.def'}",
        '{"api_key": "sk-12345"}',
        "headers token: abc.def",
        # Colon values carrying a scheme word, every scheme, X- prefix and not.
        "token: Token abc123",
        "api_key: Basic dXNlcjpwYXNz",
        "x-api-key: Bearer sk-123abc",
        "X-Auth-Token: Token deadbeef99",
        "authorization: Token abc123",
        "token: plainsecret9",
        "x-api-key: sk-plainsecret9",
        # Scheme-prefixed colon values with a delimiter are whole secrets.
        # They redact to one mask that must not grow on a second pass.
        "token: token-abc123",
        "token: bearer.reset.jwt",
        "api_key: basic/creds99",
        # Redaction tokens must not match host rules on a second pass.
        # A scrubbed user@host must not gain another token.
        "mqtt://ha.local:1883/topic",
        "http://192.168.1.50:8123/api/states",
        "http://[fd12:3456::9]:1883/x",
        "https://alice:hunter2@ha.local:8123/api",
        "connecting to 192.168.1.50",
        "ping 192.0.2.44.",
        "fd12:3456::9 unreachable",
        "[fd12:3456::9]:1883 timeout",
        "{'host': 'ha.local', 'port': 8123}",
        "mount nas.internal:445",
        f"{USER}@ha.local",
        "admin@198.51.100.7",
        f"renaming deck on {HOST} now",
        f"/srv/{HOST}/spool",
        f"{HOST}.example.org reported",
        f"{HOST}.lan reported",
        "https://raw.githubusercontent.com/StreamController/Store/main/x.json",
        "http://localhost:8080/status",
        "backend bound 0.0.0.0 unguarded",
        # Must-not-touch vocabulary. Idempotent trivially, but a rule that
        # starts eating these would show up here too.
        "painting key=3 gen=7",
        "{'key': 3, 'gen': 7}",
        "covers the basic setup steps",
        "plain message, nothing sensitive",
        "app version 1.2.3",
        "2026-07-01 23:06:44 INFO",
        "_thread_state = threading.local()",
        "~/.local/share/deckard/data/plugins",
        "",
    ]
    for text in corpus:
        once = scrub(text)
        assert scrub(once) == once, (
            f"scrub() is not idempotent for {text!r}: "
            f"pass 1 -> {once!r}, pass 2 -> {scrub(once)!r}"
        )
        # Equality covers the second pass; doubled-token checks cover the first pass.
        # One value must not produce adjacent redaction markers.
        for token in ("***", "<host>", "<ip>", UT):
            assert f"{token}{token}" not in once and f"{token} {token}" not in once, (
                f"pass 1 doubled the {token} marker for {text!r}: {once!r}"
            )

    # The fast path returns unchanged text untouched.
    assert scrub("plain message, nothing sensitive") == "plain message, nothing sensitive"
    assert scrub("") == ""


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_log_redaction")

    # The unit checks pin a known home and user, so recompile scrub()'s rules
    # against the injected identity before running them.
    _compile_rules_for(INJ_HOME, INJ_USER)
    check_scrub_unit()
    check_host_unit()
    check_scrub_bounded_cost()
    check_hostname_candidates()
    check_scrub_idempotent()

    # Exercise only the real boot entry point, which must install redaction.
    # Recompile for the real account so the process traceback paths redact.
    _compile_rules_for(REAL_HOME, REAL_USER)

    log_hooks.install_exception_hooks()
    assert logger._core.patcher is redact_record, (
        "install_exception_hooks() must install the redaction patcher -- "
        "main()'s boot path has no other install site"
    )
    log_hooks.install_exception_hooks()  # idempotent
    assert logger._core.patcher is redact_record

    # Both file and capture sinks must receive scrubbed text.
    # Enable backtrace and diagnose to expose the largest exception record.
    log_dir = os.path.join(fixtures.DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "logs.log")
    sink_id = logger.add(log_path, backtrace=True, diagnose=True, level="TRACE")
    records: list[str] = []
    logger.add(lambda m: records.append(str(m)), level="TRACE")

    # Plain messages through the normal path.
    logger.info(f"config at {REAL_HOME}/.config/streamcontroller/settings.json")
    logger.info(f"fetching https://{REAL_USER}:hunter2@git.example.com/repo.git?access_token=abc123&x=1")
    logger.info(f"mounted /run/media/{REAL_USER}/stick")
    logger.info("HA settings: {'host': 'ha.local', 'port': 8123, 'access_token': 'eyJlongtoken'}")
    logger.info("MQTT connect to 192.168.1.50:1883 failed")
    logger.info(f"deck registered on {INJ_HOST}")
    # Log a controlled home-rooted frame because CI can check out outside $HOME.
    # This line proves that frame paths redact to ~/ on every runner.
    logger.info(f'  File "{REAL_HOME}/plugins/demo/main.py", line 7 in fetch')

    # An uncaught thread exception through the real hook. The message, the
    # frame paths and a diagnose-visible local all carry PII.
    def boom() -> None:
        key_path = f"{REAL_HOME}/.ssh/id_rsa"  # a local that diagnose=True would leak
        raise ValueError(
            f"cannot open {key_path} "
            f"(remote=https://{REAL_USER}:sekrit@host.example/x?token=tok123)"
        )

    t = threading.Thread(target=boom, name="redaction-worker")
    t.start()
    t.join()

    logger.remove(sink_id)  # flush/close the file sink before reading
    with open(log_path) as f:
        content = f.read()
    joined = "".join(records)

    for output, label in ((content, "logs.log"), (joined, "capture sink")):
        # The raw values must be gone, traceback frame paths included, which is
        # why the exception is folded into the message.
        assert REAL_HOME not in output, f"{label}: raw home path leaked"
        assert "hunter2" not in output, f"{label}: URL password leaked"
        assert "sekrit" not in output, f"{label}: URL password (exception message) leaked"
        assert "access_token=abc123" not in output, f"{label}: token param leaked"
        assert "token=tok123" not in output, f"{label}: token param (exception message) leaked"
        assert "eyJlongtoken" not in output, f"{label}: dict-repr access_token leaked"
        assert f"/run/media/{REAL_USER}/" not in output, f"{label}: username path segment leaked"
        assert f"{REAL_USER}:hunter2" not in output and f"{REAL_USER}:sekrit" not in output, (
            f"{label}: URL userinfo leaked"
        )
        assert f"//{REAL_USER}@" not in output and f" {REAL_USER}@" not in output, (
            f"{label}: bare user@host leaked"
        )
        assert "ha.local" not in output, f"{label}: mDNS host leaked"
        assert "192.168.1.50" not in output, f"{label}: LAN address leaked"
        assert INJ_HOST not in output, f"{label}: machine name leaked"
        assert "git.example.com" not in output, f"{label}: url host leaked"

        # The redacted forms are present.
        assert "~/.config/streamcontroller/settings.json" in output, f"{label}: home must map to ~"
        assert "https://***@<host>/repo.git?access_token=***&x=1" in output, label
        assert "/run/media/<user>/stick" in output, label
        assert "https://***@<host>/x?token=***" in output, label
        assert "'access_token': '***'" in output, f"{label}: dict-repr token must redact"
        assert "'host': '<host>'" in output, f"{label}: an mDNS host must redact"
        assert "'port': 8123" in output, f"{label}: non-secret dict fields must survive"
        assert "MQTT connect to <ip>:1883 failed" in output, (
            f"{label}: a LAN address must redact and the port must stay"
        )
        assert "deck registered on <host>" in output, f"{label}: the machine name must redact"

        # Debuggability floor. The traceback is still a traceback.
        assert "Traceback (most recent call last):" in output, f"{label}: traceback text missing"
        assert 'File "~/plugins/demo/main.py"' in output, (
            f"{label}: a home-rooted frame path must redact to ~/ and stay readable"
        )
        assert "scenario_log_redaction.py" in output, f"{label}: frame file name must survive"
        assert "raise ValueError(" in output, f"{label}: source line must survive"
        assert "cannot open ~/.ssh/id_rsa" in output, f"{label}: message must stay readable"
        assert "Uncaught exception [thread]" in output and "redaction-worker" in output, (
            f"{label}: the hooks' kind/thread-name context must survive redaction"
        )

    print("PASS: scenario_log_redaction")


if __name__ == "__main__":
    main()
