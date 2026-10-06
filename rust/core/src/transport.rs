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

#[derive(Default)]
struct FrameQueue {
    frame: Option<std::sync::Arc<crate::render::Frame>>,
}
/// One queued frame plus one in-flight transfer. Decoding and USB writes overlap
/// without building a frame history; revisions replace obsolete pending output.
#[derive(Default)]
pub struct FrameMailbox {
    queue: Mutex<FrameQueue>,
    ready: Condvar,
}
impl FrameMailbox {
    pub fn available(&self) -> bool {
        self.queue
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .frame
            .is_none()
    }
    pub fn publish(&self, frame: std::sync::Arc<crate::render::Frame>, hardware: bool) {
        let mut queue = self.queue.lock().unwrap_or_else(|p| p.into_inner());
        queue.frame = hardware.then_some(frame);
        self.ready.notify_all();
    }
    pub fn take(
        &self,
        in_flight_revision: Option<u64>,
    ) -> Option<std::sync::Arc<crate::render::Frame>> {
        let mut queue = self.queue.lock().unwrap_or_else(|p| p.into_inner());
        if in_flight_revision.is_some_and(|revision| {
            queue
                .frame
                .as_ref()
                .is_some_and(|frame| frame.revision == revision)
        }) {
            return None;
        }
        let frame = queue.frame.take();
        self.ready.notify_all();
        frame
    }
    pub fn finish(&self) {
        self.ready.notify_all();
    }
    pub fn wait_output(&self) {
        let queue = self.queue.lock().unwrap_or_else(|p| p.into_inner());
        let _guard = self
            .ready
            .wait_timeout_while(queue, Duration::from_millis(8), |queue| {
                queue.frame.is_none()
            })
            .unwrap_or_else(|p| p.into_inner());
    }
    pub fn clear(&self) {
        let mut queue = self.queue.lock().unwrap_or_else(|p| p.into_inner());
        queue.frame = None;
        self.ready.notify_all();
    }
    pub fn wait(&self, deadline: Option<Instant>) {
        let deadline = deadline.unwrap_or_else(|| Instant::now() + Duration::from_millis(20));
        let queue = self.queue.lock().unwrap_or_else(|p| p.into_inner());
        let timeout = if queue.frame.is_some() {
            Duration::from_millis(20)
        } else {
            deadline
                .saturating_duration_since(Instant::now())
                .min(Duration::from_millis(20))
        };
        let _guard = self
            .ready
            .wait_timeout_while(queue, timeout, |queue| {
                queue.frame.is_some() || Instant::now() < deadline
            })
            .unwrap_or_else(|p| p.into_inner());
    }
}
#[cfg(test)]
mod frame_tests {
    use super::*;
    use std::sync::Arc;
    fn frame(revision: u64) -> Arc<crate::render::Frame> {
        Arc::new(crate::render::Frame {
            tiles: vec![],
            strip: None,
            revision,
        })
    }
    #[test]
    fn slow_transport_is_bounded_and_config_replacement_does_not_lose_backpressure() {
        let mailbox = FrameMailbox::default();
        mailbox.publish(frame(1), true);
        assert!(!mailbox.available());
        assert_eq!(mailbox.take(None).unwrap().revision, 1);
        assert!(
            mailbox.available(),
            "one next frame may overlap the current transfer"
        );
        mailbox.publish(frame(2), true);
        assert!(
            mailbox.take(Some(2)).is_none(),
            "a matching in-flight frame must finish before taking the next"
        );
        mailbox.publish(frame(3), true);
        mailbox.finish();
        assert!(!mailbox.available());
        assert_eq!(mailbox.take(None).unwrap().revision, 3);
        mailbox.finish();
        assert!(mailbox.available());
        mailbox.publish(frame(4), true);
        mailbox.clear();
        assert!(mailbox.available());
        assert!(mailbox.take(None).is_none());
        mailbox.publish(frame(5), false);
        assert!(
            mailbox.available(),
            "simulation must not invent a USB limit"
        );
    }
}

/// Keep the physical-key cursor across replacements, but always finish a full
/// sweep of the new frame. Frequent live revisions must not starve late keys.
#[derive(Default)]
pub struct TileCursor {
    index: usize,
    remaining: usize,
    total: usize,
}
impl TileCursor {
    pub fn replace(&mut self, total: usize) {
        self.total = total;
        self.remaining = total;
        self.index %= total.max(1);
    }
    pub fn index(&self) -> usize {
        self.index
    }
    pub fn advance(&mut self) -> bool {
        self.index = (self.index + 1) % self.total.max(1);
        self.remaining = self.remaining.saturating_sub(1);
        self.remaining == 0
    }
}
#[cfg(test)]
mod cursor_tests {
    use super::*;
    #[test]
    fn replacement_covers_the_whole_new_frame_and_frequent_revisions_remain_fair() {
        let mut cursor = TileCursor::default();
        cursor.replace(5);
        assert_eq!(cursor.index(), 0);
        assert!(!cursor.advance());
        cursor.replace(5);
        let mut seen = vec![];
        loop {
            seen.push(cursor.index());
            if cursor.advance() {
                break;
            }
        }
        assert_eq!(seen, vec![1, 2, 3, 4, 0]);
        let mut seen = HashSet::new();
        for _ in 0..25 {
            cursor.replace(5);
            seen.insert(cursor.index());
            cursor.advance();
        }
        assert_eq!(seen.len(), 5);
    }
}

/// Wake preview consumers when pixels change. A sequence closes the race between
/// reading the current frame and beginning to wait; static previews stay asleep.
#[derive(Default)]
pub struct FrameSignal {
    sequence: Mutex<u64>,
    changed: Condvar,
}
impl FrameSignal {
    pub fn notify(&self) {
        let mut sequence = self.sequence.lock().unwrap_or_else(|p| p.into_inner());
        *sequence = sequence.wrapping_add(1);
        self.changed.notify_all();
    }
    pub fn wait(&self, observed: u64, timeout: Duration) -> u64 {
        let sequence = self.sequence.lock().unwrap_or_else(|p| p.into_inner());
        *self
            .changed
            .wait_timeout_while(sequence, timeout, |sequence| *sequence == observed)
            .unwrap_or_else(|p| p.into_inner())
            .0
    }
}

#[cfg(test)]
mod signal_tests {
    use super::*;
    #[test]
    fn pixel_notification_between_snapshot_and_wait_is_not_lost() {
        let signal = FrameSignal::default();
        signal.notify();
        assert_eq!(signal.wait(0, Duration::ZERO), 1);
        assert_eq!(signal.wait(1, Duration::ZERO), 1);
        signal.notify();
        assert_eq!(signal.wait(1, Duration::ZERO), 2);
    }
}
