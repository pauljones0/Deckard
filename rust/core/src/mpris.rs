//! Native MPRIS transport. No playerctl or Python dependency.
use anyhow::{Result, ensure};
use std::time::Duration;
use zbus::blocking::{Connection, Proxy, connection::Builder};

pub fn control(method: &str, player: &str) -> Result<()> {
    ensure!(
        ["Play", "Pause", "PlayPause", "Stop", "Next", "Previous"].contains(&method),
        "unsupported media method"
    );
    let connection = Builder::session()?
        .method_timeout(Duration::from_secs(2))
        .build()?;
    control_on(&connection, method, player)
}

fn control_on(connection: &Connection, method: &str, player: &str) -> Result<()> {
    let bus = Proxy::new(
        connection,
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
    )?;
    let names: Vec<String> = bus.call("ListNames", &())?;
    let mut matched = 0;
    for name in names
        .iter()
        .filter(|n| n.starts_with("org.mpris.MediaPlayer2."))
    {
        if !player.is_empty()
            && player != name
            && player != name.trim_start_matches("org.mpris.MediaPlayer2.")
        {
            let root = Proxy::new(
                connection,
                name.as_str(),
                "/org/mpris/MediaPlayer2",
                "org.mpris.MediaPlayer2",
            )?;
            if root.get_property::<String>("Identity")? != player {
                continue;
            }
        }
        let proxy = Proxy::new(
            connection,
            name.as_str(),
            "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player",
        )?;
        proxy.call::<_, _, ()>(method, &())?;
        matched += 1;
    }
    ensure!(matched > 0, "no matching MPRIS media player is running");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    struct Player(Arc<Mutex<Vec<String>>>);
    struct Identity;
    #[zbus::interface(name = "org.mpris.MediaPlayer2")]
    impl Identity {
        #[zbus(property)]
        fn identity(&self) -> &str {
            "Deckard Test Player"
        }
    }
    #[zbus::interface(name = "org.mpris.MediaPlayer2.Player")]
    impl Player {
        fn play_pause(&self) {
            self.0.lock().unwrap().push("PlayPause".into());
        }
        fn next(&self) {
            self.0.lock().unwrap().push("Next".into());
        }
    }
    // Run with dbus-run-session; this exercises actual native D-Bus dispatch.
    #[test]
    fn controls_selected_player_on_session_bus() {
        if std::env::var_os("DBUS_SESSION_BUS_ADDRESS").is_none() {
            return;
        }
        let calls = Arc::new(Mutex::new(Vec::new()));
        let service = Builder::session()
            .unwrap()
            .name("org.mpris.MediaPlayer2.DeckardTest")
            .unwrap()
            .serve_at("/org/mpris/MediaPlayer2", Player(calls.clone()))
            .unwrap()
            .serve_at("/org/mpris/MediaPlayer2", Identity)
            .unwrap()
            .build()
            .unwrap();
        control_on(&service, "PlayPause", "DeckardTest").unwrap();
        control_on(&service, "Next", "org.mpris.MediaPlayer2.DeckardTest").unwrap();
        control_on(&service, "Next", "Deckard Test Player").unwrap();
        assert_eq!(*calls.lock().unwrap(), ["PlayPause", "Next", "Next"]);
        assert!(control_on(&service, "Next", "MissingTestPlayer").is_err());
    }
}
