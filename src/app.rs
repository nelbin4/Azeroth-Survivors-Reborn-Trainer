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
    stats_ui::{STATS, Stat, grouped},
    update,
};

const GOLD: Color32 = Color32::from_rgb(227, 190, 105);
const MINT: Color32 = Color32::from_rgb(114, 224, 190);
const FRAME: Color32 = Color32::from_rgb(20, 32, 44);
const SURFACE: Color32 = Color32::from_rgb(36, 54, 75);
const MUTED: Color32 = Color32::from_rgb(169, 184, 200);
const TEXT: Color32 = Color32::from_rgb(229, 233, 239);
const ACCENT: Color32 = Color32::from_rgb(86, 214, 176);
const ACCENT_TEXT: Color32 = Color32::from_rgb(16, 37, 31);
const EDITOR_WIDTH: f32 = 242.0;
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
    stop_result: Option<Receiver<io::Result<()>>>,
    confirm_stop: Option<Session>,
    next_process_check: Instant,
    running: bool,
    scan: Option<Search>,
    snapshot: Option<Snapshot>,
    status: String,
    error: Option<String>,
    show_details: bool,
    selected: usize,
    logo: Option<egui::TextureHandle>,
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
        visuals.window_fill = FRAME;
        visuals.override_text_color = Some(TEXT);
        visuals.widgets.inactive.weak_bg_fill = SURFACE;
        visuals.widgets.hovered.weak_bg_fill = Color32::from_rgb(53, 81, 108);
        visuals.widgets.active.weak_bg_fill = Color32::from_rgb(53, 81, 108);
        visuals.selection.bg_fill = Color32::from_rgb(35, 68, 63);
        context.egui_ctx.set_visuals(visuals);
        context.egui_ctx.style_mut(|style| {
            style.spacing.item_spacing = egui::vec2(8.0, 7.0);
            style.spacing.button_padding = egui::vec2(12.0, 7.0);
            style.spacing.interact_size.y = 27.0;
            for (style_name, size) in [
                (egui::TextStyle::Body, 15.0),
                (egui::TextStyle::Button, 14.0),
            ] {
                style
                    .text_styles
                    .insert(style_name, egui::FontId::proportional(size));
            }
        });
        let logo = icon().ok().map(|icon| {
            let image = egui::ColorImage::from_rgba_unmultiplied(
                [icon.width as usize, icon.height as usize],
                &icon.rgba,
            );
            context
                .egui_ctx
                .load_texture("trainer-logo", image, egui::TextureOptions::LINEAR)
        });
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
            stop_result: None,
            confirm_stop: None,
            next_process_check: Instant::now(),
            running: false,
            scan: None,
            snapshot: None,
            status: "Start Reborn to find a character.".into(),
            show_details: false,
            selected: 0,
            logo,
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
        if let Some(result) = finished(&mut self.stop_result) {
            if let Err(error) = result {
                self.error = Some(format!("Could not stop Reborn: {error}"));
            }
            self.next_process_check = Instant::now();
        }
        if self.process_result.is_none()
            && self.launch_result.is_none()
            && self.stop_result.is_none()
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
        ui.horizontal_top(|ui| {
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
            if ui
                .add_enabled(
                    self.session.is_some() && self.stop_result.is_none(),
                    Button::new("Stop Game"),
                )
                .clicked()
            {
                self.confirm_stop = self.session.clone();
            }
            if ui.button("Locate exe file").clicked() {
                self.locate();
            }
            self.show_details |= ui.button("Advanced").clicked();
            ui.allocate_ui_with_layout(
                egui::vec2(ui.available_width(), 0.0),
                egui::Layout::top_down(egui::Align::RIGHT),
                |ui| {
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
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
                        if ui.button("Github Page").clicked() {
                            context.open_url(egui::OpenUrl::new_tab(update::PROJECT));
                        }
                    });
                    ui.label(
                        RichText::new(format!("{} · {}", display_version(), self.update_status))
                            .small()
                            .color(MINT),
                    );
                },
            );
        });
    }

    fn stop_confirmation(&mut self, context: &egui::Context) {
        let Some(session) = self.confirm_stop.clone() else {
            return;
        };
        let mut cancel = false;
        let mut stop = false;
        egui::Modal::new(egui::Id::new("stop-reborn")).show(context, |ui| {
            ui.heading("Stop Reborn?");
            ui.label(format!("PID {}", session.pid));
            ui.label(session.path.display().to_string());
            ui.label("Stopping Reborn can lose unsaved run progress.");
            ui.horizontal(|ui| {
                cancel = ui.button("Cancel").clicked();
                stop = ui.button("Stop Game").clicked();
            });
        });
        if cancel || context.input(|input| input.key_pressed(egui::Key::Escape)) {
            self.confirm_stop = None;
        } else if stop {
            self.confirm_stop = None;
            self.scan = None;
            self.snapshot = None;
            self.stop_result = Some(task(context, move || process::stop(&session)));
        }
    }

    fn stat_display(&self, stat: &Stat) -> Option<String> {
        let character = self.snapshot.as_ref()?.character.as_ref()?;
        let index = match stat.key {
            "health" => 0,
            "max_health" => 1,
            _ => return None,
        };
        Some(format!(
            "≈ {}",
            grouped(character.health_candidate[index] as u64)
        ))
    }

    fn stats_table(&mut self, ui: &mut egui::Ui, width: f32) {
        let stat_width = (width - 403.0).max(125.0);
        let columns = [
            ("Freeze", 64.0, 56.0),
            ("Stat", stat_width, 100.0),
            ("Current", 88.0, 62.0),
            ("Target", 102.0, 59.0),
            ("Status", 117.0, 68.0),
        ];
        ui.spacing_mut().item_spacing.y = 2.0;
        let mut selected = self.selected;
        let mut table = TableBuilder::new(ui)
            .striped(true)
            .resizable(true)
            .sense(egui::Sense::click())
            .auto_shrink([false, false]);
        for (_, width, minimum) in columns {
            table = table.column(Column::initial(width).at_least(minimum).clip(true));
        }
        table.header(31.0, |mut header| {
            for (title, _, _) in columns {
                header.col(|ui| { ui.strong(title); });
            }
        }).body(|mut body| {
            for (index, stat) in STATS.iter().enumerate() {
                let target = stat.target(self.settings.stat_preferences());
                let mut checked = stat.checked(self.settings.stat_preferences());
                let current = self.stat_display(stat);
                let status = if current.is_some() { "Diagnostic" }
                    else if self.scan.is_some() { "Searching" } else { "Not found" };
                body.row(29.0, |mut row| {
                    row.set_selected(index == self.selected);
                    row.col(|ui| {
                        ui.allocate_ui_with_layout(egui::vec2(ui.available_width(), 22.0), egui::Layout::top_down(egui::Align::Center), |ui| {
                            ui.add_enabled(false, egui::Checkbox::without_text(&mut checked))
                                .on_disabled_hover_text("Saved freeze preference. Writes are disabled in this preview.");
                        });
                    });
                    row.col(|ui| { ui.label(stat.label); });
                    row.col(|ui| { ui.label(current.as_deref().unwrap_or("—")); });
                    row.col(|ui| { ui.label(target); });
                    row.col(|ui| { ui.label(RichText::new(status).color(MUTED)); });
                    if row.response().clicked() {
                        selected = index;
                    }
                });
            }
        });
        self.selected = selected;
    }

    fn search_progress(&self, ui: &mut egui::Ui) {
        let (rect, _) =
            ui.allocate_exact_size(egui::vec2(ui.available_width(), 8.0), egui::Sense::hover());
        ui.painter().rect_filled(rect, 4, SURFACE);
        if let Some(scan) = &self.scan {
            let elapsed = scan.started.elapsed().as_secs_f32();
            let width = rect.width() * 0.32;
            let phase = (elapsed * 0.45).fract() * 2.0;
            let progress = if phase < 1.0 { phase } else { 2.0 - phase };
            let left = rect.left() + progress * (rect.width() - width);
            let fill = egui::Rect::from_min_size(
                egui::pos2(left, rect.top()),
                egui::vec2(width, rect.height()),
            );
            ui.painter().rect_filled(fill, 4, ACCENT);
            ui.ctx().request_repaint_after(Duration::from_millis(35));
        }
    }

    fn stat_editor(&mut self, context: &egui::Context, ui: &mut egui::Ui) {
        let width = ui.available_width();
        let finding = self.scan.is_some();
        let title = if finding { "Stop search" } else { "Find" };
        let find = Button::new(RichText::new(title).size(24.0).strong().color(ACCENT_TEXT))
            .fill(ACCENT)
            .min_size(egui::vec2(width, 53.0));
        if ui
            .add_enabled(finding || self.session.is_some(), find)
            .clicked()
        {
            if finding {
                self.scan = None;
                self.status = "Search stopped.".into();
            } else {
                self.start_find(context);
            }
        }
        ui.add_enabled(
            false,
            Button::new("Stop Freeze").min_size(egui::vec2(width, 32.0)),
        )
        .on_disabled_hover_text("Stat writes are disabled in 0.9.9b.");
        self.search_progress(ui);
        ui.colored_label(MINT, &self.status);
        if let Some(scan) = &self.scan {
            let elapsed = scan.started.elapsed().as_secs();
            ui.label(
                RichText::new(format!("{}:{:02} elapsed", elapsed / 60, elapsed % 60)).color(MUTED),
            );
        }
        ui.separator();
        let stat = &STATS[self.selected];
        let current = self.stat_display(stat);
        ui.heading(RichText::new(stat.label).size(19.0).strong());
        ui.label(
            RichText::new(current.as_deref().unwrap_or("—"))
                .size(26.0)
                .color(MINT),
        );
        ui.label(if stat.percent { "Target (%)" } else { "Target" });
        let mut target = stat.target(self.settings.stat_preferences());
        ui.add_enabled(
            false,
            egui::TextEdit::singleline(&mut target).desired_width(width - 10.0),
        );
        ui.horizontal(|ui| {
            ui.add_enabled(false, Button::new("Apply").fill(ACCENT));
            ui.add_enabled(false, Button::new("Use maximum"));
        });
        ui.label(
            RichText::new(if current.is_some() {
                "Approximate reading. Stat writes are disabled in this preview."
            } else {
                "This preview is read-only."
            })
            .color(MUTED),
        );
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
        let frame = egui::Frame::new()
            .fill(FRAME)
            .inner_margin(egui::Margin::same(16));
        egui::TopBottomPanel::bottom("trainer-footer")
            .frame(frame)
            .show(context, |ui| self.footer(context, ui));
        egui::CentralPanel::default()
            .frame(frame)
            .show(context, |ui| {
                ui.horizontal(|ui| {
                    if let Some(logo) = &self.logo {
                        ui.image((logo.id(), egui::vec2(32.0, 32.0)));
                    }
                    ui.heading(
                        RichText::new("AZEROTH SURVIVORS REBORN")
                            .size(26.0)
                            .color(GOLD)
                            .strong(),
                    );
                });
                if let Some(session) = &self.session {
                    ui.label(RichText::new(format!("Connected · PID {}", session.pid)).color(MINT));
                } else if self.running {
                    ui.label("Reborn is running. The process could not be uniquely selected.");
                } else {
                    ui.label("Waiting for Reborn.");
                }
                if let Some(error) = &self.error {
                    ui.colored_label(Color32::LIGHT_RED, error);
                }
                ui.add_space(8.0);
                egui::SidePanel::right("selected-stat-editor")
                    .exact_width(EDITOR_WIDTH)
                    .resizable(false)
                    .frame(
                        egui::Frame::new()
                            .fill(FRAME)
                            .inner_margin(egui::Margin::symmetric(11, 0)),
                    )
                    .show_inside(ui, |ui| self.stat_editor(context, ui));
                egui::CentralPanel::default()
                    .frame(egui::Frame::new())
                    .show_inside(ui, |ui| {
                        let width = ui.available_width();
                        self.stats_table(ui, width);
                    });
            });
        self.stop_confirmation(context);
        if self.show_details {
            let text = self.diagnostic_text();
            egui::Window::new("Detection details")
                .open(&mut self.show_details)
                .show(context, |ui| {
                    ui.horizontal(|ui| {
                        ui.strong("Detection details");
                        ui.add_enabled(false, Button::new("Manual value search"))
                            .on_disabled_hover_text(
                                "Manual value search has not been ported to this preview.",
                            );
                    });
                    ui.separator();
                    if ui.button("Copy details").clicked() {
                        context.copy_text(text.clone());
                    }
                    ui.monospace(text);
                });
        }
    }
}
