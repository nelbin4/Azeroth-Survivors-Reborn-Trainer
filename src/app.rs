use std::{
    io::{self, Cursor},
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, TryRecvError},
    },
    thread,
    time::{Duration, Instant},
};

use eframe::egui::{self, Button, Color32, RichText};
use egui_extras::{Column, TableBuilder};

use crate::{
    hunt::{self, Snapshot},
    process::{self, Games, Session},
    settings::Settings,
    update,
};

const GOLD: Color32 = Color32::from_rgb(227, 190, 105);
const MINT: Color32 = Color32::from_rgb(114, 224, 190);
const FRAME: Color32 = Color32::from_rgb(20, 32, 44);
const VERSION: &str = env!("CARGO_PKG_VERSION");

fn display_version() -> String {
    match VERSION.strip_suffix("-beta") {
        Some(base) => format!("{base}b"),
        None => format!("v{VERSION}"),
    }
}

pub fn icon() -> io::Result<egui::IconData> {
    let image = include_bytes!("../assets/trainer-logo.png");
    let mut reader = png::Decoder::new(Cursor::new(image))
        .read_info()
        .map_err(io::Error::other)?;
    let len = reader
        .output_buffer_size()
        .ok_or_else(|| io::Error::other("Trainer icon is too large."))?;
    let mut rgba = vec![0; len];
    let frame = reader.next_frame(&mut rgba).map_err(io::Error::other)?;
    if frame.color_type != png::ColorType::Rgba || frame.bit_depth != png::BitDepth::Eight {
        return Err(io::Error::other("Trainer icon must be RGBA8."));
    }
    rgba.truncate(frame.buffer_size());
    Ok(egui::IconData {
        rgba,
        width: frame.width,
        height: frame.height,
    })
}

struct Search {
    stop: Arc<AtomicBool>,
    started: Instant,
    session: Session,
    result: Receiver<io::Result<Snapshot>>,
}

impl Drop for Search {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

pub struct TrainerApp {
    settings: Settings,
    session: Option<Session>,
    process_result: Option<Receiver<io::Result<Games>>>,
    launch_result: Option<Receiver<io::Result<Session>>>,
    next_process_check: Instant,
    running: bool,
    scan: Option<Search>,
    snapshot: Option<Snapshot>,
    status: String,
    error: Option<String>,
    show_details: bool,
    update_result: Option<Receiver<io::Result<update::ReleaseStatus>>>,
    update_status: String,
    update_tag: Option<String>,
}

fn task<T: Send + 'static>(
    context: &egui::Context,
    call: impl FnOnce() -> T + Send + 'static,
) -> Receiver<T> {
    let (sender, receiver) = mpsc::channel();
    let context = context.clone();
    thread::spawn(move || {
        let _ = sender.send(call());
        context.request_repaint();
    });
    receiver
}

fn finished<T>(receiver: &mut Option<Receiver<io::Result<T>>>) -> Option<io::Result<T>> {
    let result = match receiver.as_ref()?.try_recv() {
        Ok(result) => result,
        Err(TryRecvError::Empty) => return None,
        Err(TryRecvError::Disconnected) => {
            Err(io::Error::other("Background task stopped unexpectedly."))
        }
    };
    *receiver = None;
    Some(result)
}

impl TrainerApp {
    pub fn new(context: &eframe::CreationContext<'_>) -> Self {
        let mut visuals = egui::Visuals::dark();
        visuals.panel_fill = FRAME;
        visuals.selection.bg_fill = Color32::from_rgb(35, 68, 63);
        context.egui_ctx.set_visuals(visuals);
        let (settings, error) = match Settings::load() {
            Ok(settings) => (settings, None),
            Err(error) => (Settings::default(), Some(format!("Settings: {error}"))),
        };
        Self {
            settings,
            error,
            session: None,
            process_result: None,
            launch_result: None,
            next_process_check: Instant::now(),
            running: false,
            scan: None,
            snapshot: None,
            status: "Start Reborn to find a character.".into(),
            show_details: false,
            update_result: Some(task(&context.egui_ctx, || update::check(VERSION))),
            update_status: "Checking releases…".into(),
            update_tag: None,
        }
    }

    fn set_session(&mut self, session: Option<Session>) {
        if self.session != session {
            self.session = session;
            self.scan = None;
            self.snapshot = None;
            self.status = if self.session.is_some() {
                "Start a run, then click Find.".into()
            } else if self.running {
                "Choose the correct game installation.".into()
            } else {
                "Start Reborn to find a character.".into()
            };
        }
    }

