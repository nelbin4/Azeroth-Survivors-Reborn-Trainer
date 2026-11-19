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
    diagnostic_log::event,
    hunt::{self, Snapshot},
    process::{self, Games, Session},
    session_role::RoleProof,
    settings::Settings,
    stats_ui::{STATS, Stat},
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

struct Apply {
    stop: Arc<AtomicBool>,
    result: Receiver<io::Result<hunt::ApplyOutcome>>,
    automatic: bool,
}

impl Drop for Apply {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
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
    apply: Option<Apply>,
    enabled: [bool; 17],
    targets: [u32; 17],
    target_entry: String,
    auto_active: bool,
    next_auto: Instant,
    role_lease: Option<RoleProof>,
    field_issues: std::collections::BTreeMap<&'static str, String>,
    snapshot: Option<Snapshot>,
    status: String,
    error: Option<String>,
    show_details: bool,
    selected: usize,
    logo: Option<egui::TextureHandle>,
    update_result: Option<Receiver<io::Result<update::ReleaseStatus>>>,
    update_status: String,
    update_asset: Option<update::Asset>,
    update_error: Option<String>,
    manual_update_check: bool,
    using_latest_until: Option<Instant>,
    install_result: Option<Receiver<io::Result<()>>>,
}

fn checked_targets(enabled: &[bool; 17], targets: &[u32; 17]) -> hunt::Targets {
    STATS
        .iter()
        .zip(enabled)
        .zip(targets)
        .filter_map(|((stat, enabled), target)| enabled.then_some((stat.key, *target)))
        .collect()
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
            Ok(settings) => {
                event(
                    "settings.loaded",
                    format_args!("saved_game_path={}", settings.game_path().is_some()),
                );
                (settings, None)
            }
            Err(error) => {
                event("settings.load_failed", format_args!("{error}"));
                (Settings::default(), Some(format!("Settings: {error}")))
            }
        };
        let prefs = settings.stat_preferences();
        let enabled = std::array::from_fn(|index| STATS[index].checked(prefs));
        let targets = std::array::from_fn(|index| STATS[index].saved_target(prefs));
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
            apply: None,
            enabled,
            targets,
            target_entry: targets[0].to_string(),
            auto_active: false,
            next_auto: Instant::now(),
            role_lease: None,
            field_issues: Default::default(),
            snapshot: None,
            status: "Start Reborn to find a character.".into(),
            show_details: false,
            selected: 0,
            logo,
            update_result: Some(task(&context.egui_ctx, || update::check(VERSION))),
            update_status: "Checking…".into(),
            update_asset: None,
            update_error: None,
            manual_update_check: false,
            using_latest_until: None,
            install_result: None,
        }
    }

    fn set_session(&mut self, session: Option<Session>) {
        if self.session != session {
            if let Some(old) = &self.session {
                event("process.detached", format_args!("game_pid={}", old.pid));
            }
            if let Some(new) = &session {
                event("process.attached", format_args!("game_pid={}", new.pid));
            }
            if self.scan.is_some() {
                event("hunt.cancelled", format_args!("reason=connection_changed"));
            }
            self.session = session;
            self.scan = None;
            self.stop_auto();
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
                    if self.running != games.running() {
                        event(
                            "process.presence",
                            format_args!("game_running={}", games.running()),
                        );
                    }
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
                    let message = format!("Game detection: {error}");
                    if self.error.as_deref() != Some(message.as_str()) {
                        event("process.discovery_failed", format_args!("{error}"));
                    }
                    self.error = Some(message);
                }
            }
        }
        if let Some(result) = finished(&mut self.launch_result) {
            match result {
                Ok(session) => {
                    event(
                        "process.launch_finished",
                        format_args!("game_pid={}", session.pid),
                    );
                    self.running = true;
                    self.set_session(Some(session));
                }
                Err(error) => {
                    event("process.launch_failed", format_args!("{error}"));
                    self.error = Some(error.to_string());
                }
            }
        }
        if let Some(result) = finished(&mut self.stop_result) {
            if let Err(error) = result {
                event("process.stop_failed", format_args!("{error}"));
                self.error = Some(format!("Could not stop Reborn: {error}"));
            } else {
                event(
                    "process.stop_sent",
                    format_args!("waiting_for_process_exit"),
                );
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
                        event(
                            "hunt.finished",
                            format_args!(
                                "game_pid={} build={} character_found={} elapsed_ms={}",
                                search.session.pid,
                                snapshot.build,
                                snapshot.character.is_some(),
                                search.started.elapsed().as_millis()
                            ),
                        );
                        self.status = if snapshot.stats.is_some() {
                            self.auto_active = self.enabled.contains(&true);
                            self.next_auto = Instant::now();
                            if self.auto_active {
                                "Checking session before applying checked stats.".into()
                            } else {
                                "Character verified. Choose a stat to apply.".into()
                            }
                        } else if snapshot.character.is_some() {
                            "Character found. This build is read-only.".into()
                        } else {
                            "No live character verified. Start a run and retry.".into()
                        };
                        self.snapshot = Some(snapshot);
                    }
                    Err(error) => {
                        event(
                            "hunt.failed",
                            format_args!(
                                "game_pid={} elapsed_ms={} error={error}",
                                search.session.pid,
                                search.started.elapsed().as_millis()
                            ),
                        );
                        self.status = error.to_string();
                    }
                }
            }
        }
        if let Some(outcome) = self
            .apply
            .as_ref()
            .and_then(|apply| match apply.result.try_recv() {
                Ok(result) => Some(result),
                Err(TryRecvError::Disconnected) => {
                    Some(Err(io::Error::other("Apply worker stopped unexpectedly.")))
                }
                Err(TryRecvError::Empty) => None,
            })
        {
            let automatic = self.apply.take().unwrap().automatic;
            match outcome {
                Ok(result) => {
                    if let Some(snapshot) = &mut self.snapshot {
                        snapshot.values = result.values;
                    }
                    self.role_lease = Some(result.lease);
                    self.field_issues = result.skipped;
                    let count = result.applied;
                    let warning = self.field_issues.len();
                    self.status = format!(
                        "{} · {count} applied{}",
                        if automatic { "Auto Freeze" } else { "Apply" },
                        if warning > 0 {
                            format!(" · {warning} waiting")
                        } else {
                            String::new()
                        }
                    );
                    event(
                        "apply.batch_finished",
                        format_args!(
                            "role={:?} automatic={automatic} applied={count} waiting={warning}",
                            result.role
                        ),
                    );
                }
                Err(error) => {
                    event("apply.failed", format_args!("{error}"));
                    self.stop_auto();
                    self.status = error.to_string();
                }
            }
            self.next_auto = Instant::now() + Duration::from_secs(1);
        }
        if self.auto_active && self.apply.is_none() && Instant::now() >= self.next_auto {
            let targets = checked_targets(&self.enabled, &self.targets);
            if targets.is_empty() {
                self.stop_auto();
                self.status = "No checked stats.".into();
            } else {
                self.start_apply(context, targets, true);
            }
        }
        if let Some(result) = finished(&mut self.update_result) {
            let manual = std::mem::take(&mut self.manual_update_check);
            match result {
                Ok(status) => {
                    self.update_asset = status.available;
                    self.update_status = if self.update_asset.is_some() {
                        "Update Available"
                    } else {
                        "Latest"
                    }
                    .into();
                    if manual && self.update_asset.is_none() {
                        self.using_latest_until = Some(Instant::now() + Duration::from_secs(3));
                    }
                }
                Err(error) => {
                    event("update.check_failed", format_args!("{error}"));
                    self.update_status = "Update check failed".into();
                    self.update_error = Some(error.to_string());
                }
            }
        }
        if let Some(result) = finished(&mut self.install_result) {
            match result {
                Ok(()) => {
                    self.stop_auto();
                    self.scan = None;
                    self.update_status = "Restarting…".into();
                    context.send_viewport_cmd(egui::ViewportCommand::Close);
                }
                Err(error) => {
                    event("update.download_failed", format_args!("{error}"));
                    self.update_status = "Update failed".into();
                    self.update_error = Some(error.to_string());
                }
            }
        }
        context.request_repaint_after(Duration::from_millis(250));
    }

    fn start_find(&mut self, context: &egui::Context) {
        if let Some(session) = self.session.clone() {
            if !self.commit_target() {
                return;
            }
            self.stop_auto();
            self.snapshot = None;
            self.error = None;
            self.status = "Finding the selected character…".into();
            event("hunt.requested", format_args!("game_pid={}", session.pid));
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

    fn stop_auto(&mut self) {
        self.auto_active = false;
        self.apply = None;
        self.role_lease = None;
        self.field_issues.clear();
    }

    fn save_preferences(&mut self) {
        let enabled: Vec<_> = STATS
            .iter()
            .enumerate()
            .filter_map(|(index, stat)| self.enabled[index].then_some(stat.key))
            .collect();
        let targets: std::collections::BTreeMap<_, _> = STATS
            .iter()
            .enumerate()
            .map(|(index, stat)| (stat.key, self.targets[index]))
            .collect();
        if let Err(error) = self
            .settings
            .set_stat_preferences(serde_json::json!({"enabled": enabled, "targets": targets}))
        {
            self.error = Some(format!("Could not save stat preferences: {error}"));
            event("settings.save_failed", format_args!("{error}"));
        }
    }

    fn commit_target(&mut self) -> bool {
        let stat = &STATS[self.selected];
        if stat.key == "health" {
            return true;
        }
        let value = self
            .target_entry
            .trim()
            .parse::<u32>()
            .ok()
            .filter(|value| stat.valid_target(*value));
        let Some(value) = value else {
            self.status = format!(
                "{} target must be between {} and {}.",
                stat.label, stat.min, stat.max
            );
            return false;
        };
        if value != self.targets[self.selected] {
            self.targets[self.selected] = value;
            self.save_preferences();
        }
        true
    }

    fn start_apply(&mut self, context: &egui::Context, targets: hunt::Targets, automatic: bool) {
        let Some(proof) = self.snapshot.as_ref().and_then(|s| s.stats.clone()) else {
            self.stop_auto();
            return;
        };
        let stop = Arc::new(AtomicBool::new(false));
        let worker_stop = stop.clone();
        let lease = self.role_lease;
        if lease.is_none() {
            self.status = "Checking whether the session is solo or hosted…".into();
        }
        let result = task(context, move || {
            hunt::apply_stats(&proof, &targets, lease, &worker_stop)
        });
        self.apply = Some(Apply {
            stop,
            result,
            automatic,
        });
    }

    fn locate(&mut self) -> Option<PathBuf> {
        event("settings.locate_game", format_args!("dialog_opened"));
        let Some(path) = rfd::FileDialog::new()
            .add_filter("Windows executable", &["exe"])
            .pick_file()
        else {
            event("settings.locate_game_cancelled", format_args!(""));
            return None;
        };
        if !path
            .file_name()
            .is_some_and(|name| name.eq_ignore_ascii_case(process::GAME_EXE))
        {
            event(
                "settings.game_path_rejected",
                format_args!("unexpected_executable_name"),
            );
            self.error = Some("Choose AzerothSurvivorsGame.exe.".into());
            return None;
        }
        if let Err(error) = self.settings.set_game_path(&path) {
            event("settings.save_failed", format_args!("{error}"));
            self.error = Some(format!("Could not save game path: {error}"));
        } else {
            event("settings.saved", format_args!("changed=game_executable"));
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
                event("process.launch_requested", format_args!(""));
                self.launch_result = Some(task(context, move || process::launch(&path)));
            }
            if ui
                .add_enabled(
                    self.session.is_some() && self.stop_result.is_none() && self.apply.is_none(),
                    Button::new("Stop Game"),
                )
                .clicked()
            {
                self.confirm_stop = self.session.clone();
            }
            if ui.button("Locate exe file").clicked() {
                self.locate();
            }
            if ui.button("Advanced").clicked() {
                event("diagnostics.opened", format_args!(""));
                self.show_details = true;
            }
            ui.allocate_ui_with_layout(
                egui::vec2(ui.available_width(), 0.0),
                egui::Layout::top_down(egui::Align::RIGHT),
                |ui| {
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        let latest = self
                            .using_latest_until
                            .is_some_and(|until| Instant::now() < until);
                        let idle = self.update_result.is_none()
                            && self.install_result.is_none()
                            && !latest;
                        let label = if self.install_result.is_some() {
                            "Downloading…"
                        } else if self.update_result.is_some() {
                            "Checking…"
                        } else if latest {
                            "Using Latest"
                        } else if self.update_asset.is_some() {
                            "Download & Install"
                        } else {
                            "Check updates"
                        };
                        if ui.add_enabled(idle, Button::new(label)).clicked() {
                            self.update_error = None;
                            if let Some(asset) = self.update_asset.clone() {
                                self.update_status = "Downloading update…".into();
                                self.install_result =
                                    Some(task(context, move || update::stage_and_launch(&asset)));
                            } else {
                                self.update_status = "Checking…".into();
                                self.manual_update_check = true;
                                self.update_result = Some(task(context, || update::check(VERSION)));
                            }
                        }
                        if ui.button("Github Page").clicked() {
                            context.open_url(egui::OpenUrl::new_tab(update::PROJECT));
                        }
                    });
                    let mut status =
                        RichText::new(format!("{VERSION} - {}", self.update_status)).small();
                    if self.update_status == "Update Available" {
                        let seconds = context.input(|input| input.time);
                        let glow = ((seconds * std::f64::consts::TAU / 5.0).cos() + 1.0) * 0.5;
                        status = status
                            .color(Color32::from_rgb(
                                255,
                                200 + (45.0 * glow) as u8,
                                35 + (105.0 * glow) as u8,
                            ))
                            .background_color(Color32::from_rgba_unmultiplied(
                                255,
                                215,
                                65,
                                (28.0 * glow) as u8,
                            ));
                        context.request_repaint_after(Duration::from_millis(50));
                    } else {
                        status = status.color(MINT);
                    }
                    let response = ui.label(status);
                    if let Some(error) = &self.update_error {
                        response.on_hover_text(error);
                    }
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
            event(
                "process.stop_cancelled",
                format_args!("game_pid={}", session.pid),
            );
            self.confirm_stop = None;
        } else if stop {
            event(
                "process.stop_requested",
                format_args!("game_pid={}", session.pid),
            );
            self.confirm_stop = None;
            self.scan = None;
            self.stop_auto();
            self.snapshot = None;
            self.stop_result = Some(task(context, move || process::stop(&session)));
        }
    }

    fn stat_display(&self, stat: &Stat) -> Option<String> {
        let snapshot = self.snapshot.as_ref()?;
        if let Some(value) = snapshot.values.get(stat.key) {
            return Some(stat.display(*value));
        }
        let character = snapshot.character.as_ref()?;
        let index = match stat.key {
            "health" => 0,
            "max_health" => 1,
            _ => return None,
        };
        Some(format!(
            "≈ {}",
            stat.display(character.health_candidate[index] as u64)
        ))
    }

    fn stats_table(&mut self, ui: &mut egui::Ui, width: f32) {
        let stat_width = (width - 415.0).max(125.0);
        let columns = [
            ("Enabled", 76.0, 68.0),
            ("Stat", stat_width, 100.0),
            ("Current", 88.0, 62.0),
            ("Target", 102.0, 59.0),
            ("Status", 117.0, 68.0),
        ];
        ui.spacing_mut().item_spacing.y = 2.0;
        let mut selected = self.selected;
        let mut preferences_changed = false;
        let mut table = TableBuilder::new(ui)
            .striped(true)
            .resizable(true)
            .sense(egui::Sense::click())
            .auto_shrink([false, false]);
        for (_, width, minimum) in columns {
            table = table.column(Column::initial(width).at_least(minimum).clip(true));
        }
        table
            .header(31.0, |mut header| {
                for (title, _, _) in columns {
                    header.col(|ui| {
                        ui.strong(title);
                    });
                }
            })
            .body(|mut body| {
                for (index, stat) in STATS.iter().enumerate() {
                    let target = stat.target(self.targets[index]);
                    let mut checked = self.enabled[index];
                    let current = self.stat_display(stat);
                    let verified = self
                        .snapshot
                        .as_ref()
                        .is_some_and(|s| s.values.contains_key(stat.key));
                    let status = if self.field_issues.contains_key(stat.key) {
                        "Waiting"
                    } else if verified && checked && self.auto_active && self.role_lease.is_some() {
                        "Frozen"
                    } else if verified {
                        "Verified"
                    } else if current.is_some() {
                        "Diagnostic"
                    } else if self.scan.is_some() {
                        "Searching"
                    } else {
                        "Not found"
                    };
                    body.row(29.0, |mut row| {
                        row.set_selected(index == self.selected);
                        row.col(|ui| {
                            ui.allocate_ui_with_layout(
                                egui::vec2(ui.available_width(), 22.0),
                                egui::Layout::top_down(egui::Align::Center),
                                |ui| {
                                    let response = ui.add_enabled(
                                        self.apply.is_none(),
                                        egui::Checkbox::without_text(&mut checked),
                                    );
                                    if response.changed() {
                                        self.enabled[index] = checked;
                                        preferences_changed = true;
                                    }
                                },
                            );
                        });
                        row.col(|ui| {
                            ui.label(stat.label);
                        });
                        row.col(|ui| {
                            ui.label(current.as_deref().unwrap_or("—"));
                        });
                        row.col(|ui| {
                            ui.label(target);
                        });
                        row.col(|ui| {
                            ui.label(RichText::new(status).color(MUTED));
                        });
                        if row.response().clicked() {
                            if selected != index {
                                event("stats.selected", format_args!("stat={}", stat.key));
                            }
                            selected = index;
                        }
                    });
                }
            });
        if self.selected != selected && self.commit_target() {
            self.selected = selected;
            self.target_entry = self.targets[selected].to_string();
        }
        if preferences_changed {
            self.save_preferences();
        }
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
            .add_enabled(
                self.apply.is_none() && (finding || self.session.is_some()),
                find,
            )
            .clicked()
        {
            if finding {
                event("hunt.cancelled", format_args!("reason=user"));
                self.scan = None;
                self.status = "Search stopped.".into();
            } else {
                self.start_find(context);
            }
        }
        let can_freeze = self.snapshot.as_ref().is_some_and(|s| s.stats.is_some());
        let freeze_label = if self.auto_active {
            "Stop Freeze"
        } else {
            "Resume Freeze"
        };
        if ui
            .add_enabled(
                can_freeze,
                Button::new(freeze_label).min_size(egui::vec2(width, 32.0)),
            )
            .clicked()
        {
            if self.auto_active {
                self.stop_auto();
                self.status = "Freezes stopped.".into();
            } else if self.commit_target() {
                self.auto_active = true;
                self.next_auto = Instant::now();
            }
        }
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
        let editable = self.apply.is_none() && stat.key != "health";
        let reply = ui.add_enabled(
            editable,
            egui::TextEdit::singleline(&mut self.target_entry).desired_width(width - 10.0),
        );
        if reply.lost_focus() {
            self.commit_target();
        }
        let can_apply = can_freeze
            && self.apply.is_none()
            && self.stop_result.is_none()
            && self.session.is_some()
            && self.scan.is_none();
        ui.horizontal(|ui| {
            if ui
                .add_enabled(can_apply, Button::new("Apply").fill(ACCENT))
                .clicked()
                && self.commit_target()
            {
                self.start_apply(
                    context,
                    [(stat.key, self.targets[self.selected])].into(),
                    false,
                );
            }
            if ui
                .add_enabled(editable, Button::new("Use maximum"))
                .clicked()
            {
                self.target_entry = stat.max.to_string();
                self.commit_target();
            }
        });
        let hint = if let Some(error) = self.field_issues.get(stat.key) {
            error.as_str()
        } else if stat.key == "health" {
            "Current health follows verified maximum health."
        } else {
            "Find applies and holds checked targets."
        };
        ui.label(RichText::new(hint).color(MUTED));
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
        let access = if self.snapshot.as_ref().is_some_and(|s| s.stats.is_some()) {
            "Verified mapping · Solo or host verification required"
        } else {
            "Read-only"
        };
        format!(
            "{}\nGame process: {pid}\nBuild: {build}\nCharacter: {character}\nAccess: {access}",
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
                        event("diagnostics.copied", format_args!(""));
                        context.copy_text(text.clone());
                    }
                    ui.monospace(text);
                });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn automatic_apply_uses_only_checked_custom_targets() {
        let mut enabled = [false; 17];
        enabled[0] = true;
        enabled[2] = true;
        let mut targets = std::array::from_fn(|index| STATS[index].max);
        targets[0] = 222;
        targets[2] = 127;
        assert_eq!(
            checked_targets(&enabled, &targets),
            [("damage", 222), ("crit_damage", 127)].into()
        );
    }
}
