use std::{
    fmt,
    fs::{self, OpenOptions},
    io::{self, Write},
    path::PathBuf,
    sync::{
        Arc, OnceLock,
        atomic::{AtomicUsize, Ordering},
        mpsc::{self, Receiver, SyncSender},
    },
    thread::{self, JoinHandle},
};

enum Message {
    Event(&'static str, String),
    Stop,
}

#[derive(Clone)]
struct Sender {
    channel: SyncSender<Message>,
    dropped: Arc<AtomicUsize>,
}

impl Sender {
    fn write(&self, name: &'static str, details: fmt::Arguments<'_>) {
        if let Err(mpsc::TrySendError::Full(_)) = self
            .channel
            .try_send(Message::Event(name, details.to_string()))
        {
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }
}

pub struct Log {
    sender: Sender,
    background: Option<JoinHandle<io::Result<()>>>,
}

static SENDER: OnceLock<Sender> = OnceLock::new();

impl Log {
    pub fn start(path: PathBuf) -> io::Result<Self> {
        let log = Self::new(path)?;
        let _ = SENDER.set(log.sender.clone());
        Ok(log)
    }

    fn new(path: PathBuf) -> io::Result<Self> {
        let (channel, receiver) = mpsc::sync_channel(512);
        let dropped = Arc::new(AtomicUsize::new(0));
        let writer_dropped = dropped.clone();
        let background = thread::Builder::new()
            .name("reborn-log-writer".into())
            .spawn(move || write_file(path, receiver, writer_dropped))?;
        Ok(Self {
            sender: Sender { channel, dropped },
            background: Some(background),
        })
    }
}

impl Drop for Log {
    fn drop(&mut self) {
        let _ = self.sender.channel.send(Message::Stop);
        if let Some(background) = self.background.take() {
            let _ = background.join();
        }
    }
}

pub fn event(name: &'static str, details: fmt::Arguments<'_>) {
    if let Some(sender) = SENDER.get() {
        sender.write(name, details);
    }
}

fn timestamp() -> String {
    use windows_sys::Win32::{Foundation::SYSTEMTIME, System::SystemInformation::GetSystemTime};

    let mut utc = SYSTEMTIME::default();
    // Windows populates the provided SYSTEMTIME with a UTC timestamp.
    unsafe { GetSystemTime(&mut utc) };
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        utc.wYear, utc.wMonth, utc.wDay, utc.wHour, utc.wMinute, utc.wSecond, utc.wMilliseconds,
    )
}

fn write_file(
    path: PathBuf,
    receiver: Receiver<Message>,
    dropped: Arc<AtomicUsize>,
) -> io::Result<()> {
    if let Some(directory) = path.parent() {
        fs::create_dir_all(directory)?;
    }
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    let pid = std::process::id();
    let mut append = |name, details: &str| -> io::Result<()> {
        let details = details.replace('\r', "\\r").replace('\n', "\\n");
        let line = format!("{} pid={pid} {name} {details}\n", timestamp());
        file.write_all(line.as_bytes())
    };

    while let Ok(message) = receiver.recv() {
        let skipped = dropped.swap(0, Ordering::Relaxed);
        if skipped > 0 {
            append("log.records_dropped", &format!("count={skipped}"))?;
        }
        match message {
            Message::Event(name, details) => append(name, &details)?,
            Message::Stop => break,
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    #[test]
    fn appends_single_lines_that_are_visible_while_running() {
        let path =
            std::env::temp_dir().join(format!("reborn-rust-test-{}.log", std::process::id()));
        fs::write(&path, "previous session\n").unwrap();
        let log = Log::new(path.clone()).unwrap();
        log.sender.write("hunt.start", format_args!("a\nb"));
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let data = fs::read_to_string(&path).unwrap();
            if data.contains("hunt.start a\\nb\n") {
                assert!(data.starts_with("previous session\n"));
                assert_eq!(data.lines().count(), 2);
                break;
            }
            assert!(Instant::now() < deadline, "event never became visible");
            thread::sleep(Duration::from_millis(10));
        }
        log.sender.write("app.exit", format_args!("ok"));
        drop(log);
        assert!(fs::read_to_string(&path).unwrap().contains("app.exit ok"));
        fs::remove_file(path).unwrap();
    }
}