    fn poll(&mut self, context: &egui::Context) {
        if let Some(result) = finished(&mut self.process_result) {
            match result {
                Ok(games) => {
                    self.running = games.running();
                    self.set_session(
                        games
                            .select(self.settings.game_path(), self.session.as_ref())
                            .cloned(),
                    );
                }
                Err(error) => {
                    self.running = true;
                    self.set_session(None);
                    self.error = Some(format!("Game detection: {error}"));
                }
            }
        }
        if let Some(result) = finished(&mut self.launch_result) {
            match result {
                Ok(session) => {
                    self.running = true;
                    self.set_session(Some(session));
                }
                Err(error) => self.error = Some(error.to_string()),
            }
        }
        if self.process_result.is_none()
            && self.launch_result.is_none()
            && Instant::now() >= self.next_process_check
        {
            self.process_result = Some(task(context, process::discover));
            self.next_process_check = Instant::now() + Duration::from_secs(1);
        }
        let outcome = self
            .scan
            .as_ref()
            .and_then(|search| match search.result.try_recv() {
                Ok(result) => Some(result),
                Err(TryRecvError::Empty) => None,
                Err(TryRecvError::Disconnected) => {
                    Some(Err(io::Error::other("Find stopped unexpectedly.")))
                }
            });
        if let Some(outcome) = outcome {
            let search = self.scan.take().unwrap();
            if self.session.as_ref() == Some(&search.session)
                && !search.stop.load(Ordering::Relaxed)
            {
                match outcome {
                    Ok(snapshot) => {
                        self.status = if snapshot.character.is_some() {
                            "Character found. Diagnostic readings only.".into()
                        } else {
                            "No live character verified. Start a run and retry.".into()
                        };
                        self.snapshot = Some(snapshot);
                    }
                    Err(error) => self.status = error.to_string(),
                }
            }
        }
        if let Some(result) = finished(&mut self.update_result) {
            match result {
                Ok(status) => {
                    self.update_status = format!("Latest stable · {}", status.tag);
                    self.update_tag = status.available.then_some(status.tag);
                }
                Err(error) => self.update_status = format!("Release check: {error}"),
            }
        }
        context.request_repaint_after(Duration::from_millis(250));
    }

    fn start_find(&mut self, context: &egui::Context) {
        if let Some(session) = self.session.clone() {
            self.snapshot = None;
            self.error = None;
            self.status = "Finding the selected character…".into();
            let stop = Arc::new(AtomicBool::new(false));
            let worker_session = session.clone();
            let worker_stop = stop.clone();
            let result = task(context, move || hunt::find(&worker_session, &worker_stop));
            self.scan = Some(Search {
                stop,
                started: Instant::now(),
                session,
                result,
            });
        }
    }

    fn locate(&mut self) -> Option<PathBuf> {
        let path = rfd::FileDialog::new()
            .add_filter("Windows executable", &["exe"])
            .pick_file()?;
        if !path
            .file_name()
            .is_some_and(|name| name.eq_ignore_ascii_case(process::GAME_EXE))
        {
            self.error = Some("Choose AzerothSurvivorsGame.exe.".into());
            return None;
        }
        if let Err(error) = self.settings.set_game_path(&path) {
            self.error = Some(format!("Could not save game path: {error}"));
        }
        Some(path)
    }

