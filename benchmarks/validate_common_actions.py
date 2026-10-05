#!/usr/bin/env python3
"""Isolated native migration/mixer integration check. Requires a PRIVATE Pulse server.
Never point --pulse at your desktop audio server. Python is test tooling only.
"""

import argparse, json, os, pathlib, socket, subprocess, tempfile, time


def rpc(data, method, **params):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(3)
        connection.connect(str(data / "native-control.sock"))
        connection.sendall(
            json.dumps({"method": method, "params": params}).encode() + b"\n"
        )
        buffer = b""
        while not buffer.endswith(b"\n"):
            buffer += connection.recv(65536)
        response = json.loads(buffer)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response.get("result", response)


def wait_for(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached within deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=pathlib.Path, required=True)
    parser.add_argument("--pulse", required=True)
    args = parser.parse_args()
    assert args.pulse.startswith("unix:/tmp/deckard-"), (
        "private fixture socket required"
    )
    environment = os.environ.copy()
    environment["PULSE_SERVER"] = args.pulse

    def pulse(*arguments):
        return subprocess.check_output(
            ["pactl", *arguments], env=environment, text=True
        )

    def stream():
        return json.loads(pulse("--format=json", "list", "sink-inputs"))[0]

    index = str(stream()["index"])
    pulse("set-sink-input-volume", index, "50%")
    pulse("set-sink-input-mute", index, "0")
    serial = "FAKE-PLUS-0"
    with tempfile.TemporaryDirectory(prefix="deckard-common-") as directory:
        data = pathlib.Path(directory)
        home = data / "home"
        home.mkdir()
        environment.update(
            HOME=str(home),
            XDG_CONFIG_HOME=str(home / ".config"),
            XDG_DATA_HOME=str(home / ".local/share"),
            XDG_CACHE_HOME=str(home / ".cache"),
        )
        (data / "pages").mkdir()
        (data / "sticky").mkdir()
        (data / "settings/plugins/com_core447_OBSPlugin").mkdir(parents=True)
        (data / "settings/native.json").write_text(
            json.dumps({"auto_lock": False, "devices": {serial: {"page": "Legacy"}}})
        )
        obs = data / "settings/plugins/com_core447_OBSPlugin/settings.json"
        obs.write_text(
            json.dumps(
                {
                    "connections": [
                        {
                            "id": "fixture",
                            "host": "localhost",
                            "port": 4455,
                            "password": "fixture-secret",
                        }
                    ]
                }
            )
        )

        def action(identifier, **settings):
            return {"id": identifier, "settings": settings}

        page = {"keys": {}, "dials": {}, "fixture": "preserved"}
        for key, actions in {
            "0x0": [action("com_core447_VolumeMixer::Open", increments=10)],
            "1x0": [action("com_core447_DeckPlugin::ChangeBrightness", brightness=30)],
            "2x0": [
                action(
                    "com_core447_OSPlugin::EasyCommand",
                    command='printf native > "$HOME/launched"',
                )
            ],
            "3x0": [
                action("com_core447_MediaPlugin::PlayPause", player_name="fixture")
            ],
            "0x1": [
                action(
                    "com_core447_OBSPlugin::SwitchScene",
                    connection_id="fixture",
                    scene="Scene",
                )
            ],
            "1x1": [action("unknown::retained", secret="original")],
        }.items():
            page["keys"][key] = {"states": {"0": {"actions": actions}}}
        source = data / "pages/Legacy.json"
        original = json.dumps(page, indent=3).encode()
        source.write_bytes(original)
        sticky = data / f"sticky/{serial}.json"
        sticky.write_text(
            json.dumps(
                {
                    "keys": {
                        "2x1": {
                            "states": {
                                "0": {
                                    "actions": [
                                        action(
                                            "com_core447_OSPlugin::Delay", delay=0.01
                                        )
                                    ]
                                }
                            }
                        }
                    }
                }
            )
        )
        sticky_original = sticky.read_bytes()
        log = (data / "application.log").open("w")
        process = subprocess.Popen(
            [
                str(args.native.resolve()),
                "--daemon-only",
                "--skip-load-hardware-decks",
                "--fake-deck-model",
                "plus",
                "--data",
                str(data),
            ],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_for(lambda: (data / "native-control.sock").exists())
            preview = rpc(data, "inspect-legacy-actions")
            assert (
                source.read_bytes() == original
                and sticky.read_bytes() == sticky_original
            )
            assert sum(len(row["converted"]) for row in preview["documents"]) == 6
            assert sum(len(row["remaining"]) for row in preview["documents"]) == 1
            report = rpc(data, "migrate-legacy-actions")
            assert {
                (data / row["backup"]).read_bytes() for row in report["documents"]
            } == {original, sticky_original}
            assert all(
                (data / row["backup"]).stat().st_mode & 0o777 == 0o600
                for row in report["documents"]
            )
            migrated = json.loads(source.read_text())
            assert migrated["fixture"] == "preserved"
            assert (
                migrated["keys"]["1x1"]["states"]["0"]["actions"][0]
                == page["keys"]["1x1"]["states"]["0"]["actions"][0]
            )
            native_settings = data / "settings/native.json"
            assert (
                json.loads(native_settings.read_text())["obs"]["connections"][
                    "fixture"
                ]["password"]
                == "fixture-secret"
            )
            assert native_settings.stat().st_mode & 0o777 == 0o600
            assert (
                sum(
                    len(row["converted"])
                    for row in rpc(data, "migrate-legacy-actions")["documents"]
                )
                == 0
            )

            def emit(input, family="keys", event="press"):
                rpc(
                    data,
                    "emulate-input",
                    serial=serial,
                    input=input,
                    family=family,
                    event=event,
                )
                if family == "keys":
                    rpc(
                        data,
                        "emulate-input",
                        serial=serial,
                        input=input,
                        family=family,
                        event="release",
                    )

            emit("1x0")
            wait_for(lambda: rpc(data, "status")["devices"][0]["brightness"] == 30)
            emit("2x0")
            wait_for(lambda: (home / "launched").exists())
            assert (home / "launched").read_text() == "native"
            emit("0x0")
            mixer_name = f"Native Mixer {serial}"
            wait_for(lambda: rpc(data, "status")["devices"][0]["page"] == mixer_name)
            emit("0", "dials", "turn-cw")
            wait_for(
                lambda: (
                    abs(
                        next(iter(stream()["volume"].values()))["value"] / 65536 * 100
                        - 60
                    )
                    < 0.02
                )
            )
            emit("0", "dials", "turn-cw")
            wait_for(
                lambda: (
                    abs(
                        next(iter(stream()["volume"].values()))["value"] / 65536 * 100
                        - 70
                    )
                    < 0.02
                )
            )
            emit("0", "dials", "press")
            wait_for(lambda: stream()["mute"])
            wait_for(
                lambda: (
                    "Muted"
                    in rpc(data, "get-page", page=mixer_name)["dials"]["0"]["states"][
                        "0"
                    ]["labels"]["center"]["text"]
                )
            )
            emit("0x0")
            wait_for(lambda: rpc(data, "status")["devices"][0]["page"] == "Legacy")
            assert not rpc(data, "status")["errors"]
            print(
                "PASS: five-plugin migration, exact private backups, sticky migration, OBS credentials, unknown actions, idempotence, detached command, brightness, mixer open/dial/mute/live labels/return"
            )
        finally:
            if process.poll() is None:
                try:
                    rpc(data, "quit")
                except Exception:
                    process.terminate()
            process.wait(timeout=10)
            log.close()
            if process.returncode:
                print((data / "application.log").read_text())


if __name__ == "__main__":
    main()
