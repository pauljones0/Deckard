use serde_json::json;
use std::io::{self, BufRead};
fn main() {
    for line in io::stdin().lock().lines() {
        let Ok(line) = line else { break };
        let Ok(request) = serde_json::from_str::<serde_json::Value>(&line) else {
            break;
        };
        println!(
            "{}",
            json!({"jsonrpc":"2.0", "id":request["id"], "result": {"label":request["params"]["settings"]["label"].as_str().unwrap_or("Hello Rust"), "color":[24,106,180]}})
        );
    }
}
