#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

mod app;
mod hunt;
mod process;
mod settings;
mod update;

fn main() -> eframe::Result {
    match std::env::args().nth(1).as_deref() {
        Some("--smoke-test") => {
            app::icon().expect("embedded trainer icon should decode");
            let core = env!("CARGO_PKG_VERSION").split('-').next().unwrap();
            update::version(core).expect("trainer package version should be valid");
            return Ok(());
        }
        _ => {}
    }
    let mut viewport = eframe::egui::ViewportBuilder::default()
        .with_inner_size([980.0, 780.0])
        .with_min_inner_size([780.0, 580.0]);
    if let Ok(icon) = app::icon() {
        viewport = viewport.with_icon(icon);
    }
    eframe::run_native(
        "Azeroth Survivor Reborn Trainer",
        eframe::NativeOptions {
            viewport,
            ..Default::default()
        },
        Box::new(|context| Ok(Box::new(app::TrainerApp::new(context)))),
    )
}
