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
            // Unrelated players may quit between ListNames and this identity probe.
            if !root
                .get_property::<String>("Identity")
                .is_ok_and(|identity| identity == player)
            {
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

#[derive(Clone, Debug, Default)]
pub struct Player {
    pub bus: String,
    pub identity: String,
    pub status: String,
    pub title: String,
    pub artist: String,
    pub artwork: String,
    pub position: f64,
    pub duration: f64,
}
impl Player {
    pub fn matches(&self, name: &str) -> bool {
        name.is_empty()
            || name == self.bus
            || name == self.bus.trim_start_matches("org.mpris.MediaPlayer2.")
            || name == self.identity
    }
}
pub struct Monitor {
    connection: Connection,
}
impl Monitor {
    pub fn connect() -> Result<Self> {
        Ok(Self {
            connection: Builder::session()?
                .method_timeout(Duration::from_secs(2))
                .build()?,
        })
    }
    pub fn snapshot(&self) -> Result<Vec<Player>> {
        snapshot_on(&self.connection)
    }
}
pub fn snapshot() -> Result<Vec<Player>> {
    Monitor::connect()?.snapshot()
}
fn snapshot_on(connection: &Connection) -> Result<Vec<Player>> {
    use std::collections::HashMap;
    use zbus::zvariant::OwnedValue;
    let bus = Proxy::new(
        connection,
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
    )?;
    let names: Vec<String> = bus.call("ListNames", &())?;
    let mut players = Vec::new();
    for name in names
        .iter()
        .filter(|n| n.starts_with("org.mpris.MediaPlayer2."))
    {
        let read = (|| -> Result<Player> {
            let root = Proxy::new(
                connection,
                name.as_str(),
                "/org/mpris/MediaPlayer2",
                "org.mpris.MediaPlayer2",
            )?;
            let proxy = Proxy::new(
                connection,
                name.as_str(),
                "/org/mpris/MediaPlayer2",
                "org.freedesktop.DBus.Properties",
            )?;
            // One GetAll replaces repeated proxy creation, signal subscriptions and property reads.
            let properties: HashMap<String, OwnedValue> =
                proxy.call("GetAll", &("org.mpris.MediaPlayer2.Player",))?;
            let metadata = properties
                .get("Metadata")
                .and_then(|v| v.try_clone().ok())
                .and_then(|v| HashMap::<String, OwnedValue>::try_from(v).ok())
                .unwrap_or_default();
            let string = |key: &str| {
                metadata
                    .get(key)
                    .and_then(|v| <&str>::try_from(v).ok())
                    .unwrap_or("")
                    .to_owned()
            };
            let artist = metadata
                .get("xesam:artist")
                .and_then(|v| v.try_clone().ok())
                .and_then(|v| Vec::<String>::try_from(v).ok())
                .unwrap_or_default()
                .join(", ");
            let duration = metadata
                .get("mpris:length")
                .and_then(|v| i64::try_from(v).ok())
                .unwrap_or(0) as f64
                / 1_000_000.0;
            Ok(Player {
                bus: name.clone(),
                identity: root
                    .get_property("Identity")
                    .unwrap_or_else(|_| name.clone()),
                status: properties
                    .get("PlaybackStatus")
                    .and_then(|v| <&str>::try_from(v).ok())
                    .unwrap_or("")
                    .into(),
                title: string("xesam:title"),
                artist,
                artwork: string("mpris:artUrl"),
                position: properties
                    .get("Position")
                    .and_then(|v| i64::try_from(v).ok())
                    .unwrap_or(0) as f64
                    / 1_000_000.0,
                duration,
            })
        })();
        if let Ok(player) = read {
            players.push(player);
        }
    }
    players.sort_by_key(|p| (p.status != "Playing", p.bus.clone()));
    Ok(players)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    struct TestPlayer(Arc<Mutex<Vec<String>>>);
    struct Identity(&'static str);
    #[zbus::interface(name = "org.mpris.MediaPlayer2")]
    impl Identity {
        #[zbus(property)]
        fn identity(&self) -> &str {
            self.0
        }
    }
    #[zbus::interface(name = "org.mpris.MediaPlayer2.Player")]
    impl TestPlayer {
        fn play_pause(&self) {
            self.0.lock().unwrap().push("PlayPause".into());
        }
        fn next(&self) {
            self.0.lock().unwrap().push("Next".into());
        }
    }
    struct MetadataPlayer(Arc<Mutex<String>>);
    #[zbus::interface(name = "org.mpris.MediaPlayer2.Player")]
    impl MetadataPlayer {
        #[zbus(property)]
        fn playback_status(&self) -> &str {
            "Playing"
        }
        #[zbus(property)]
        fn position(&self) -> i64 {
            30_000_000
        }
        #[zbus(property)]
        fn metadata(&self) -> std::collections::HashMap<String, zbus::zvariant::OwnedValue> {
            use zbus::zvariant::{OwnedValue, Str, Value};
            std::collections::HashMap::from([
                (
                    "xesam:title".into(),
                    OwnedValue::from(Str::from(self.0.lock().unwrap().clone())),
                ),
                (
                    "xesam:artist".into(),
                    OwnedValue::try_from(Value::from(vec!["First Artist", "Second Artist"]))
                        .unwrap(),
                ),
                ("mpris:length".into(), OwnedValue::from(180_000_000_i64)),
                (
                    "mpris:artUrl".into(),
                    OwnedValue::from(Str::from("file:///tmp/album%20cover.png")),
                ),
            ])
        }
    }
    #[test]
    fn reads_live_metadata_and_external_track_changes() {
        if std::env::var_os("DBUS_SESSION_BUS_ADDRESS").is_none() {
            return;
        }
        let title = Arc::new(Mutex::new("First Track".to_owned()));
        let service = Builder::session()
            .unwrap()
            .name("org.mpris.MediaPlayer2.DeckardMetadata")
            .unwrap()
            .serve_at("/org/mpris/MediaPlayer2", MetadataPlayer(title.clone()))
            .unwrap()
            .serve_at(
                "/org/mpris/MediaPlayer2",
                Identity("Deckard Metadata Player"),
            )
            .unwrap()
            .build()
            .unwrap();
        let players = snapshot_on(&service).unwrap();
        let player = players
            .iter()
            .find(|p| p.bus.ends_with("DeckardMetadata"))
            .unwrap();
        assert_eq!(player.title, "First Track");
        assert_eq!(player.artist, "First Artist, Second Artist");
        assert_eq!(player.duration, 180.0);
        assert_eq!(player.position, 30.0);
        assert_eq!(player.artwork, "file:///tmp/album%20cover.png");
        assert_eq!(player.status, "Playing");
        *title.lock().unwrap() = "New Track".into();
        let players = snapshot_on(&service).unwrap();
        assert_eq!(
            players
                .iter()
                .find(|p| p.bus.ends_with("DeckardMetadata"))
                .unwrap()
                .title,
            "New Track"
        );
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
            .serve_at("/org/mpris/MediaPlayer2", TestPlayer(calls.clone()))
            .unwrap()
            .serve_at("/org/mpris/MediaPlayer2", Identity("Deckard Test Player"))
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
