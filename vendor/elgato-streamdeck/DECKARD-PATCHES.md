# Deckard driver patch

Based on the published elgato-streamdeck 0.13.2 crate. Original license and source attribution are retained.

Neo touchpoint color feature reports are padded to 32 bytes, matching the original upstream streamcontroller-streamdeck 0.2.1 Neo implementation (`[0x03, 0x06, key, red, green, blue]` followed by 26 zero bytes). All HID writes remain owned by the native device writer. Native rendering separately uses the upstream Plus XL key dimensions and strip orientation.

Original StreamController SDK 0.2.1 protocols are adapted natively for Studio, Mirabox 293S and Ulanzi D200, including Ulanzi interface-0 filtering, priming, keep-alive, combined multi-key uploads, unique image filenames, phantom-slot suppression and ZIP framing-byte avoidance. Missing USB serials receive a path-derived identifier. Mirabox release-only firmware reports become paired synthetic press/release events. No Python code is shipped or invoked. Physical testing remains necessary.
