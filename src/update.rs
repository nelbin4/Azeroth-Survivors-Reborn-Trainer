use std::{
    env, fs,
    io::{self, Read, Write},
    os::windows::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command},
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use serde::Deserialize;
use sha2::{Digest, Sha256};
use url::Url;

use crate::{diagnostic_log::event, process};

pub const PROJECT: &str = "https://github.com/nelbin4/Azeroth-Survivors-Reborn-Trainer";
pub const ASSET_NAME: &str = "Azeroth.Survivor.Reborn.Trainer.exe";
const API: &str =
    "https://api.github.com/repos/nelbin4/Azeroth-Survivors-Reborn-Trainer/releases/latest";
const MAX_DOWNLOAD: u64 = 64 * 1024 * 1024;
const HELPER: &str = "reborn-update.exe";
const PAYLOAD: &str = "verified-update.exe";
const COMPLETE: &str = "complete";

#[derive(Deserialize)]
struct Release {
    tag_name: String,
    draft: bool,
    prerelease: bool,
    #[serde(default)]
    assets: Vec<ReleaseAsset>,
}

#[derive(Deserialize)]
struct ReleaseAsset {
    name: String,
    browser_download_url: String,
    digest: Option<String>,
    size: u64,
}

pub struct ReleaseStatus {
    pub tag: String,
    pub available: Option<Asset>,
}

#[derive(Clone)]
pub struct Asset {
    url: Url,
    digest: String,
    size: u64,
}

pub fn version(input: &str) -> Option<[u32; 3]> {
    let input = input.strip_prefix('v').unwrap_or(input);
    let mut fields = input.split('.');
    let mut parts = [0; 3];
    for part in &mut parts {
        let field = fields.next()?;
        if field.is_empty() || !field.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        *part = field.parse().ok()?;
    }
    (fields.next().is_none()).then_some(parts)
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(45))
        .redirects(0)
        .build()
}

pub fn check(current: &str) -> io::Result<ReleaseStatus> {
    event("update.check_started", format_args!("installed={current}"));
    let response = agent()
        .get(API)
        .set("User-Agent", "AzerothSurvivorRebornTrainer")
        .set("Accept", "application/vnd.github+json")
        .call()
        .map_err(io::Error::other)?;
    if response.status() != 200 {
        return Err(io::Error::other(
            "GitHub returned an unexpected release response.",
        ));
    }
    let mut body = Vec::new();
    response
        .into_reader()
        .take(2 * 1024 * 1024 + 1)
        .read_to_end(&mut body)?;
    if body.len() > 2 * 1024 * 1024 {
        return Err(io::Error::other("Release response is too large."));
    }
    let status = from_response(serde_json::from_slice(&body)?, current)?;
    event(
        "update.check_finished",
        format_args!(
            "latest={} available={}",
            status.tag,
            status.available.is_some()
        ),
    );
    Ok(status)
}

