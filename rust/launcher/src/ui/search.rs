use std::cmp::Ordering;
pub(super) fn fuzzy_ratio(a: &str, b: &str) -> f64 {
    let a = a.chars().collect::<Vec<_>>();
    let b = b.chars().collect::<Vec<_>>();
    if a.is_empty() && b.is_empty() {
        return 100.;
    }
    let mut previous = vec![0usize; b.len() + 1];
    for x in &a {
        let mut diagonal = 0;
        for (j, y) in b.iter().enumerate() {
            let old = previous[j + 1];
            previous[j + 1] = if x == y {
                diagonal + 1
            } else {
                previous[j + 1].max(previous[j])
            };
            diagonal = old;
        }
    }
    200. * previous[b.len()] as f64 / (a.len() + b.len()) as f64
}
pub(super) fn natural_cmp(a: &str, b: &str) -> Ordering {
    let mut a = a.chars().peekable();
    let mut b = b.chars().peekable();
    loop {
        match (a.peek(), b.peek()) {
            (None, None) => return Ordering::Equal,
            (None, _) => return Ordering::Less,
            (_, None) => return Ordering::Greater,
            (Some(x), Some(y)) if x.is_ascii_digit() && y.is_ascii_digit() => {
                let mut x = String::new();
                let mut y = String::new();
                while a.peek().is_some_and(char::is_ascii_digit) {
                    x.push(a.next().unwrap());
                }
                while b.peek().is_some_and(char::is_ascii_digit) {
                    y.push(b.next().unwrap());
                }
                let x = x.trim_start_matches('0');
                let y = y.trim_start_matches('0');
                let result = x.len().cmp(&y.len()).then_with(|| x.cmp(y));
                if result != Ordering::Equal {
                    return result;
                }
            }
            _ => {
                let result = a
                    .next()
                    .unwrap()
                    .to_ascii_lowercase()
                    .cmp(&b.next().unwrap().to_ascii_lowercase());
                if result != Ordering::Equal {
                    return result;
                }
            }
        }
    }
}

/// Upstream's token ladder: exact, prefix, word prefix, then substring. All
/// query tokens must match; joined words preserve searches such as wi-fi/wifi.
pub(super) fn asset_rank(name: &str, query: &str) -> (i32, usize, usize, String) {
    fn normalize(text: &str) -> String {
        text.to_lowercase()
            .split(|c: char| c.is_whitespace() || "_-./\\+".contains(c))
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(" ")
    }
    fn token(name: &str, token: &str) -> (i32, usize) {
        if name == token {
            return (100, 0);
        }
        if name.starts_with(token) {
            return (90, 0);
        }
        let Some(first) = name.find(token) else {
            return (0, 0);
        };
        let mut start = first;
        loop {
            if name[..start].ends_with(' ') {
                return (80, name[..start].chars().count());
            }
            let Some(next) =
                name[start + name[start..].chars().next().unwrap().len_utf8()..].find(token)
            else {
                break;
            };
            start += name[start..].chars().next().unwrap().len_utf8() + next;
        }
        (65, name[..first].chars().count())
    }
    let name = normalize(name);
    let query = normalize(query);
    if query.is_empty() {
        return (-100, 0, 0, name);
    }
    if name == query {
        return (-100, 0, name.chars().count(), name);
    }
    let mut score = 100;
    let mut positions = 0;
    let joined = name.replace(' ', "");
    for word in query.split(' ') {
        let mut rank = token(&name, word);
        if rank.0 == 0 && name.contains(' ') {
            rank = token(&joined, word);
        }
        if rank.0 == 0 {
            return (0, 0, name.chars().count(), name);
        }
        score = score.min(rank.0);
        positions += rank.1;
    }
    (-score, positions, name.chars().count(), name)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn upstream_asset_ranking_keeps_long_names_and_rejects_misspellings() {
        for name in ["battery-charging-outline", "battery_charging_20_symbolic"] {
            assert!(asset_rank(name, "battery").0 <= -65);
        }
        assert_eq!(asset_rank("battery", "batery").0, 0);
        assert_eq!(asset_rank("wi-fi", "wifi").0, -100);
        assert_eq!(asset_rank("battery.charging_full", "battery full").0, -80);
        assert_eq!(asset_rank("battery_empty", "battery full").0, 0);
        assert!(asset_rank("home", "home") < asset_rank("go-home", "home"));
        assert_eq!(asset_rank("éclair-lamp", "lamp").1, 7);
    }
    #[test]
    fn page_search_preserves_numeric_order_and_unicode_fuzzy_matching() {
        assert!(natural_cmp("Page 2", "Page 10").is_lt());
        assert!(natural_cmp("Page 999999999999999999999", "Page 1000000000000000000000").is_lt());
        assert_eq!(fuzzy_ratio("éclairage", "éclairage"), 100.);
        assert_eq!(fuzzy_ratio("abcd", "acbd"), 75.);
    }
}
