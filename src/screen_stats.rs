// Window capture and OCR calls adapted from the MIT-licensed Azeroth Survivors Trainer.\nuse image::{RgbaImage, imageops::FilterType};
use std::{io, ptr::null_mut, slice};
use windows::{
    Graphics::Imaging::{BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap},
    Media::Ocr::{OcrEngine, OcrResult},
    Storage::Streams::DataWriter,
    Win32::System::WinRT::{RO_INIT_MULTITHREADED, RoInitialize, RoUninitialize},
};
use windows_sys::Win32::{
    Foundation::{HWND, LPARAM, RECT},
    Graphics::Gdi::{
        BI_RGB, BITMAPINFO, CreateCompatibleDC, CreateDIBSection, DIB_RGB_COLORS, DeleteDC,
        DeleteObject, SelectObject,
    },
    Storage::Xps::PrintWindow,
    UI::WindowsAndMessaging::{
        EnumWindows, GetClientRect, GetWindowTextW, GetWindowThreadProcessId, IsIconic,
        IsWindowVisible,
    },
};

struct Frame {
    pixels: Vec<u8>,
    width: usize,
    height: usize,
}

struct Apartment;

impl Apartment {
    fn enter() -> windows::core::Result<Self> {
        unsafe { RoInitialize(RO_INIT_MULTITHREADED) }?;
        Ok(Self)
    }
}

impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe { RoUninitialize() };
    }
}

pub fn read(pid: u32) -> io::Result<String> {
    let frame = capture(pid)?;
    let _apartment = Apartment::enter().map_err(io::Error::other)?;
    let engine = OcrEngine::TryCreateFromUserProfileLanguages().map_err(io::Error::other)?;
    let image = RgbaImage::from_raw(frame.width as u32, frame.height as u32, frame.pixels)
        .ok_or_else(|| io::Error::other("Invalid game capture dimensions."))?;
    let (width, height) = image.dimensions();
    // Reborn puts live Stats to the right of the Character panel.
    let x = width * 63 / 100;
    let y = height * 13 / 100;
    let w = width * 21 / 100;
    let h = height * 54 / 100;
    let crop = image::imageops::crop_imm(&image, x, y, w, h);
    let limit = OcrEngine::MaxImageDimension().map_err(io::Error::other)?;
    let scale = (limit / w.max(h)).clamp(1, 3);
    let crop = image::imageops::resize(&*crop, w * scale, h * scale, FilterType::Lanczos3);
    let result = recognize(
        &engine,
        crop.as_raw(),
        crop.width() as usize,
        crop.height() as usize,
    )
    .map_err(io::Error::other)?;
    let rows = stat_rows(&result).map_err(io::Error::other)?;
    panel_text(&rows).ok_or_else(|| {
        io::Error::other("Stats panel was not recognized. Open Character in Reborn and try again.")
    })
}

