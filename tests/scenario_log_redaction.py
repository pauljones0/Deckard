"""Scenario for src/backend/log_redaction.py.

A loguru core patcher scrubs every record, message and folded traceback,
before any sink formats it. install_exception_hooks() must install it.
"""
import fixtures  # must be first; isolates DATA_PATH before any src import

import getpass
import os
import threading

from loguru import logger

from src.backend import log_hooks
from src.backend import log_redaction
from src.backend.log_redaction import redact_record, scrub

UT = "<user>"  # the token scrub() substitutes for the username

# The real account, captured before any override. The end-to-end block below
# drives the process's own traceback, whose frame paths live under this home,
# so scrub() must know it to redact them.
REAL_HOME = os.path.expanduser("~")
REAL_USER = getpass.getuser()

# Injected identity for the unit checks. scrub() compiles its home and username
# rules from the environment, so the unit assertions pin a known home and user
# rather than read the runner's account. That keeps them deterministic under any
# runner, a freshly created CI user or root included, whose home (root's is
# /root, whose dirname is "/") and short username would otherwise skew the
# constructed paths and the expected strings.
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
    """Recompile scrub()'s module rules against a chosen identity.

    scrub() builds its home, username and machine-name patterns from the
    environment at import. The unit checks want a fixed identity so their
    expected strings do not depend on the runner's account, and the end-to-end
    block wants the real account so it redacts the process's own traceback frame
    paths. This swaps the compiled rules between the two.

    The machine name is injected in both, because the runner's own name could be
    any word and a rule on it would rewrite unrelated text in these checks.
    """
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
    # The injected home is a normal two-segment path, so basename is the user and
    # dirname is a real parent, never "/". A sibling dir of home keeps its suffix
    # and hides the username.
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

    # Secret params in the equals form. Unambiguous names match anywhere and
    # tolerate spaces. A bare key equals matches only when query-anchored,
    # because key is deck vocabulary and key=3 in a debug message must survive.
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

    # The colon form may carry an HTTP scheme word in an unquoted value, from a
    # header dump that pairs a scheme word with a credential. A non-Authorization
    # key must keep the scheme word and drop the credential after it, never star
    # the scheme word alone and leave the secret behind it.
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

    # A secret colon value that BEGINS with a scheme keyword followed by a
    # non-space delimiter is one whole credential, not a scheme word with a
    # credential after it. The mandatory-scheme form above keeps the scheme word
    # only when whitespace follows it, so these must redact whole and never leak.
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

    # The no-scheme branch must never consume a bare scheme word as the value.
    # A credential that merely starts with those letters is not a scheme word
    # and must still be redacted.
    assert scrub("Authorization: basicauthvalue123") == "Authorization: ***"
    assert scrub("Authorization: tokenvalue99") == "Authorization: ***"
    assert scrub("Authorization: bearertoken.abc") == "Authorization: ***"


def check_host_unit() -> None:
    """Hosts and addresses. Every value here is invented: a documentation range
    from RFC 5737, a private range, or a made-up name. None of them comes from
    the machine this runs on."""
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
    assert scrub("printer.fritz.box offline") == "<host> offline"
    assert scrub("broker mosquitto.lan reachable") == "broker <host> reachable"

    # user@host. The host rules run before the username rule, so both halves go
    # in one pass. A single-label host after an "@" stays: a bare word there is
    # as often prose as a machine.
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
    ):
        assert scrub(kept) == kept, f"must not redact: {kept}"

    # A deliberate trade-off, pinned so a change to it is a visible edit. A
    # four-part version is a valid address and the ipaddress module cannot tell
    # the two apart, so it redacts. A three-part version never reaches the
    # check, and a "v" prefix keeps a four-part one whole.
    assert scrub("plugin version 1.2.3.4") == "plugin version <ip>"


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
        # Too short to be worth a rule.
        os.environ["HOSTNAME"] = "pi"
        assert "pi" not in _REAL_HOSTNAME_CANDIDATES()
    finally:
        if previous is None:
            os.environ.pop("HOSTNAME", None)
        else:
            os.environ["HOSTNAME"] = previous


def check_scrub_idempotent() -> None:
    """scrub(scrub(x)) must equal scrub(x) over the whole corpus.

    The auth-header rule can backtrack out of its optional scheme group and
    consume the scheme word itself on a second pass, so any pipeline that
    scrubs twice would mangle every header it had already redacted.
    """
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
        # Colon values that BEGIN with a scheme word plus a delimiter. These are
        # whole secrets, so they redact to a single mask, and a re-scrub of that
        # mask must not grow it.
        "token: token-abc123",
        "token: bearer.reset.jwt",
        "api_key: basic/creds99",
        # Hosts and addresses. A token such as "<host>" must not read as a host
        # on the second pass, and a scrubbed user@host must not grow a second
        # token.
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
        # Already-redacted markers must survive a re-scrub verbatim, so the
        # count can only be what pass 1 produced and never grow.
        for token in ("***", "<host>", "<ip>", UT):
            assert once.count(token) == scrub(once).count(token), (
                f"re-scrub changed the {token} count for {text!r}"
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
    check_hostname_candidates()
    check_scrub_idempotent()

    # The real boot wiring, and nothing else. main() only ever calls
    # install_exception_hooks(), and redaction must ride along. This does not
    # call install_log_redaction(), so reverting the piggyback inside
    # install_exception_hooks() turns this red.
    #
    # The traceback below is the process's own, so its frame paths live under
    # the real account home. Recompile scrub()'s rules against that account so
    # the folded traceback redacts, then assert on the real identity here.
    _compile_rules_for(REAL_HOME, REAL_USER)

    log_hooks.install_exception_hooks()
    assert logger._core.patcher is redact_record, (
        "install_exception_hooks() must install the redaction patcher -- "
        "main()'s boot path has no other install site"
    )
    log_hooks.install_exception_hooks()  # idempotent
    assert logger._core.patcher is redact_record

    # A real file sink plus a capture sink. Both must receive scrubbed text.
    # backtrace and diagnose are on here, because they are the loudest possible
    # exception expansion, so a patcher that failed to clear the record would
    # leak the most here.
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
    # A traceback-frame line under home, logged directly, so the home->~ frame
    # redaction is exercised wherever the checkout lives. The process's own
    # traceback frames only carry ~ when the checkout is under $HOME; CI checks
    # out under /builds, so this controlled line, not the boom() frames, is
    # what proves a home-rooted frame path redacts to ~/.
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
