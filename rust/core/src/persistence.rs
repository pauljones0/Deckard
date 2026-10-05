use std::fs::{self, File, Permissions};
use std::io::{self, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

/// Resolve existing symlinks, including ancestors of a not-yet-created file.
pub fn resolve(path: &Path) -> io::Result<PathBuf> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut resolved = PathBuf::from("/");
    let mut remaining: std::collections::VecDeque<_> = absolute
        .components()
        .map(|p| p.as_os_str().to_owned())
        .collect();
    let mut links = 0;
    while let Some(part) = remaining.pop_front() {
        if part == "/" || part == "." {
            continue;
        }
        if part == ".." {
            resolved.pop();
            continue;
        }
        let next = resolved.join(&part);
        match fs::symlink_metadata(&next) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                links += 1;
                if links > 40 {
                    return Err(io::Error::other("too many symbolic links"));
                }
                let target = fs::read_link(&next)?;
                if target.is_absolute() {
                    resolved = PathBuf::from("/");
                }
                for component in target.components().rev() {
                    remaining.push_front(component.as_os_str().to_owned());
                }
            }
            Ok(_) => resolved.push(part),
            Err(error) if error.kind() == io::ErrorKind::NotFound => resolved.push(part),
            Err(error) => return Err(error),
        }
    }
    Ok(resolved)
}

fn new_file_mode() -> io::Result<u32> {
    // Reading /proc avoids changing the process-wide mask in a multithreaded app.
    let status = fs::read_to_string("/proc/self/status")?;
    let mask = status
        .lines()
        .find_map(|line| line.strip_prefix("Umask:"))
        .ok_or_else(|| io::Error::other("cannot read process umask"))?;
    let mask = u32::from_str_radix(mask.trim(), 8).map_err(io::Error::other)?;
    Ok(0o666 & !mask)
}

/// Replace a file durably, preserving its symlink target and permissions.
pub fn atomic_write(path: &Path, content: &[u8]) -> io::Result<()> {
    atomic_write_after_sync(path, content, || {})
}

fn atomic_write_after_sync(
    path: &Path,
    content: &[u8],
    after_sync: impl FnOnce(),
) -> io::Result<()> {
    atomic_write_with_mode(path, content, after_sync, None)
}

/// Settings may contain copied legacy credentials; publish them with private permissions.
pub fn atomic_write_private(path: &Path, content: &[u8]) -> io::Result<()> {
    atomic_write_with_mode(path, content, || {}, Some(0o600))
}
fn atomic_write_with_mode(
    path: &Path,
    content: &[u8],
    after_sync: impl FnOnce(),
    private_mode: Option<u32>,
) -> io::Result<()> {
    let path = resolve(path)?;
    let directory = path
        .parent()
        .ok_or_else(|| io::Error::other("file has no parent"))?;
    fs::create_dir_all(directory)?;
    let prefix = format!(
        ".save-{}.",
        path.file_name()
            .ok_or_else(|| io::Error::other("file has no name"))?
            .to_string_lossy()
    );
    for entry in fs::read_dir(directory)?.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with(&prefix)
            && name.ends_with(".tmp")
            && entry
                .metadata()
                .and_then(|m| m.modified())
                .ok()
                .and_then(|time| SystemTime::now().duration_since(time).ok())
                .is_some_and(|age| age > Duration::from_secs(3600))
        {
            let _ = fs::remove_file(entry.path());
        }
    }
    let mode = if let Some(mode) = private_mode {
        mode
    } else {
        match fs::metadata(&path) {
            Ok(metadata) => metadata.permissions().mode() & 0o777,
            Err(error) if error.kind() == io::ErrorKind::NotFound => new_file_mode()?,
            Err(error) => return Err(error),
        }
    };
    let mut temporary = tempfile::Builder::new()
        .prefix(&prefix)
        .suffix(".tmp")
        .tempfile_in(directory)?;
    temporary.write_all(content)?;
    temporary
        .as_file()
        .set_permissions(Permissions::from_mode(mode))?;
    temporary.as_file().sync_all()?;
    after_sync();
    temporary.persist(&path).map_err(|error| error.error)?;
    File::open(directory)?.sync_all()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    #[test]
    fn write_keeps_symlinks_and_private_modes() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("private.json");
        fs::write(&target, b"old").unwrap();
        fs::set_permissions(&target, Permissions::from_mode(0o600)).unwrap();
        let link = root.path().join("link.json");
        symlink("private.json", &link).unwrap();
        atomic_write(&link, b"new").unwrap();
        assert!(
            fs::symlink_metadata(&link)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert_eq!(fs::read(&target).unwrap(), b"new");
        assert_eq!(
            fs::metadata(target).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }

    #[test]
    fn dangling_relative_link_and_missing_parents_resolve() {
        let root = tempfile::tempdir().unwrap();
        symlink("missing/../new/file", root.path().join("link")).unwrap();
        atomic_write(&root.path().join("link"), b"new").unwrap();
        assert_eq!(fs::read(root.path().join("new/file")).unwrap(), b"new");
    }

    #[test]
    fn rejects_symlink_cycles_without_hanging() {
        let root = tempfile::tempdir().unwrap();
        symlink("b", root.path().join("a")).unwrap();
        symlink("a", root.path().join("b")).unwrap();
        assert!(resolve(&root.path().join("a")).is_err());
    }

    #[test]
    fn interrupted_writer_preserves_the_previous_document() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("page.json");
        fs::write(&target, br#"{"generation":1}"#).unwrap();
        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "persistence::tests::writer_child", "--nocapture"])
            .env("DECKARD_TEST_WRITE_TARGET", &target)
            .status()
            .unwrap();
        assert_eq!(status.code(), Some(9));
        assert_eq!(fs::read(&target).unwrap(), br#"{"generation":1}"#);
        atomic_write(&target, br#"{"generation":3}"#).unwrap();
        assert_eq!(fs::read(&target).unwrap(), br#"{"generation":3}"#);
    }

    #[test]
    fn writer_child() {
        if let Some(path) = std::env::var_os("DECKARD_TEST_WRITE_TARGET") {
            atomic_write_after_sync(Path::new(&path), br#"{"generation":2}"#, || {
                std::process::exit(9)
            })
            .unwrap();
        }
    }
}
