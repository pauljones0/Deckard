use super::*;

pub(super) enum Notice {
    Refresh,
    Finished {
        result: Result<Value>,
        title: String,
        rebuild: bool,
    },
}

type Operation = Box<dyn FnOnce(&mut deckard_core::engine::Engine) -> Result<Value> + Send>;

pub(super) struct Work {
    pub key: Option<String>,
    pub title: String,
    pub rebuild: bool,
    pub execute: Operation,
}

pub(super) struct Bridge {
    pub send: std::sync::mpsc::SyncSender<Work>,
    stop: Arc<AtomicBool>,
    pub hidden: Arc<AtomicBool>,
    pub notified: Arc<AtomicBool>,
    observer: Option<std::thread::JoinHandle<()>>,
    receive: async_channel::Receiver<Notice>,
}

impl Bridge {
    pub fn new(shared: Shared, signal: Arc<AtomicBool>) -> Self {
        let (send, work) = std::sync::mpsc::sync_channel::<Work>(32);
        let (notify, receive) = async_channel::bounded(64);
        let stop = Arc::new(AtomicBool::new(false));
        let hidden = Arc::new(AtomicBool::new(false));
        let notified = Arc::new(AtomicBool::new(false));
        let worker_engine = shared.clone();
        let worker_notify = notify.clone();
        let worker_stop = stop.clone();
        std::thread::spawn(move || {
            while let Ok(job) = work.recv() {
                if worker_stop.load(Ordering::Relaxed) {
                    break;
                }
                let result = (job.execute)(&mut worker_engine.lock().unwrap());
                if worker_notify
                    .send_blocking(Notice::Finished {
                        result,
                        title: job.title,
                        rebuild: job.rebuild,
                    })
                    .is_err()
                {
                    break;
                }
            }
        });
        let observed_stop = stop.clone();
        let observed_hidden = hidden.clone();
        let observed_notified = notified.clone();
        let frames = shared.lock().unwrap().frame_ready.clone();
        let observer = std::thread::spawn(move || {
            let mut previous = None;
            let mut sequence = 0;
            let mut last_frame = std::time::Instant::now();
            while !observed_stop.load(Ordering::Relaxed) {
                let signature = {
                    let engine = shared.lock().unwrap();
                    let mut devices: Vec<_> = engine
                        .devices
                        .values()
                        .map(|device| {
                            (
                                device.serial.clone(),
                                device.epoch,
                                device.connected,
                                device.page.clone(),
                                device.brightness,
                                device.sleeping,
                                device.low_fps,
                                if observed_hidden.load(Ordering::Relaxed) {
                                    0
                                } else {
                                    device.tile_updates.values().sum::<u64>()
                                },
                            )
                        })
                        .collect();
                    devices.sort_unstable();
                    (
                        engine.docs.revision,
                        engine.plugin_revision,
                        engine.errors.back().cloned(),
                        engine.show_window,
                        engine.quit,
                        signal.load(Ordering::Relaxed),
                        devices,
                    )
                };
                if previous.as_ref() != Some(&signature) {
                    if !observed_notified.swap(true, Ordering::Relaxed)
                        && notify.try_send(Notice::Refresh).is_err()
                    {
                        observed_notified.store(false, Ordering::Relaxed);
                    }
                    previous = Some(signature);
                }
                sequence = frames.wait(sequence, Duration::from_millis(50));
                // Coalesce animated previews to the display cadence. USB rendering
                // keeps its independent, source/model-aware frame rate.
                let remaining = Duration::from_millis(16).saturating_sub(last_frame.elapsed());
                if !remaining.is_zero() {
                    std::thread::sleep(remaining);
                }
                last_frame = std::time::Instant::now();
            }
        });
        Self {
            send,
            stop,
            hidden,
            notified,
            observer: Some(observer),
            receive,
        }
    }

    pub fn listen(&self, ui: &Rc<Ui>) {
        let receive = self.receive.clone();
        let weak = Rc::downgrade(ui);
        glib::MainContext::default().spawn_local(async move {
            while let Ok(notice) = receive.recv().await {
                let Some(ui) = weak.upgrade() else {
                    break;
                };
                match notice {
                    Notice::Refresh => {
                        ui.bridge.notified.store(false, Ordering::Relaxed);
                    }
                    Notice::Finished {
                        result,
                        title,
                        rebuild,
                    } => {
                        ui.pending_count
                            .set(ui.pending_count.get().saturating_sub(1));
                        let completion = ui.completions.borrow_mut().remove(&title);
                        let internal = completion.is_some();
                        if let Some(done) = completion {
                            done(&ui, &result);
                        }
                        match result {
                            Ok(value) => {
                                if title == "catalog" {
                                    ui.show_catalog(value);
                                } else if title == "obs-choices" {
                                    *ui.choices.borrow_mut() = value;
                                } else if title == "media-players" {
                                    *ui.players.borrow_mut() = value;
                                } else if title == "migration" {
                                    ui.show_report("Plugin migration", &value);
                                } else if title == "ai-proposal" {
                                    ui.review_ai(value);
                                } else if !title.is_empty() && !internal {
                                    ui.toast(&title);
                                }
                                if rebuild {
                                    ui.force_reload.set(true);
                                }
                            }
                            Err(error) => {
                                ui.toast(&deckard_core::engine::redact(&format!("{error:#}")));
                                ui.force_reload.set(true);
                            }
                        }
                        ui.pump();
                    }
                }
                ui.refresh();
            }
        });
    }
}

impl Drop for Bridge {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.receive.close();
        if let Some(observer) = self.observer.take() {
            let _ = observer.join();
        }
    }
}
