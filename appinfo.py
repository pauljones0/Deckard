"""Application identity strings in a side-effect-free standard-library module.
Do not import globals or src here because rebrand migration imports this module before globals."""

# Derive D-Bus, Ayatana, and dotted forms from APP_ID.
# An application-ID change then needs one edit.
APP_ID = "io.github.nazbert.Deckard"
APP_NAME = "Deckard"

# /io/github/nazbert/Deckard
DBUS_OBJECT_PATH = "/" + APP_ID.replace(".", "/")
# io_github_nazbert_Deckard  (ayatana NotificationItem path component)
DBUS_UNDERSCORE = APP_ID.replace(".", "_")

# Retain the old identity only for data migration and the running-instance guard.
# Do not reuse it elsewhere.
OLD_APP_ID = "com.core447.StreamController"
OLD_DBUS_OBJECT_PATH = "/" + OLD_APP_ID.replace(".", "/")
