//! Independent wire fixtures for additional upstream device protocols.
use elgato_streamdeck::{compat, info::Kind};
use std::io::{Cursor, Read};

#[test]
fn device_ids_and_key_dimensions_match_original_sdk() {
    for (vid, pid, kind, keys, size) in [
        (0x0fd9, 0x00aa, Kind::Studio, 32, (80, 120)),
        (0x5548, 0x6670, Kind::Mirabox293s, 18, (85, 85)),
        (0x2207, 0x0019, Kind::UlanziD200, 15, (196, 196)),
    ] {
        assert_eq!(Kind::from_vid_pid(vid, pid), Some(kind));
        assert_eq!(kind.key_count(), keys);
        assert_eq!(kind.key_image_format().size, size);
        assert_eq!(Kind::from_vid_pid(vid, pid + 1), None);
    }
}

#[test]
fn ulanzi_wire_header_continuations_and_phantom_input_match_fixtures() {
    let data: Vec<_> = (0..2500).map(|i| (i % 251) as u8).collect();
    let packets = compat::ulanzi_packets(10, &data);
    assert_eq!(&packets[0][..8], &[0x7c, 0x7c, 0, 10, 0xc4, 9, 0, 0]);
    assert_eq!(packets.len(), 3);
    assert!(packets.iter().all(|p| p.len() == 1024));
    assert_eq!(&packets[0][8..], &data[..1016]);
    assert_eq!(&packets[1][..], &data[1016..2040]);
    assert_eq!(&packets[2][..460], &data[2040..]);
    assert!(packets[2][460..].iter().all(|b| *b == 0));
    let mut report = [0x7c, 0x7c, 1, 1, 0, 0, 0, 0, 0, 13, 0, 1];
    assert_eq!(compat::ulanzi_key(&report), Some((13, true)));
    report[11] = 0;
    assert_eq!(compat::ulanzi_key(&report), Some((13, false)));
    report[9] = 14;
    assert_eq!(compat::ulanzi_key(&report), None);
    assert_eq!(compat::ulanzi_key(&report[..9]), None);
}

#[test]
fn ulanzi_combined_zip_preserves_every_image_and_safe_framing() {
    // Incompressible deterministic bytes span multiple continuation boundaries.
    let mut seed = 0x12345678u32;
    let image: Vec<_> = (0..14000)
        .map(|_| {
            seed ^= seed << 13;
            seed ^= seed >> 17;
            seed ^= seed << 5;
            seed as u8
        })
        .collect();
    let zip = compat::ulanzi_bundle(&[(0, 101, &image), (13, 102, &image)]).unwrap();
    assert!(
        (1016..zip.len())
            .step_by(1024)
            .all(|i| ![0, 0x7c].contains(&zip[i]))
    );
    let mut archive = zip::ZipArchive::new(Cursor::new(zip)).unwrap();
    let manifest: serde_json::Value =
        serde_json::from_reader(archive.by_name("manifest.json").unwrap()).unwrap();
    assert_eq!(manifest["0_0"]["ViewParam"][0]["Icon"], "icons/0_101.png");
    assert_eq!(manifest["3_2"]["ViewParam"][0]["Icon"], "icons/13_102.png");
    for name in ["icons/0_101.png", "icons/13_102.png"] {
        let mut actual = Vec::new();
        archive
            .by_name(name)
            .unwrap()
            .read_to_end(&mut actual)
            .unwrap();
        assert_eq!(actual, image);
    }
}

#[test]
fn mirabox_release_reports_and_output_framing_match_fixtures() {
    let mut ack = *b"ACK\0\0OK\0\0\x0d";
    assert_eq!(compat::mirabox_key(&ack), Some(0));
    ack[9] = 18;
    assert_eq!(compat::mirabox_key(&ack), Some(17));
    ack[9] = 0;
    assert_eq!(compat::mirabox_key(&ack), None);
    let command = b"CRT\0\0CONNECT";
    let report = compat::mirabox_report(command);
    assert_eq!(report.len(), 514);
    assert_eq!(report[0], 0);
    assert_eq!(&report[1..1 + command.len()], command);
    assert!(report[1 + command.len()..].iter().all(|b| *b == 0));
}
