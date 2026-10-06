//! Linux system readouts, independent of Python and external monitoring programs.
use anyhow::{Context, Result};
use serde_json::{Value, json};
use std::{fs, path::Path, time::Duration};
#[derive(Default)]
pub struct Metrics {
    previous: Option<(u64, u64)>,
}
impl Metrics {
    pub fn read(&mut self) -> Result<(f64, f64)> {
        let current = cpu_counters(&fs::read_to_string("/proc/stat")?)?;
        let cpu = self
            .previous
            .map(|old| {
                let total = current.0.saturating_sub(old.0);
                let idle = current.1.saturating_sub(old.1);
                if total == 0 {
                    0.0
                } else {
                    100.0 * total.saturating_sub(idle) as f64 / total as f64
                }
            })
            .unwrap_or(0.0);
        self.previous = Some(current);
        Ok((cpu, memory_percent(&fs::read_to_string("/proc/meminfo")?)?))
    }
}
fn cpu_counters(text: &str) -> Result<(u64, u64)> {
    let values = text
        .lines()
        .find(|l| l.starts_with("cpu "))
        .context("CPU counters missing")?
        .split_whitespace()
        .skip(1)
        .take(8)
        .map(str::parse::<u64>)
        .collect::<std::result::Result<Vec<_>, _>>()?;
    anyhow::ensure!(values.len() >= 4, "incomplete CPU counters");
    Ok((
        values.iter().sum(),
        values[3] + values.get(4).copied().unwrap_or(0),
    ))
}
fn memory_percent(text: &str) -> Result<f64> {
    let field = |name: &str| {
        text.lines()
            .find(|l| l.starts_with(name))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|v| v.parse::<f64>().ok())
    };
    let total = field("MemTotal:").context("memory total missing")?;
    anyhow::ensure!(total > 0.0, "invalid memory total");
    let available = field("MemAvailable:").unwrap_or_else(|| {
        field("MemFree:").unwrap_or(0.0)
            + field("Buffers:").unwrap_or(0.0)
            + field("Cached:").unwrap_or(0.0)
    });
    Ok((100.0 * (total - available) / total).clamp(0.0, 100.0))
}
pub fn temperature() -> Option<f64> {
    temperature_at(Path::new("/sys/class/hwmon"))
}
fn temperature_at(root: &Path) -> Option<f64> {
    let mut fallback = None;
    for entry in fs::read_dir(root).ok()?.flatten() {
        let p = entry.path();
        let name = fs::read_to_string(p.join("name")).unwrap_or_default();
        if !["coretemp", "k10temp", "zenpower", "cpu_thermal"].contains(&name.trim()) {
            continue;
        }
        let mut sensors = fs::read_dir(&p)
            .ok()?
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                p.file_name()
                    .and_then(|s| s.to_str())
                    .is_some_and(|s| s.starts_with("temp") && s.ends_with("_input"))
            })
            .collect::<Vec<_>>();
        sensors.sort();
        for sensor in sensors {
            let value = fs::read_to_string(&sensor)
                .ok()
                .and_then(|s| s.trim().parse::<f64>().ok())
                .map(|v| v / 1000.0);
            let label = fs::read_to_string(
                sensor.with_file_name(sensor.file_name()?.to_str()?.replace("_input", "_label")),
            )
            .unwrap_or_default();
            if label.trim() == "Tccd1"
                || (name.trim() == "coretemp" && label.starts_with("Package"))
            {
                return value;
            }
            if fallback.is_none() {
                fallback = value;
            }
        }
    }
    fallback
}
pub fn ping(settings: &Value) -> Result<Value> {
    let host = settings["host"].as_str().unwrap_or("");
    if host.is_empty() {
        return Ok(json!({}));
    }
    anyhow::ensure!(
        !host.starts_with('-') && !host.contains('\0'),
        "invalid ping host"
    );
    let count = settings["count"].as_u64().unwrap_or(3).clamp(1, 20);
    let timeout = settings["timeout"].as_u64().unwrap_or(2).clamp(1, 60);
    let result = crate::desktop::run_command(
        &[
            "ping".into(),
            "-n".into(),
            "-q".into(),
            "-c".into(),
            count.to_string(),
            "-W".into(),
            timeout.to_string(),
            host.into(),
        ],
        Duration::from_secs(count * timeout + 10),
    );
    let times = result.ok().and_then(|text| parse_ping(&text));
    let mut overlay = json!({"labels":{"center":{"text":"Unreachable","font-size":18}}});
    if let Some(times) = times {
        let index = match settings["value"].as_str().unwrap_or("avg") {
            "min" => 0,
            "max" => 2,
            _ => 1,
        };
        let time = times[index];
        overlay["labels"]["center"]["text"] = json!(if time >= 10.0 {
            format!("{time:.0} ms")
        } else {
            format!("{time:.1} ms")
        });
    }
    if settings["color_background"].as_bool().unwrap_or(true) {
        overlay["color"] = if times.is_some() {
            json!([45, 130, 60, 255])
        } else {
            json!([150, 40, 40, 255])
        };
    }
    Ok(overlay)
}
fn parse_ping(text: &str) -> Option<[f64; 3]> {
    let captures = regex::Regex::new(r"min/avg/max[^=]*=\s*([\d.]+)/([\d.]+)/([\d.]+)")
        .ok()?
        .captures(text)?;
    Some([
        captures[1].parse().ok()?,
        captures[2].parse().ok()?,
        captures[3].parse().ok()?,
    ])
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn kernel_counters_do_not_double_count_guest_or_cache() {
        assert_eq!(
            cpu_counters("cpu  10 20 30 40 50 60 70 80 90 100\n").unwrap(),
            (360, 90)
        );
        assert_eq!(
            memory_percent(
                "MemTotal: 1000 kB\nMemFree: 10 kB\nMemAvailable: 700 kB\nCached: 40 kB\n"
            )
            .unwrap(),
            30.0
        );
        assert_eq!(
            parse_ping("rtt min/avg/max/mdev = 0.010/0.020/0.030/0.002 ms"),
            Some([0.01, 0.02, 0.03])
        );
        assert_eq!(
            parse_ping("round-trip min/avg/max = 1/2/3 ms"),
            Some([1.0, 2.0, 3.0])
        );
    }
    #[test]
    fn amd_temperature_uses_ccd_and_skips_unrelated_sensors() {
        let tmp = tempfile::tempdir().unwrap();
        let d = tmp.path().join("hwmon0");
        fs::create_dir(&d).unwrap();
        fs::write(d.join("name"), "k10temp\n").unwrap();
        fs::write(d.join("temp1_input"), "70000\n").unwrap();
        fs::write(d.join("temp1_label"), "Tctl\n").unwrap();
        fs::write(d.join("temp3_input"), "55000\n").unwrap();
        fs::write(d.join("temp3_label"), "Tccd1\n").unwrap();
        assert_eq!(temperature_at(tmp.path()), Some(55.0));
    }
}
