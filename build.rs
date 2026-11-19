fn main() {
    println!("cargo:rerun-if-changed=assets/trainer-icon.ico");
    if std::env::var_os("CARGO_CFG_WINDOWS").is_some() {
        winresource::WindowsResource::new()
            .set_icon("assets/trainer-icon.ico")
            .compile()
            .expect("compile trainer icon");
    }
}
