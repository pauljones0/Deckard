"""Side-effect-free standard-library command-line parser.
It is safe to import before globals resolves and creates the data directory."""
# globals and rebrand migration share this parser.
# This keeps --data abbreviations and flag handling identical.
import argparse

argparser = argparse.ArgumentParser()
argparser.add_argument("-b", help="Open in background", action="store_true")
argparser.add_argument("--devel", help="Developer mode (disables auto update)", action="store_true")
argparser.add_argument("--skip-load-hardware-decks", help="Skips initilization/use of hardware decks", action="store_true")
# Keep model names as a literal because this module must remain importable before globals.
# argparse rejects unknown names at the flag.
FAKE_DECK_MODEL_NAMES = ("default", "original", "mk2", "mini", "xl", "plus", "neo", "pedal")
argparser.add_argument("--fake-deck-model", action="append", metavar="MODEL",
                       choices=FAKE_DECK_MODEL_NAMES,
                       help="Shape of a fake deck: default, original, mk2, mini, xl, plus, neo or pedal. "
                            "Repeat it once per fake deck; the last one given covers the rest. "
                            "The name default keeps the shape a fake deck already has, including a "
                            "key layout its settings hold")
argparser.add_argument("--close-running", help="Close running", action="store_true")
argparser.add_argument("--data", help="Data path", type=str)
argparser.add_argument("--change-page", action="append", nargs=2, help="Change the page for a device", metavar=("SERIAL_NUMBER", "PAGE_NAME"))
argparser.add_argument("--list-devices", help="List all connected StreamDeck devices and their properties", action="store_true")
argparser.add_argument("--list-pages", help="List all available pages", action="store_true")
argparser.add_argument("--change-state", action="append", nargs=4,
                      help="Change the state of a StreamDeck item. Format: SERIAL PAGE COORDS STATE\n"
                           "  SERIAL: Device serial number (e.g., CL123456789)\n"
                           "  PAGE: Page name (e.g., Main, Soundboard) \n"
                           "  COORDS: Position as x,y (e.g., 0,0 for top-left)\n"
                           "  STATE: State number to change to (0, 1, 2, etc.)\n"
                           "Example: --change-state CL123456789 Main 0,0 1",
                      metavar=("SERIAL", "PAGE", "COORDS", "STATE"))
argparser.add_argument("--emulate-input", action="append", nargs=4,
                      help="Press an input on a StreamDeck, as a finger would. Format: SERIAL PAGE COORDS EVENT\n"
                           "  SERIAL: Device serial number (e.g., CL123456789)\n"
                           "  PAGE: Page name (e.g., Main, Soundboard)\n"
                           "  COORDS: Position as x,y (e.g., 0,0 for top-left)\n"
                           "  EVENT: press or long-press\n"
                           "Example: --emulate-input CL123456789 Main 0,0 press\n"
                           "Deckard has to be running already: a press cannot wait for a deck to appear",
                      metavar=("SERIAL", "PAGE", "COORDS", "EVENT"))
# These read and page verbs require a running instance because they cannot wait for an open deck.
# Refuse them instead of parking them when no instance runs.
argparser.add_argument("--json", action="store_true",
                      help="Print the state of the running Deckard as one JSON object: "
                           "every deck with its serial, active page and brightness, and "
                           "the pages that exist")
argparser.add_argument("--get-brightness", metavar="SERIAL",
                      help="Print the brightness, 0 to 100, of the deck with this serial")
argparser.add_argument("--set-brightness", nargs=2, metavar=("SERIAL", "VALUE"),
                      help="Set the brightness of the deck with this serial. "
                           "VALUE is a whole number from 0 to 100")
argparser.add_argument("--sleep", metavar="SERIAL",
                      help="Put the deck with this serial to its screensaver. A press wakes it")
argparser.add_argument("--wake", metavar="SERIAL",
                      help="Wake the deck with this serial from its screensaver")
argparser.add_argument("--list-actions", nargs="+", metavar="PAGE",
                      help="List the actions on a page as JSON. Format: PAGE [COORDS]\n"
                           "  PAGE: Page name (e.g., Main, Soundboard)\n"
                           "  COORDS: Position as x,y to list one key alone (optional)")
argparser.add_argument("--rename-page", nargs=2, metavar=("OLD", "NEW"),
                      help="Rename a page. The deck showing it follows the new name")
argparser.add_argument("--duplicate-page", nargs=2, metavar=("SOURCE", "NEW"),
                      help="Copy a page to a new name")
argparser.add_argument("app_args", nargs="*")