fn panel_text(rows: &str) -> Option<String> {
    let mut lines = rows
        .lines()
        .map(str::trim)
        .skip_while(|line| !line.eq_ignore_ascii_case("Stats"));
    lines.next()?;
    let mut section = "";
    let mut readings = Vec::new();
    for line in lines {
        if ["RUN", "OFFENSE", "DEFENSE", "UTILITY"]
            .iter()
            .any(|heading| heading.eq_ignore_ascii_case(line))
        {
            section = line;
            readings.push(line.to_owned());
            continue;
        }
        if section.is_empty() {
            continue;
        }
        let labels: &[&str] = if section.eq_ignore_ascii_case("RUN") {
            &["Time", "Experience", "Kills", "Gold", "Rerolls"]
        } else if section.eq_ignore_ascii_case("OFFENSE") {
            &[
                "Damage",
                "Critical strike",
                "Critical damage",
                "Cooldown reduction",
                "Area of effect",
                "Extra targets",
                "Echo",
            ]
        } else if section.eq_ignore_ascii_case("DEFENSE") {
            &["Health per second", "Health", "Damage reduction"]
        } else {
            &["Movement speed", "Pickup range", "Gold find", "Luck"]
        };
        if labels.iter().any(|label| {
            line.get(..label.len())
                .is_some_and(|prefix| prefix.eq_ignore_ascii_case(label))
                && line.get(label.len()..).is_some_and(|value| {
                    value.starts_with(' ') && value.bytes().any(|byte| byte.is_ascii_digit())
                })
        }) {
            readings.push(line.to_owned());
        }
    }
    (readings
        .iter()
        .any(|line| line.eq_ignore_ascii_case("OFFENSE"))
        && readings
            .iter()
            .any(|line| line.eq_ignore_ascii_case("DEFENSE"))
        && readings
            .iter()
            .any(|line| line.bytes().any(|byte| byte.is_ascii_digit())))
    .then(|| readings.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ignores_gear_bonuses_and_requires_the_stats_panel() {
        let text = "Equipment\nDamage +21%\nStats\nRUN\nGold 2\nOFFENSE\nDamage +27%\nDEFENSE\nHealth 72 / 353\nUTILITY\nLuck +35%";
        let report = panel_text(text).unwrap();
        assert!(report.contains("Damage +27%"));
        assert!(!report.contains("Damage +21%"));
        assert!(report.contains("Health 72 / 353"));
        assert!(panel_text("Equipment\nDamage +21%").is_none());
    }
}

fn recognize(
    engine: &OcrEngine,
    pixels: &[u8],
    width: usize,
    height: usize,
) -> windows::core::Result<OcrResult> {
    let writer = DataWriter::new()?;
    writer.WriteBytes(pixels)?;
    let buffer = writer.DetachBuffer()?;
    let bitmap = SoftwareBitmap::CreateWithAlpha(
        BitmapPixelFormat::Bgra8,
        width as i32,
        height as i32,
        BitmapAlphaMode::Ignore,
    )?;
    bitmap.CopyFromBuffer(&buffer)?;
    engine.RecognizeAsync(&bitmap)?.get()
}

fn stat_rows(result: &OcrResult) -> windows::core::Result<String> {
    struct Word {
        x: f32,
        center: f32,
        height: f32,
        text: String,
    }
    let lines = result.Lines()?;
    let mut words = Vec::new();
    for index in 0..lines.Size()? {
        let line = lines.GetAt(index)?.Words()?;
        for index in 0..line.Size()? {
            let word = line.GetAt(index)?;
            let bounds = word.BoundingRect()?;
            words.push(Word {
                x: bounds.X,
                center: bounds.Y + bounds.Height / 2.0,
                height: bounds.Height,
                text: word.Text()?.to_string_lossy(),
            });
        }
    }
    words.sort_by(|a, b| a.center.total_cmp(&b.center));
    let mut text = String::new();
    let mut remaining = words.as_mut_slice();
    while let Some(first) = remaining.first() {
        let end = remaining
            .partition_point(|word| word.center - first.center <= (first.height * 0.5).max(3.0));
        let (row, rest) = remaining.split_at_mut(end);
        row.sort_by(|a, b| a.x.total_cmp(&b.x));
        for word in row {
            text.push_str(&word.text);
            text.push(' ');
        }
        text.push('\n');
        remaining = rest;
    }
    Ok(text)
}

fn game_window(pid: u32) -> HWND {
    struct Search {
        pid: u32,
        hwnd: HWND,
    }
    unsafe extern "system" fn visit(hwnd: HWND, data: LPARAM) -> i32 {
        unsafe {
            let search = &mut *(data as *mut Search);
            let mut owner = 0;
            GetWindowThreadProcessId(hwnd, &mut owner);
            if owner == search.pid && IsWindowVisible(hwnd) != 0 && IsIconic(hwnd) == 0 {
                let mut title = [0u16; 64];
                let len = GetWindowTextW(hwnd, title.as_mut_ptr(), title.len() as i32) as usize;
                if String::from_utf16_lossy(&title[..len])
                    .to_ascii_lowercase()
                    .starts_with("azeroth survivors")
                {
                    search.hwnd = hwnd;
                    return 0;
                }
            }
            1
        }
    }
    unsafe {
        let mut search = Search {
            pid,
            hwnd: null_mut(),
        };
        EnumWindows(Some(visit), (&mut search as *mut Search) as LPARAM);
        search.hwnd
    }
}

fn capture(pid: u32) -> io::Result<Frame> {
    let hwnd = game_window(pid);
    unsafe {
        let mut bounds: RECT = std::mem::zeroed();
        if hwnd.is_null() || GetClientRect(hwnd, &mut bounds) == 0 {
            return Err(io::Error::other("Game window is unavailable."));
        }
        let width = bounds.right - bounds.left;
        let height = bounds.bottom - bounds.top;
        if !(200..=4096).contains(&width) || !(200..=4096).contains(&height) {
            return Err(io::Error::other(
                "Game window size is outside the capture range.",
            ));
        }
        let mut info: BITMAPINFO = std::mem::zeroed();
        info.bmiHeader.biSize = size_of_val(&info.bmiHeader) as u32;
        info.bmiHeader.biWidth = width;
        info.bmiHeader.biHeight = -height;
        info.bmiHeader.biPlanes = 1;
        info.bmiHeader.biBitCount = 32;
        info.bmiHeader.biCompression = BI_RGB;
        let dc = CreateCompatibleDC(null_mut());
        if dc.is_null() {
            return Err(io::Error::last_os_error());
        }
        let mut bits = null_mut();
        let bitmap = CreateDIBSection(dc, &info, DIB_RGB_COLORS, &mut bits, null_mut(), 0);
        if bitmap.is_null() || bits.is_null() {
            if !bitmap.is_null() {
                DeleteObject(bitmap);
            }
            DeleteDC(dc);
            return Err(io::Error::other("Unable to allocate a game screenshot."));
        }
        let previous = SelectObject(dc, bitmap);
        let frame = if !previous.is_null() && PrintWindow(hwnd, dc, 3) != 0 {
            let stride = width as usize * 4;
            let width = width as usize;
            let height = height as usize;
            let source = slice::from_raw_parts(bits.cast::<u8>(), stride * height);
            let pixels = source.to_vec();
            Ok(Frame {
                pixels,
                width,
                height,
            })
        } else {
            Err(io::Error::other("The game did not return a screenshot."))
        };
        if !previous.is_null() {
            SelectObject(dc, previous);
        }
        DeleteObject(bitmap);
        DeleteDC(dc);
        frame
    }
}
