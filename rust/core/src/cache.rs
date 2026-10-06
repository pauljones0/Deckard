use hashlink::LinkedHashMap;
use std::hash::Hash;
use std::sync::Arc;
use std::time::{Duration, Instant};

/// Bounded immutable byte cache; eviction does not invalidate a borrowed frame.
pub struct ByteCache<K> {
    entries: LinkedHashMap<K, (Arc<[u8]>, Instant)>,
    capacity: usize,
    bytes: usize,
}

impl<K: Eq + Hash + Clone> ByteCache<K> {
    pub fn new(capacity: usize) -> Self {
        Self {
            entries: LinkedHashMap::new(),
            capacity,
            bytes: 0,
        }
    }
    pub fn bytes(&self) -> usize {
        self.bytes
    }
    pub fn len(&self) -> usize {
        self.entries.len()
    }
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
    pub fn set_capacity(&mut self, capacity: usize) {
        self.capacity = capacity;
        while self.bytes > capacity {
            self.evict();
        }
    }
    pub fn get(&mut self, key: &K) -> Option<Arc<[u8]>> {
        let (bytes, stamp) = self.entries.get_mut(key)?;
        *stamp = Instant::now();
        let result = bytes.clone();
        self.entries.to_back(key);
        Some(result)
    }
    pub fn put(&mut self, key: K, data: Arc<[u8]>) {
        if self.capacity == 0 {
            return;
        }
        if let Some((old, _)) = self.entries.remove(&key) {
            self.bytes -= old.len();
        }
        if data.len() > self.capacity {
            return;
        }
        self.bytes += data.len();
        self.entries.insert(key.clone(), (data, Instant::now()));
        while self.bytes > self.capacity {
            self.evict();
        }
    }
    pub fn clear(&mut self) {
        self.entries.clear();
        self.bytes = 0;
    }
    fn evict(&mut self) -> usize {
        let Some((_, (data, _))) = self.entries.pop_front() else {
            return 0;
        };
        self.bytes -= data.len();
        data.len()
    }
    pub fn evict_oldest(&mut self, min_age: Duration, floor: usize) -> usize {
        if self.bytes <= floor {
            return 0;
        }
        let Some((_, (_, stamp))) = self.entries.front() else {
            return 0;
        };
        if stamp.elapsed() < min_age {
            return 0;
        }
        self.evict()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reducing_capacity_evicts_oldest_bytes_without_invalidating_live_frames() {
        let mut cache = ByteCache::new(8);
        cache.put(1, Arc::from(&b"abcd"[..]));
        cache.put(2, Arc::from(&b"efgh"[..]));
        let retained = cache.get(&2).unwrap();
        cache.set_capacity(4);
        assert!(cache.get(&1).is_none());
        assert_eq!(cache.bytes(), 4);
        cache.set_capacity(0);
        assert!(cache.is_empty());
        assert_eq!(&*retained, b"efgh");
    }
    #[test]
    fn byte_accounting_lru_and_retained_frame_survive_eviction() {
        let mut cache = ByteCache::new(5);
        cache.put(1, Arc::from(&b"abc"[..]));
        let retained = cache.get(&1).unwrap();
        cache.put(2, Arc::from(&b"def"[..]));
        assert_eq!(cache.bytes(), 3);
        assert!(cache.get(&1).is_none());
        assert_eq!(&*retained, b"abc");
        cache.put(2, Arc::from(&b"x"[..]));
        assert_eq!(cache.bytes(), 1);
        assert_eq!(cache.evict_oldest(Duration::ZERO, 0), 1);
        assert!(cache.is_empty());
    }
}
