/// Check ZIP names before any extraction. Backslashes count as separators.
pub fn unsafe_member_reason(name: &str) -> Option<&'static str> {
    if name.starts_with(['/', '\\']) {
        return Some("the member names an absolute path");
    }
    if name.as_bytes().get(1) == Some(&b':') {
        return Some("the member names a drive-relative path");
    }
    if name.contains('\0') {
        return Some("the member contains a NUL byte");
    }
    let mut depth = 0;
    for component in name.split(['/', '\\']) {
        match component {
            "" | "." => {}
            ".." if depth == 0 => {
                return Some("the member resolves outside the directory it unpacks into");
            }
            ".." => depth -= 1,
            _ => depth += 1,
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn traversal_and_windows_paths_cannot_escape() {
        for bad in [
            "/etc/passwd",
            "../a",
            "a/../../b",
            "a\\..\\..\\b",
            "C:foo",
            "\\server\\file",
            "a\0b",
        ] {
            assert!(unsafe_member_reason(bad).is_some(), "{bad}");
        }
        for good in ["a/../b", "icons/a.png", "./a", "foo..bar"] {
            assert!(unsafe_member_reason(good).is_none(), "{good}");
        }
    }
}
