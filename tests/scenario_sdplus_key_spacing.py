"""Check spacing against the raw device inside BetterDeck.

StreamDeckPlus uses (116, 34); other decks use (36, 36), with identical fake I/O.
"""
import fixtures  # must be first; isolates DATA_PATH before import globals
import globals as gl

from StreamDeck.Devices.StreamDeckPlus import StreamDeckPlus

from faulty_fake_deck import FaultyFakeDeck


class FakePlus(FaultyFakeDeck, StreamDeckPlus):
    """A FaultyFakeDeck that also has StreamDeckPlus type identity.

    __class__ reassignment avoids the StreamDeckPlus constructor and transport.
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
