use std::{io, time::Duration};

use serde::Deserialize;

pub const PROJECT: &str = "https://github.com/nelbin4/Azeroth-Survivors-Reborn-Trainer";
const API: &str =
    "https://api.github.com/repos/nelbin4/Azeroth-Survivors-Reborn-Trainer/releases/latest";

#[derive(Deserialize)]
struct Release {
    tag_name: String,
    draft: bool,
    prerelease: bool,
}

pub struct ReleaseStatus {
    pub tag: String,
    pub available: bool,
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

pub fn check(current: &str) -> io::Result<ReleaseStatus> {
    let response = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(15))
        .redirects(0)
        .build()
        .get(API)
        .set("User-Agent", "AzerothSurvivorRebornTrainer")
        .set("Accept", "application/vnd.github+json")
        .call()
        .map_err(io::Error::other)?;
    use std::io::Read;
    let mut body = Vec::new();
    response
        .into_reader()
        .take(2 * 1024 * 1024 + 1)
        .read_to_end(&mut body)?;
    if body.len() > 2 * 1024 * 1024 {
        return Err(io::Error::other("Release response is too large."));
    }
    from_response(serde_json::from_slice(&body)?, current)
}

fn from_response(release: Release, current: &str) -> io::Result<ReleaseStatus> {
    if release.draft || release.prerelease {
        return Err(io::Error::other("Expected a stable published release."));
    }
    let (current_core, prerelease) = current.split_once('-').unwrap_or((current, ""));
    let installed =
        version(current_core).ok_or_else(|| io::Error::other("Invalid installed version."))?;
    let stable =
        version(&release.tag_name).ok_or_else(|| io::Error::other("Invalid release version."))?;
    Ok(ReleaseStatus {
        tag: release.tag_name,
        available: stable > installed || (stable == installed && !prerelease.is_empty()),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stable(tag: &str) -> Release {
        Release {
            tag_name: tag.into(),
            draft: false,
            prerelease: false,
        }
    }

    #[test]
    fn existing_release_does_not_replace_rust_preview_with_python() {
        assert!(
            !from_response(stable("v0.9.19"), "1.0.0-alpha.1")
                .unwrap()
                .available
        );
        assert!(
            from_response(stable("v1.0.0"), "1.0.0-alpha.1")
                .unwrap()
                .available
        );
    }

    #[test]
    fn rollover_advances_version() {
        assert!(from_response(stable("v1.1.0"), "1.0.9").unwrap().available);
        assert!(version("v1.1.0-alpha").is_none());
    }
}