fn digest_valid(digest: &str) -> bool {
    digest.len() == 64
        && digest
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn from_response(release: Release, current: &str) -> io::Result<ReleaseStatus> {
    if release.draft || release.prerelease {
        return Err(io::Error::other("Expected a stable published release."));
    }
    let installed =
        version(current).ok_or_else(|| io::Error::other("Invalid installed version."))?;
    let stable =
        version(&release.tag_name).ok_or_else(|| io::Error::other("Invalid release version."))?;
    let available = if stable > installed {
        let entry = release
            .assets
            .into_iter()
            .find(|asset| asset.name == ASSET_NAME)
            .ok_or_else(|| io::Error::other("The latest release has no standalone EXE."))?;
        let expected = format!(
            "{PROJECT}/releases/download/{}/{ASSET_NAME}",
            release.tag_name
        );
        let digest = entry
            .digest
            .as_deref()
            .and_then(|s| s.strip_prefix("sha256:"))
            .filter(|digest| digest_valid(digest))
            .ok_or_else(|| io::Error::other("The release EXE has no valid SHA-256 digest."))?;
        if entry.browser_download_url != expected || !(1024..=MAX_DOWNLOAD).contains(&entry.size) {
            return Err(io::Error::other(
                "The release EXE has invalid download metadata.",
            ));
        }
        Some(Asset {
            url: Url::parse(&expected).map_err(io::Error::other)?,
            digest: digest.into(),
            size: entry.size,
        })
    } else {
        None
    };
    Ok(ReleaseStatus {
        tag: release.tag_name,
        available,
    })
}

fn trusted_download(url: &Url) -> bool {
    url.scheme() == "https"
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && url
            .host_str()
            .is_some_and(|host| host == "github.com" || host.ends_with(".githubusercontent.com"))
}

fn download(asset: &Asset, destination: &Path) -> io::Result<()> {
    let agent = agent();
    let mut url = asset.url.clone();
    for _ in 0..=5 {
        if !trusted_download(&url) {
            return Err(io::Error::other("Unexpected update download redirect."));
        }
        let response = agent
            .get(url.as_str())
            .set("User-Agent", "AzerothSurvivorRebornTrainer")
            .call()
            .map_err(io::Error::other)?;
        if matches!(response.status(), 301 | 302 | 303 | 307 | 308) {
            let next = response
                .header("Location")
                .ok_or_else(|| io::Error::other("Update redirect did not specify a location."))?;
            url = url.join(next).map_err(io::Error::other)?;
            continue;
        }
        if response.status() != 200 {
            return Err(io::Error::other(
                "Unexpected response while downloading the update.",
            ));
        }
        let mut input = response.into_reader().take(asset.size + 1);
        let mut output = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(destination)?;
        let mut hash = Sha256::new();
        let mut buffer = [0; 64 * 1024];
        let mut total = 0u64;
        loop {
            let read = input.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            total += read as u64;
            if total > asset.size {
                return Err(io::Error::other("Update exceeds its declared size."));
            }
            hash.update(&buffer[..read]);
            output.write_all(&buffer[..read])?;
        }
        if total != asset.size || format!("{:x}", hash.finalize()) != asset.digest {
            return Err(io::Error::other("Update SHA-256 verification failed."));
        }
        output.sync_all()?;
        return verify_file(destination, &asset.digest);
    }
    Err(io::Error::other("Too many update redirects."))
}

fn verify_file(path: &Path, digest: &str) -> io::Result<()> {
    if !digest_valid(digest) || !(1024..=MAX_DOWNLOAD).contains(&fs::metadata(path)?.len()) {
        return Err(io::Error::other("Invalid staged update."));
    }
    let file = fs::read(path)?;
    let pe = file
        .get(60..64)
        .map(|raw| u32::from_le_bytes(raw.try_into().unwrap()) as usize);
    if file.get(..2) != Some(b"MZ")
        || pe.and_then(|pe| file.get(pe..pe + 6)) != Some(b"PE\0\0\x64\x86")
        || format!("{:x}", Sha256::digest(&file)) != digest
    {
        return Err(io::Error::other(
            "Staged update is not the verified Windows x64 EXE.",
        ));
    }
    Ok(())
}

fn staging(target: &Path) -> io::Result<PathBuf> {
    let root = target
        .parent()
        .ok_or_else(|| io::Error::other("No installation folder."))?
        .join(".updates");
    fs::create_dir_all(&root)?;
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(io::Error::other)?
        .as_nanos();
    let stage = root.join(format!("stage-{}-{stamp}", std::process::id()));
    fs::create_dir(&stage)?;
    Ok(stage)
}

fn launch_helper(target: &Path, stage: &Path, digest: &str, parent_pid: u32) -> io::Result<Child> {
    let helper = stage.join(HELPER);
    fs::copy(env::current_exe()?, &helper)?;
    Command::new(helper)
        .arg("--install-update")
        .arg(target)
        .arg(digest)
        .arg(parent_pid.to_string())
        .arg(process::current_creation_time()?.to_string())
        .creation_flags(0x08000000)
        .spawn()
}

pub fn stage_and_launch(asset: &Asset) -> io::Result<()> {
    let target = env::current_exe()?.canonicalize()?;
    let stage = staging(&target)?;
    event(
        "update.download_started",
        format_args!("bytes={}", asset.size),
    );
    let result = download(asset, &stage.join(PAYLOAD)).and_then(|()| {
        launch_helper(&target, &stage, &asset.digest, std::process::id()).map(|_| ())
    });
    if result.is_err() {
        discard_stage(&stage);
    }
    result
}

fn discard_stage(stage: &Path) {
    for file in [PAYLOAD, COMPLETE, HELPER] {
        let _ = fs::remove_file(stage.join(file));
    }
    let _ = fs::remove_dir(stage);
}

pub fn cleanup() {
    let Ok(target) = env::current_exe() else {
        return;
    };
    let Some(parent) = target.parent() else {
        return;
    };
    let Ok(entries) = fs::read_dir(parent.join(".updates")) else {
        return;
    };
    for entry in entries.flatten() {
        if entry.file_type().is_ok_and(|kind| kind.is_dir())
            && entry.file_name().to_string_lossy().starts_with("stage-")
            && entry.path().join(COMPLETE).is_file()
        {
            discard_stage(&entry.path());
        }
    }
}

fn reopen(target: &Path) -> io::Result<Child> {
    let mut child = Command::new(target);
    child.current_dir(target.parent().expect("installer target has parent"));
    if env::var_os("REBORN_UPDATE_SMOKE").is_some() {
        child.arg("--smoke-test");
    }
    child.spawn()
}

fn replace_and_reopen(target: &Path, payload: &Path) -> io::Result<()> {
    let backup = target.with_extension("previous.exe");
    fs::rename(target, &backup)?;
    let result = (|| -> io::Result<()> {
        fs::rename(payload, target)?;
        let mut child = reopen(target)?;
        for _ in 0..20 {
            if let Some(status) = child.try_wait()? {
                if !status.success() {
                    return Err(io::Error::other("The updated trainer failed to start."));
                }
                break;
            }
            thread::sleep(Duration::from_millis(250));
        }
        Ok(())
    })();
    if result.is_err() {
        fs::rename(&backup, target)?;
    }
    result
}

pub fn install(target: &Path, digest: &str, parent_pid: u32, created: u64) -> io::Result<()> {
    let target = target.canonicalize()?;
    let helper = env::current_exe()?.canonicalize()?;
    let stage = helper.parent().expect("installer helper has parent");
    let root = target.parent().expect("canonical path has parent");
    if helper.file_name().is_none_or(|name| name != HELPER)
        || stage.parent() != Some(root.join(".updates").as_path())
        || stage
            .file_name()
            .is_none_or(|name| !name.to_string_lossy().starts_with("stage-"))
    {
        return Err(io::Error::other(
            "Installer stage does not match the target folder.",
        ));
    }
    process::wait_for_update_parent(parent_pid, created, &target)?;
    let payload = stage.join(PAYLOAD);
    let result = verify_file(&payload, digest).and_then(|()| replace_and_reopen(&target, &payload));
    match &result {
        Ok(()) => event("update.installed", format_args!("sha256={digest}")),
        Err(error) => {
            event("update.install_failed", format_args!("{error}"));
            if target.is_file() {
                let _ = reopen(&target);
            }
        }
    }
    let _ = fs::write(stage.join(COMPLETE), b"");
    result
}

pub fn smoke_install() -> io::Result<()> {
    let root = env::temp_dir().join(format!("reborn-update-smoke-{}", std::process::id()));
    fs::create_dir(&root)?;
    let target = root.join(ASSET_NAME);
    let original = fs::read(env::current_exe()?)?;
    fs::write(&target, &original)?;
    let stage = staging(&target)?;
    let mut updated = original.clone();
    updated.push(0);
    fs::write(stage.join(PAYLOAD), &updated)?;
    let digest = format!("{:x}", Sha256::digest(&updated));
    let helper = stage.join(HELPER);
    fs::copy(env::current_exe()?, &helper)?;
    let status = Command::new(&helper)
        .arg("--install-update")
        .arg(&target)
        .arg(&digest)
        .args(["0", "0"])
        .env("REBORN_UPDATE_SMOKE", "1")
        .status()?;
    if !status.success()
        || fs::read(&target)? != updated
        || fs::read(target.with_extension("previous.exe"))? != original
    {
        return Err(io::Error::other(
            "Standalone self-update smoke test failed.",
        ));
    }
    discard_stage(&stage);
    fs::remove_file(target.with_extension("previous.exe"))?;
    fs::remove_file(target)?;
    fs::remove_dir(root.join(".updates"))?;
    fs::remove_dir(root)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stable(tag: &str) -> Release {
        Release {
            tag_name: tag.into(),
            draft: false,
            prerelease: false,
            assets: vec![],
        }
    }

    #[test]
    fn recognizes_stable_versions_and_validates_installable_asset() {
        assert!(
            from_response(stable("v0.9.19"), "1.0.0")
                .unwrap()
                .available
                .is_none()
        );
        assert!(
            from_response(stable("v1.0.0"), "1.0.0")
                .unwrap()
                .available
                .is_none()
        );
        let mut release = stable("v1.1.0");
        release.assets.push(ReleaseAsset {
            name: ASSET_NAME.into(),
            browser_download_url: format!("{PROJECT}/releases/download/v1.1.0/{ASSET_NAME}"),
            digest: Some(format!("sha256:{}", "a".repeat(64))),
            size: 4096,
        });
        assert!(from_response(release, "1.0.9").unwrap().available.is_some());
        assert!(from_response(stable("v1.1.0"), "1.0.9").is_err());
        assert!(version("v1.1.0-alpha").is_none());
    }

    #[test]
    fn only_accepts_https_github_download_hosts() {
        for address in [
            "https://github.com/releases",
            "https://release-assets.githubusercontent.com/x",
        ] {
            assert!(trusted_download(&Url::parse(address).unwrap()));
        }
        for address in [
            "http://github.com/releases",
            "https://github.com.example.org/x",
            "https://github.com@example.org/x",
            "https://github.com:8443/x",
        ] {
            assert!(!trusted_download(&Url::parse(address).unwrap()));
        }
    }
}
