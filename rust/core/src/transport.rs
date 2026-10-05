use std::collections::HashSet;
use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

#[derive(Default)]
struct Queue {
    next: u64,
    serving: u64,
    abandoned: HashSet<u64>,
}

/// FIFO transport mutex. A timed-out reader must never strand the writer queue.
#[derive(Default)]
pub struct FairLock {
    queue: Mutex<Queue>,
    ready: Condvar,
}

impl FairLock {
    pub fn acquire(&self, blocking: bool, timeout: Option<Duration>) -> bool {
        let mut queue = self.queue.lock().unwrap_or_else(|error| error.into_inner());
        if !blocking {
            if queue.serving != queue.next {
                return false;
            }
            queue.next += 1;
            return true;
        }
        let ticket = queue.next;
        queue.next += 1;
        let deadline = timeout.map(|duration| Instant::now() + duration);
        while queue.serving != ticket {
            queue = if let Some(deadline) = deadline {
                let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                    queue.abandoned.insert(ticket);
                    return false;
                };
                self.ready
                    .wait_timeout(queue, remaining)
                    .unwrap_or_else(|error| error.into_inner())
                    .0
            } else {
                self.ready
                    .wait(queue)
                    .unwrap_or_else(|error| error.into_inner())
            };
        }
        true
    }
    pub fn release(&self) -> Result<(), &'static str> {
        let mut queue = self.queue.lock().unwrap_or_else(|error| error.into_inner());
        if queue.serving == queue.next {
            return Err("release unlocked FairLock");
        }
        queue.serving += 1;
        loop {
            let serving = queue.serving;
            if !queue.abandoned.remove(&serving) {
                break;
            }
            queue.serving += 1;
        }
        self.ready.notify_all();
        Ok(())
    }
    pub fn locked(&self) -> bool {
        let queue = self.queue.lock().unwrap_or_else(|error| error.into_inner());
        queue.serving != queue.next
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn timeout_does_not_leave_a_hole_in_the_queue() {
        let lock = FairLock::default();
        assert!(lock.acquire(true, None));
        assert!(!lock.acquire(false, None));
        assert!(!lock.acquire(true, Some(Duration::from_millis(1))));
        lock.release().unwrap();
        assert!(lock.acquire(false, None));
        lock.release().unwrap();
        assert!(lock.release().is_err());
    }
}
