"""The SD+ background tile spacing probe matches the wrapped handle.

The controller wraps its deck in BetterDeck before the spacing line runs, so
the probe must test the wrapped raw device, not the wrapper. A raw deck that
is a StreamDeckPlus gets the device-calibrated (116, 34); every other deck
keeps (36, 36). The first leg reassigns the fake deck's class to a
StreamDeckPlus subclass before construction, which is what the wrapper hands
back to the probe; no device I/O differs between the legs.
"""
import fixtures  # must be first; isolates DATA_PATH before import globals
import globals as gl

from StreamDeck.Devices.StreamDeckPlus import StreamDeckPlus

from faulty_fake_deck import FaultyFakeDeck


class FakePlus(FaultyFakeDeck, StreamDeckPlus):
    """A FaultyFakeDeck whose type also reads as a StreamDeckPlus.

    Never instantiated: instances get here by __class__ reassignment, so no
    StreamDeckPlus constructor or transport runs.
    """


def make_controller(serial: str, plus: bool):
    fixtures._install_integration_globals()
    fixtures.seed_page("Main")
    from src.backend.DeckManagement.DeckController import DeckController

    deck = FaultyFakeDeck(serial_number=serial, deck_type="Fake Deck",
                          key_layout=[2, 4])
    if plus:
        deck.__class__ = FakePlus
    controller = DeckController(gl.deck_manager, deck)
    gl.deck_manager.deck_controller.append(controller)
    return controller


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_sdplus_key_spacing")

    plus = make_controller("spacing-plus", plus=True)
    assert plus.key_spacing == (116, 34), (
        f"an SD+ raw deck must tile at the calibrated (116, 34), "
        f"got {plus.key_spacing}")
    fixtures.teardown(plus)

    plain = make_controller("spacing-plain", plus=False)
    assert plain.key_spacing == (36, 36), (
        f"a non-SD+ deck must keep (36, 36), got {plain.key_spacing}")
    fixtures.teardown(plain)

    print("PASS: scenario_sdplus_key_spacing")


if __name__ == "__main__":
    main()