    fn footer(&mut self, context: &egui::Context, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            if ui
                .add_enabled(
                    !self.running && self.launch_result.is_none() && self.process_result.is_none(),
                    Button::new("Run Game"),
                )
                .clicked()
                && let Some(path) = self
                    .settings
                    .game_path()
                    .map(PathBuf::from)
                    .or_else(|| self.locate())
            {
                self.launch_result = Some(task(context, move || process::launch(&path)));
            }
            if ui.button("Locate exe file").clicked() {
                self.locate();
            }
            self.show_details |= ui.button("Advanced").clicked();
            ui.with_layout(egui::Layout::right_to_left(egui::Align::TOP), |ui| {
                ui.vertical(|ui| {
                    ui.horizontal(|ui| {
                        if ui.button("Github Page").clicked() {
                            context.open_url(egui::OpenUrl::new_tab(update::PROJECT));
                        }
                        let label = if self.update_tag.is_some() {
                            "View update"
                        } else {
                            "Check updates"
                        };
                        if ui
                            .add_enabled(self.update_result.is_none(), Button::new(label))
                            .clicked()
                        {
                            if let Some(tag) = &self.update_tag {
                                context.open_url(egui::OpenUrl::new_tab(format!(
                                    "{}/releases/tag/{tag}",
                                    update::PROJECT
                                )));
                            } else {
                                self.update_status = "Checking releases…".into();
                                self.update_result = Some(task(context, || update::check(VERSION)));
                            }
                        }
                    });
                    ui.label(
                        RichText::new(format!("{} · {}", display_version(), self.update_status))
                            .small()
                            .color(MINT),
                    );
                });
            });
        });
    }

    fn diagnostic_text(&self) -> String {
        let pid = self
            .session
            .as_ref()
            .map_or_else(|| "Disconnected".into(), |s| format!("PID {}", s.pid));
        let build = self.snapshot.as_ref().map_or("—", |s| s.build.as_str());
        let character = self
            .snapshot
            .as_ref()
            .and_then(|s| s.character.as_ref())
            .map_or_else(
                || "—".into(),
                |c| format!("{} · slot {} of {}", c.name, c.index + 1, c.count),
            );
        format!(
            "{}\nGame process: {pid}\nBuild: {build}\nCharacter: {character}\nAccess: Read-only",
            self.status
        )
    }
}

impl eframe::App for TrainerApp {
    fn update(&mut self, context: &egui::Context, _: &mut eframe::Frame) {
        self.poll(context);
        egui::TopBottomPanel::bottom("trainer-footer").show(context, |ui| self.footer(context, ui));
        egui::CentralPanel::default().show(context, |ui| {
            ui.heading(
                RichText::new("AZEROTH SURVIVORS REBORN")
                    .color(GOLD)
                    .strong(),
            );
            ui.label("Rust preview · Read-only");
            if let Some(session) = &self.session {
                ui.label(RichText::new(format!("Connected · PID {}", session.pid)).color(MINT));
            } else if self.running {
                ui.label("Reborn is running. The process could not be uniquely selected.");
            } else {
                ui.label("Waiting for Reborn.");
            }
            ui.separator();
            ui.horizontal(|ui| {
                if self.scan.is_some() {
                    if ui.button("Stop search").clicked() {
                        self.scan = None;
                        self.status = "Search stopped.".into();
                    }
                } else if ui
                    .add_enabled(self.session.is_some(), Button::new("Find"))
                    .clicked()
                {
                    self.start_find(context);
                }
                ui.label(&self.status);
            });
            if let Some(scan) = &self.scan {
                let elapsed = scan.started.elapsed().as_secs();
                ui.add(egui::Spinner::new());
                ui.label(format!("{}:{:02} elapsed", elapsed / 60, elapsed % 60));
            }
            if let Some(error) = &self.error {
                ui.colored_label(Color32::LIGHT_RED, error);
            }
            ui.add_space(14.0);
            let character = self.snapshot.as_ref().and_then(|s| s.character.as_ref());
            let rows = [
                (
                    "Selected character",
                    character.map_or_else(|| "—".into(), |c| c.name.clone()),
                    "Identity",
                ),
                (
                    "Player slot",
                    character
                        .map_or_else(|| "—".into(), |c| format!("{} / {}", c.index + 1, c.count)),
                    "Identity",
                ),
                (
                    "Health candidate",
                    character.map_or_else(
                        || "—".into(),
                        |c| format!("{} / {}", c.health_candidate[0], c.health_candidate[1]),
                    ),
                    "Diagnostic",
                ),
            ];
            TableBuilder::new(ui)
                .striped(true)
                .resizable(true)
                .column(Column::initial(210.0).at_least(90.0))
                .column(Column::initial(160.0).at_least(80.0))
                .column(Column::remainder().at_least(80.0))
                .header(28.0, |mut header| {
                    for label in ["Reading", "Current", "Status"] {
                        header.col(|ui| {
                            ui.strong(label);
                        });
                    }
                })
                .body(|mut body| {
                    for (name, value, status) in rows {
                        body.row(31.0, |mut row| {
                            row.col(|ui| {
                                ui.label(name);
                            });
                            row.col(|ui| {
                                ui.label(value);
                            });
                            row.col(|ui| {
                                ui.label(status);
                            });
                        });
                    }
                });
        });
        if self.show_details {
            let text = self.diagnostic_text();
            egui::Window::new("Detection details")
                .open(&mut self.show_details)
                .show(context, |ui| {
                    if ui.button("Copy details").clicked() {
                        context.copy_text(text.clone());
                    }
                    ui.monospace(text);
                });
        }
    }
}
