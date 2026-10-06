#!/usr/bin/env python3
"""Full-application CPU/PSS comparison; Python is benchmark tooling only.
Requires psutil, Pillow, a managed X11 display, git-exported upstream sources,
and the upstream runtime dependencies. Never measures failed application startup.
"""

import argparse, hashlib, json, os, pathlib, platform, signal, socket, statistics, subprocess, time
import psutil
from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHAS = {
    "original": "0f439967a16a8bfdc859439b0ae2b1370185aa06",
    "direct": "a3609c7de63347dbc7e031926826898950600c83",
}


def native_status(data):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(2)
        connection.connect(str(data / "native-control.sock"))
        connection.sendall(b'{"method":"status"}\n')
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
        response = json.loads(buffer)
        return response.get("result", response)


def assets(directory):
    directory.mkdir(parents=True, exist_ok=True)
    frames = []
    for index in range(100):
        frame = Image.new(
            "RGB", (96, 96), (index * 7 % 256, index * 11 % 256, index * 17 % 256)
        )
        draw = ImageDraw.Draw(frame)
        draw.rectangle((index % 72, 20, index % 72 + 24, 76), fill=(240, 240, 240))
        frames.append(frame)
    frames[0].save(
        directory / "animation.gif",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
        optimize=False,
    )
    return directory / "animation.gif"


def validate_native_animation(data, output):
    start = time.monotonic()
    previous = {}
    changes = {}
    dimensions = {}
    while time.monotonic() - start < 5:
        state = native_status(data)
        assert not state["errors"]
        for tile in state["devices"][0]["frame_tiles"]:
            key = str(tile["key"])
            dimensions[key] = [tile["width"], tile["height"]]
            if key in previous and previous[key] != tile["identity"]:
                changes[key] = changes.get(key, 0) + 1
            previous[key] = tile["identity"]
        time.sleep(0.02)
    elapsed = time.monotonic() - start
    fps = {key: count / elapsed for key, count in changes.items()}
    assert len(fps) == 8 and all(8.5 <= value <= 10.5 for value in fps.values()), fps
    assert all(size == [120, 120] for size in dimensions.values())
    output.write_text(
        json.dumps(
            {
                "elapsed": elapsed,
                "changes": changes,
                "fps": fps,
                "dimensions": dimensions,
            },
            indent=2,
        )
    )


def prepare(data, animation, workload):
    (data / "settings/decks").mkdir(parents=True, exist_ok=True)
    (data / "pages").mkdir(exist_ok=True)
    (data / ".skip-onboarding").touch()
    page = {"keys": {}, "dials": {}, "touchscreens": {}, "settings": {}}
    for y in range(2):
        for x in range(4):
            state = {
                "actions": [],
                "background": {"color": [30 + 40 * x, 30 + 60 * y, 90, 255]},
                "labels": {
                    "center": {
                        "text": f"Key {y * 4 + x + 1}",
                        "font-family": "DejaVu Sans",
                        "font-size": 14,
                        "color": [255, 255, 255, 255],
                    }
                },
            }
            if workload == "animated":
                state["media"] = {"path": str(animation), "fps": 10, "loop": True}
            page["keys"][f"{x}x{y}"] = {"states": {"0": state}}
    values = {
        "settings/settings.json": {
            "dev": {"n-fake-decks": 1, "fake-deck-types": ["Stream Deck +"]},
            "store": {"auto-update": False},
            "system": {"lock-on-lock-screen": False},
            "general": {"show-notifications": False},
        },
        "settings/pages.json": {
            "default-pages": {"fake-deck-1": str(data / "pages/Bench.json")}
        },
        "settings/decks/fake-deck-1.json": {
            "brightness": {"value": 75},
            "screensaver": {"enable": False},
        },
        "settings/native.json": {
            "devices": {"FAKE-PLUS-0": {"page": "Bench", "brightness": 75}},
            "auto_lock": False,
            "cache_mib": 64,
        },
        "pages/Bench.json": page,
    }
    for name, value in values.items():
        (data / name).write_text(json.dumps(value))


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def snapshot(root):
    processes = [root] + root.children(recursive=True)
    cpu = {}
    pss = 0
    rss = 0
    for process in processes:
        try:
            stamp = process.create_time()
            times = process.cpu_times()
            cpu[(process.pid, stamp)] = (
                times.user + times.system + times.children_user + times.children_system
            )
            memory = process.memory_full_info()
            pss += memory.pss
            rss += memory.rss
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
    return cpu, pss / 2**20, rss / 2**20, len(cpu)


