#!/usr/bin/env python3
"""Run native action integration against a temporary null-sink PulseAudio server."""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=pathlib.Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="deckard-pulse-") as directory:
        root = pathlib.Path(directory)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        home = root / "home"
        home.mkdir(mode=0o700)
        socket = root / "pulse.sock"
        configuration = root / "pulse.pa"
        configuration.write_text(
            f"load-module module-null-sink sink_name=deckard_fixture\nload-module module-native-protocol-unix socket={socket} auth-anonymous=1\nset-default-sink deckard_fixture\n"
        )
        environment = os.environ.copy()
        environment.update(
            HOME=str(home),
            XDG_RUNTIME_DIR=str(runtime),
            XDG_CONFIG_HOME=str(home / ".config"),
            PULSE_SERVER=f"unix:{socket}",
        )
        log = (root / "server.log").open("w")
        server = subprocess.Popen(
            [
                "pulseaudio",
                "-n",
                "--daemonize=no",
                "--use-pid-file=no",
                "--exit-idle-time=-1",
                f"--file={configuration}",
            ],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        playback = None
        try:
            deadline = time.monotonic() + 10
            while not socket.exists():
                if server.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError((root / "server.log").read_text())
                time.sleep(0.05)
            playback = subprocess.Popen(
                [
                    "paplay",
                    "--raw",
                    "--rate=48000",
                    "--channels=2",
                    "--stream-name=DeckardFixture",
                    "/dev/zero",
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 10
            while True:
                streams = subprocess.check_output(
                    ["pactl", "--format=json", "list", "sink-inputs"],
                    env=environment,
                    text=True,
                )
                if json.loads(streams):
                    break
                if playback.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("fixture stream did not start")
                time.sleep(0.05)
            subprocess.run(
                [
                    sys.executable,
                    str(pathlib.Path(__file__).with_name("validate_common_actions.py")),
                    "--native",
                    str(args.native.resolve()),
                    "--pulse",
                    f"unix:{socket}",
                ],
                check=True,
            )
        finally:
            for process in [playback, server]:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            log.close()


if __name__ == "__main__":
    main()
