#!/usr/bin/env python3
"""Matched screenshots of the real direct upstream and native GTK editor.

Developer tooling only. Requires the reference environment from benchmarks/README,
exported sources, an extracted Weston bundle, and a built native executable.
The common-plugin chooser additionally needs its cached sources and OBS dependency.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace

from PIL import Image, ImageChops
import compare

ROOT = Path(__file__).resolve().parents[1]
CASES = ["main", "no-devices", "settings-ui", "settings-performance", "settings-developer",
         "pages", "assets", "assets-icon-packs", "action", "dial", "touchscreen",
         "labels-detail", "deck-settings", "chooser-populated"]


@contextlib.contextmanager
def private_audio(image):
    """A null sink for the reference VolumeMixer, never the user's audio server."""
    with tempfile.TemporaryDirectory(prefix="deckard-ui-pulse-") as directory:
        root = Path(directory)
        home, runtime, socket = root / "home", root / "runtime", root / "pulse.sock"
        home.mkdir()
        runtime.mkdir(mode=0o700)
        configuration = root / "pulse.pa"
        configuration.write_text(
            f"load-module module-null-sink sink_name=deckard_fixture\n"
            f"load-module module-native-protocol-unix socket={socket} auth-anonymous=1\n"
            "set-default-sink deckard_fixture\n")
        name = f"deckard-ui-pulse-{os.getpid()}"
        subprocess.run([
            "docker", "run", "-d", "--rm", "--name", name, "--network", "none",
            "--user", f"{os.getuid()}:{os.getgid()}", "-e", f"HOME={home}",
            "-e", f"XDG_RUNTIME_DIR={runtime}", "-v", f"{root}:{root}", image,
            "pulseaudio", "-n", "--daemonize=no", "--use-pid-file=no",
            "--exit-idle-time=-1", f"--file={configuration}"], check=True,
            stdout=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while not socket.exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError(subprocess.check_output(["docker", "logs", name], text=True))
                time.sleep(0.1)
            yield f"unix:{socket}"
        finally:
            subprocess.run(["docker", "stop", "-t", "3", name], check=True,
                           stdout=subprocess.DEVNULL)


def fixture(directory, case, implementation, python):
    compare.prepare(directory, directory.parent / "unused.gif", "static")
    page_file = directory / "pages/Bench.json"
    page = json.loads(page_file.read_text())
    for key in page["keys"].values():
        key["states"]["0"]["labels"]["center"]["font-family"] = "Liberation Sans"
    if case in ("action", "chooser-populated"):
        names = ["OSPlugin"] if case == "action" else [
            "OSPlugin", "DeckPlugin", "MediaPlugin", "OBSPlugin", "VolumeMixer"]
        for name in names:
            shutil.copytree(ROOT / "target/benchmarks" / name,
                            directory / "plugins" / f"com_core447_{name}")
        if case == "chooser-populated":
            (directory / "plugins/com_core447_OBSPlugin/backend/.venv").symlink_to(
                python.parent.parent, target_is_directory=True)
            settings = directory / "settings/plugins/com_core447_OBSPlugin/settings.json"
            settings.parent.mkdir(parents=True)
            # The reference backend has no connection to a real OBS instance.
            settings.write_text(json.dumps({"connections": [{"id": "fixture", "name": "Fixture",
                "ip": "127.0.0.1", "port": 9, "password": ""}]}))
        else:
            state = page["keys"]["0x0"]["states"]["0"]
            state["image-control-action"] = 0
            state["actions"] = [{"id": "com_core447_OSPlugin::OpenInBrowser" if implementation == "direct"
                else "native::OSPlugin-OpenInBrowser", "event": "auto",
                "settings": {"url": "https://example.org"}, "comment": ""}]
    if case == "no-devices":
        for name in ("settings/settings.json", "settings/native.json"):
            path = directory / name
            values = json.loads(path.read_text())
            values.setdefault("dev", {})["n-fake-decks"] = 0
            path.write_text(json.dumps(values))
    page_file.write_text(json.dumps(page))


def capture(args, implementation, pulse):
    output = args.output / implementation
    output.mkdir()
    data = output / "data"
    fixture(data, args.case, implementation, args.python)
    compositor_args = SimpleNamespace(output=output, weston_bundle=args.weston,
        wayland_runtime=args.runtime, wayland_socket="deckard-ui", weston_shell="desktop",
        width=1400, height=900)
    compositor = bus = application = None
    try:
        compositor = compare.start_compositor(compositor_args, implementation)
        configuration = output / "bus.conf"
        configuration.write_text('<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen>'
            '<policy context="default"><allow own="*"/><allow send_destination="*"/>'
            '<allow receive_sender="*"/></policy></busconfig>')
        bus = subprocess.Popen(["dbus-daemon", f"--config-file={configuration}", "--nofork",
            "--print-address=1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True)
        home = output / "home"
        home.mkdir()
        environment = os.environ.copy()
        environment.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
            XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"),
            DBUS_SESSION_BUS_ADDRESS=bus.stdout.readline().strip(),
            XDG_RUNTIME_DIR=str(args.runtime), WAYLAND_DISPLAY="deckard-ui",
            GDK_BACKEND="wayland", XDG_SESSION_TYPE="wayland", GTK_A11Y="none",
            GSK_RENDERER="cairo", DECKARD_UI_CAPTURE=str(output / "editor.png"),
            DECKARD_UI_CAPTURE_CASE="" if args.case == "main" else args.case)
        environment.pop("DISPLAY", None)
        if pulse:
            environment["PULSE_SERVER"] = pulse
        flags = ["--skip-load-hardware-decks", "--data", str(data)]
        if args.case != "no-devices":
            flags.extend(["--fake-deck-model", "plus"])
        command = [str(args.native), *flags] if implementation == "rust" else [
            str(args.python), str(ROOT / "benchmarks/ui_reference.py"), str(args.direct),
            str(output / "editor.png"), *flags]
        with (output / "app.log").open("w") as log:
            application = subprocess.Popen(command, cwd=args.direct if implementation == "direct" else ROOT,
                env=environment, stdout=log, stderr=log, start_new_session=True)
            deadline = time.monotonic() + 40
            while not (output / "editor.png").exists():
                if application.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError((output / "app.log").read_text())
                time.sleep(0.1)
        if args.case == "chooser-populated" and implementation == "direct":
            rows = json.loads((output / "editor.json").read_text())
            plugins = [row["title"] for row in rows if row["type"].endswith("PluginExpander")]
            if set(plugins) != {"OS", "Deck", "Media", "OBS", "Volume Mixer"}:
                raise RuntimeError(f"Reference plugins failed to load: {plugins}; inspect app.log")
        print(implementation, output / "editor.png", flush=True)
    finally:
        for process in (application, bus, compositor):
            if process:
                compare.stop(process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=CASES, default="main")
    parser.add_argument("--native", type=Path, default=ROOT / "target/debug/deckard")
    parser.add_argument("--direct", type=Path, default=ROOT / "target/benchmarks/direct")
    parser.add_argument("--python", type=Path, default=ROOT / ".venv/bin/python")
    parser.add_argument("--weston", type=Path, default=ROOT / "target/benchmarks/weston")
    parser.add_argument("--pulse-image", default="deckard-linux-cachyos:x86_64")
    args = parser.parse_args()
    for field in ("output", "native", "direct", "weston"):
        setattr(args, field, getattr(args, field).resolve())
    # Preserve the virtual-environment path rather than resolving the interpreter symlink.
    args.python = args.python.absolute()
    args.output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="deckard-ui-wayland-") as runtime:
        args.runtime = Path(runtime)
        audio = private_audio(args.pulse_image) if args.case == "chooser-populated" else contextlib.nullcontext(None)
        with audio as pulse:
            for implementation in ("direct", "rust"):
                capture(args, implementation, pulse)
    a = Image.open(args.output / "direct/editor.png").convert("RGB")
    b = Image.open(args.output / "rust/editor.png").convert("RGB")
    if a.size != b.size:
        raise RuntimeError(f"Window dimensions differ: {a.size} / {b.size}")
    histogram = ImageChops.difference(a, b).histogram()
    mean = sum((index % 256) * count for index, count in enumerate(histogram)) / (a.width * a.height * 3)
    changed = sum(max(pixel) > 4 for pixel in ImageChops.difference(a, b).getdata())
    report = {"case": args.case, "direct_upstream": compare.SHAS["direct"],
        "native_sha256": hashlib.sha256(args.native.read_bytes()).hexdigest(),
        "size": a.size, "mean_channel_error": mean, "pixels_over_4_percent": 100 * changed / (a.width * a.height),
        "environment": "GTK Cairo, private Weston desktop shell, identical Plus/label fixtures",
        "note": "Device text rasterization, runtime IDs and data paths can differ; this is not a universal pixel-identity claim."}
    (args.output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