def start_compositor(args, name):
    """Give every trial a fresh private compositor; exclude it from app timings."""
    if not args.weston_bundle:
        return None
    if not args.wayland_runtime:
        raise ValueError("--weston-bundle requires --wayland-runtime")
    bundle = args.weston_bundle.resolve()
    lib = bundle / "usr/lib"
    config = args.output / "weston.ini"
    config.write_text("[core]\nshell=kiosk-shell.so\nidle-time=0\n")
    args.wayland_runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = str(args.wayland_runtime.resolve())
    env["LD_LIBRARY_PATH"] = f"{lib}:{lib}/weston"
    env["WESTON_MODULE_MAP"] = ";".join(
        f"{module}={lib}/{directory}/{module}" for module, directory in
        [("headless-backend.so", "libweston-15"), ("gl-renderer.so", "libweston-15"), ("kiosk-shell.so", "weston")]
    )
    process = subprocess.Popen(
        [str(bundle / "usr/bin/weston"), "--backend=headless", "--renderer=gl",
         "--socket=" + args.wayland_socket, "--width=1280", "--height=800",
         "--config=" + str(config), "--log=" + str(args.output / f"{name}-weston.log")],
        env=env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    socket_path = args.wayland_runtime / args.wayland_socket
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("private Weston failed to start")
        if socket_path.exists():
            # Wait for initial EGL/output setup as well as the listening socket.
            time.sleep(0.5)
            return process
        time.sleep(0.05)
    stop(process)
    raise RuntimeError("private Weston did not create its socket")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=str(ROOT / ".venv/bin/python"))
    parser.add_argument(
        "--original", type=pathlib.Path, default=ROOT / "target/benchmarks/sources"
    )
    parser.add_argument(
        "--direct", type=pathlib.Path, default=ROOT / "target/benchmarks/direct"
    )
    parser.add_argument(
        "--native", type=pathlib.Path, default=ROOT / "target/release/deckard"
    )
    parser.add_argument(
        "--observe-legacy-frames",
        action="store_true",
        help="Validation-only instrumentation; never publish these timings",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="All applications launched with -b; deck rendering remains active",
    )
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument(
        "--apps",
        nargs="+",
        choices=["original", "direct", "rust"],
        default=["original", "direct", "rust"],
    )
    parser.add_argument(
        "--workloads",
        nargs="+",
        choices=["static", "animated"],
        default=["static", "animated"],
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--warmup", type=float, default=30)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument(
        "--wayland-runtime",
        type=pathlib.Path,
        help="Private compositor runtime directory; enables Wayland GPU tests",
    )
    parser.add_argument("--wayland-socket", default="deckard-benchmark")
    parser.add_argument("--weston-bundle", type=pathlib.Path, help="Extracted Weston package; start a fresh private GPU compositor for each trial")
    parser.add_argument("--display", default=":98")
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if len(str(args.output / "rust-animated-999/native-control.sock").encode()) >= 100:
        raise ValueError("benchmark output path is too long for native Unix control sockets")
    animation = assets(args.output / "assets")
    metadata = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "cpu": next(
            line.split(":", 1)[1].strip()
            for line in pathlib.Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "logical_cpus": os.cpu_count(),
        "python": subprocess.check_output(
            [args.python, "--version"], text=True
        ).strip(),
        "upstreams": SHAS,
        "native_renderer_override": os.environ.get("DECKARD_RENDERER"),
        "wgpu_backend_override": os.environ.get("WGPU_BACKEND"),
        "native_sha256": hashlib.sha256(args.native.read_bytes()).hexdigest(),
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "git_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            )
        ),
        "warmup_seconds": args.warmup,
        "sample_seconds": args.duration,
        "trials": args.trials,
        "units": {
            "cpu_percent": "100% = one logical CPU, sum of app and descendants; including waited child CPU; Linux process accounting at 10 ms resolution",
            "memory_mib": "PSS sums app and descendants, 1 MiB = 1048576 bytes",
        },
        "instrumented": args.observe_legacy_frames,
        "background": args.background,
        "fresh_compositor_per_trial": bool(args.weston_bundle),
        "wayland_runtime": str(args.wayland_runtime) if args.wayland_runtime else None,
        "rendering": (
            "Wayland, 1280x800 Weston headless EGL, hardware GPU"
            if args.wayland_runtime
            else "X11, 1280x800 Xvfb + Openbox, software OpenGL"
        )
        + ", one fake Plus, 8 labelled keys, no plugins, no hardware USB",
    }
    subprocess.run(
        [args.python, "-m", "pip", "freeze"],
        stdout=(args.output / "dependencies.txt").open("w"),
        stderr=subprocess.DEVNULL,
    ) if subprocess.run(
        [args.python, "-m", "pip", "--version"], capture_output=True
    ).returncode == 0 else subprocess.run(
        ["uv", "pip", "freeze", "--python", args.python],
        stdout=(args.output / "dependencies.txt").open("w"),
        check=True,
    )
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2))
    doctor = subprocess.run(
        [str(args.native), "--doctor"], capture_output=True, text=True, check=True
    )
    if json.loads(doctor.stdout).get("runtime") != "Rust":
        raise RuntimeError("native executable is not the Rust application")
    results = []
    for trial in range(args.trials):
        # Rotate order to avoid always measuring Rust last.
        order = (
            args.apps[trial % len(args.apps) :] + args.apps[: trial % len(args.apps)]
        )
        for workload in args.workloads:
            for app in order:
                name = f"{app}-{workload}-{trial + 1}"
                data = args.output / name
                prepare(data, animation, workload)
                home = data / "home"
                home.mkdir(exist_ok=True)
                config = args.output / "session.conf"
                config.write_text(
                    '<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen><policy context="default"><allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>'
                )
                bus = subprocess.Popen(
                    [
                        "dbus-daemon",
                        f"--config-file={config}",
                        "--nofork",
                        "--print-address=1",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=(args.output / f"{name}-bus.log").open("w"),
                    text=True,
                    start_new_session=True,
                )
                address = bus.stdout.readline().strip()
                env = os.environ.copy()
                env.pop("WAYLAND_DISPLAY", None)
                env.pop("GSK_RENDERER", None)
                env.update(
                    HOME=str(home),
                    XDG_CONFIG_HOME=str(home / ".config"),
                    XDG_DATA_HOME=str(home / ".local/share"),
                    XDG_CACHE_HOME=str(home / ".cache"),
                    DISPLAY=args.display,
                    GDK_BACKEND="x11",
                    XDG_SESSION_TYPE="x11",
                    XDG_CURRENT_DESKTOP="DeckardBenchmark",
                    LIBGL_ALWAYS_SOFTWARE="1",
                    DBUS_SESSION_BUS_ADDRESS=address,
                    NO_AT_BRIDGE="1",
                    GTK_A11Y="none",
                )
                if args.wayland_runtime:
                    env.pop("DISPLAY", None)
                    env.pop("LIBGL_ALWAYS_SOFTWARE", None)
                    env.update(
                        WAYLAND_DISPLAY=args.wayland_socket,
                        XDG_RUNTIME_DIR=str(args.wayland_runtime.resolve()),
                        GDK_BACKEND="wayland",
                        XDG_SESSION_TYPE="wayland",
                    )
                command = (
                    [
                        str(args.native),
                        "--skip-load-hardware-decks",
                        "--fake-deck-model",
                        "plus",
                        "--data",
                        str(data),
                    ]
                    if app == "rust"
                    else [
                        args.python,
                        str(getattr(args, app) / "main.py"),
                        "--skip-load-hardware-decks",
                        "--devel",
                        "--data",
                        str(data),
                    ]
                )
                if args.observe_legacy_frames and app != "rust":
                    command = [
                        args.python,
                        str(ROOT / "benchmarks/observe_legacy_frames.py"),
                        str(getattr(args, app)),
                        str(args.output / f"{name}-frames.json"),
                        *command[2:],
                    ]
                if args.background:
                    command.append("-b")
                if app == "direct":
                    command.extend(["--fake-deck-model", "plus"])
                print(f"Starting {name}", flush=True)
                compositor = start_compositor(args, name)
                log_path = args.output / f"{name}.log"
                process = subprocess.Popen(
                    command,
                    cwd=ROOT if app == "rust" else getattr(args, app),
                    env=env,
                    stdout=log_path.open("w"),
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    time.sleep(args.warmup)
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"{name} exited {process.returncode}: {log_path.read_text()[-3000:]}"
                        )
                    if app == "rust":
                        state = native_status(data)
                        if state.get("runtime") != "Rust":
                            raise RuntimeError("wrong runtime under test")
                        if (
                            not state.get("devices")
                            or state["devices"][0]["page"] != "Bench"
                            or state.get("errors")
                        ):
                            raise RuntimeError(f"bad native workload: {state}")
                        if args.observe_legacy_frames and workload == "animated":
                            validate_native_animation(
                                data, args.output / f"{name}-frames.json"
                            )
                    else:
                        log = log_path.read_text()
                        if (
                            "Loaded page Bench" not in log
                            or "Finished loading app" not in log
                        ):
                            raise RuntimeError(f"{name} has not loaded its page/GUI")
                        if "Error in media player tick" in log:
                            raise RuntimeError(f"{name} renderer failed")
                    root = psutil.Process(process.pid)
                    previous, _, _, _ = snapshot(root)
                    last = time.monotonic()
                    start = last
                    accumulated = 0
                    samples = []
                    while time.monotonic() - start < args.duration:
                        time.sleep(min(1, args.duration - (time.monotonic() - start)))
                        if process.poll() is not None:
                            raise RuntimeError(f"{name} died during sample")
                        current, pss, rss, count = snapshot(root)
                        now = time.monotonic()
                        delta = max(0, sum(current.values()) - sum(previous.values()))
                        accumulated += delta
                        samples.append(
                            {
                                "elapsed": now - start,
                                "cpu_percent": 100 * delta / (now - last),
                                "pss_mib": pss,
                                "rss_mib": rss,
                                "processes": count,
                            }
                        )
                        previous = current
                        last = now
                    result = {
                        "app": app,
                        "workload": workload,
                        "trial": trial + 1,
                        "cpu_percent": 100 * accumulated / (last - start),
                        "pss_mib": statistics.median(s["pss_mib"] for s in samples),
                        "rss_mib": statistics.median(s["rss_mib"] for s in samples),
                        "samples": samples,
                    }
                    results.append(result)
                    (args.output / "results.json").write_text(
                        json.dumps(results, indent=2)
                    )
                    print(
                        f"{name}: CPU {result['cpu_percent']:.3f}%, PSS {result['pss_mib']:.1f} MiB",
                        flush=True,
                    )
                finally:
                    if compositor is not None:
                        # End the owned compositor before client teardown: Weston 15 kiosk
                        # can crash in weston_view_move_to_layer on its final client closing.
                        stop(compositor)
                    stop(process)
                    stop(bus)
    for workload in args.workloads:
        for app in args.apps:
            rows = [r for r in results if r["workload"] == workload and r["app"] == app]
            print(
                workload,
                app,
                "median CPU",
                statistics.median(r["cpu_percent"] for r in rows),
                "median PSS",
                statistics.median(r["pss_mib"] for r in rows),
                flush=True,
            )


if __name__ == "__main__":
    main()
