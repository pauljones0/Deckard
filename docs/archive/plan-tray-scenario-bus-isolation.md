# Tray Scenario Bus Isolation Plan

## Status

Implementation complete. Delivery awaits commit approval.

## Problem

The tray re-registration scenario imports GTK before it starts `Gio.TestDBus`.
GTK initializes the shared Gio session-bus connection against the desktop bus.
Changing `DBUS_SESSION_BUS_ADDRESS` later does not replace that cached connection.
The tray then announces to the desktop watcher while the fake watcher listens on
the private test bus.

## Changes

1. Split the oversized scenario into a small orchestrator and focused helper
   modules. Keep each changed file below 300 lines.
2. Keep GTK out of module-level imports. Import it only after the private test
   bus is active.
3. Add a direct bus-daemon identity assertion before watcher registration.
4. Preserve the existing double-registration, icon, late-watcher, restarted-
   watcher, and teardown checks.
5. Run the focused scenario, full scenario suite, static gates, and module-size
   gate.

## Acceptance Criteria

- When the scenario starts on a desktop that already has a tray watcher, the
  scenario shall connect the tray and fake watcher only to its private bus.
- When a fake watcher appears after tray registration, the tray shall announce
  its item path exactly once.
- When the fake watcher restarts, the tray shall announce its item path exactly
  once to the new watcher.
- When the scenario compares D-Bus daemon identities, the tray connection and
  the explicit private connection shall report the same identity.
- When the repository gates run, the focused scenario, full suite, Ruff, ty,
  and module-size checks shall pass.
