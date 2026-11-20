use std::{
    io,
    path::{Path, PathBuf},
    process::Command,
};

use windows_sys::Win32::{
    Foundation::{CloseHandle, ERROR_NO_MORE_FILES, FILETIME, HANDLE, INVALID_HANDLE_VALUE},
    System::{
        Diagnostics::{
            Debug::ReadProcessMemory,
            ToolHelp::{
                CreateToolhelp32Snapshot, MODULEENTRY32W, Module32FirstW, Module32NextW,
                PROCESSENTRY32W, Process32FirstW, Process32NextW, TH32CS_SNAPMODULE,
                TH32CS_SNAPPROCESS, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
            },
        },
        Threading::{
            GetProcessTimes, OpenProcess, OpenThread, PROCESS_QUERY_INFORMATION,
            PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_VM_READ, QueryFullProcessImageNameW,
            THREAD_QUERY_INFORMATION,
        },
    },
};

pub const GAME_EXE: &str = "AzerothSurvivorsGame.exe";

struct Handle(HANDLE);

impl Handle {
    fn new(raw: HANDLE) -> io::Result<Self> {
        if raw.is_null() || raw == INVALID_HANDLE_VALUE {
            Err(io::Error::last_os_error())
        } else {
            Ok(Self(raw))
        }
    }
}

impl Drop for Handle {
    fn drop(&mut self) {
        // This object owns exactly one successful Windows handle.
        unsafe { CloseHandle(self.0) };
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Session {
    pub pid: u32,
    pub created: u64,
    pub path: PathBuf,
}

#[derive(Debug, Default)]
pub struct Games {
    sessions: Vec<Session>,
    inaccessible: usize,
}

impl Games {
    pub fn running(&self) -> bool {
        self.inaccessible > 0 || !self.sessions.is_empty()
    }

    pub fn select(&self, preferred: Option<&Path>, current: Option<&Session>) -> Option<&Session> {
        if let Some(session) =
            current.and_then(|current| self.sessions.iter().find(|s| *s == current))
        {
            return Some(session);
        }
        if self.inaccessible > 0 {
            return None;
        }
        if let [session] = self.sessions.as_slice() {
            return Some(session);
        }
        let preferred = preferred?;
        let mut matches = self
            .sessions
            .iter()
            .filter(|session| same_path(&session.path, preferred));
        let selected = matches.next()?;
        matches.next().is_none().then_some(selected)
    }
}

fn same_path(a: &Path, b: &Path) -> bool {
    fn comparable(path: &Path) -> String {
        let raw = path.to_string_lossy().replace('/', "\\");
        raw.strip_prefix(r"\\?\").unwrap_or(&raw).to_lowercase()
    }
    comparable(a) == comparable(b)
}

fn wide_string(raw: &[u16]) -> String {
    String::from_utf16_lossy(&raw[..raw.iter().position(|&c| c == 0).unwrap_or(raw.len())])
}

fn identity(pid: u32, handle: &Handle) -> io::Result<Session> {
    let mut path = vec![0; 32_768];
    let mut length = path.len() as u32;
    let mut created = FILETIME::default();
    let mut exit = FILETIME::default();
    let mut kernel = FILETIME::default();
    let mut user = FILETIME::default();
    // The buffer and output structs remain valid for both synchronous calls.
    unsafe {
        if QueryFullProcessImageNameW(handle.0, 0, path.as_mut_ptr(), &mut length) == 0
            || GetProcessTimes(handle.0, &mut created, &mut exit, &mut kernel, &mut user) == 0
        {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(Session {
        pid,
        created: (u64::from(created.dwHighDateTime) << 32) | u64::from(created.dwLowDateTime),
        path: PathBuf::from(String::from_utf16_lossy(&path[..length as usize])),
    })
}

pub fn discover() -> io::Result<Games> {
    // The snapshot and process query handles are checked before they are used.
    let snapshot = Handle::new(unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) })?;
    let mut entry = PROCESSENTRY32W {
        dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32,
        ..Default::default()
    };
    let mut available = unsafe { Process32FirstW(snapshot.0, &mut entry) };
    let mut games = Games::default();
    while available != 0 {
        if wide_string(&entry.szExeFile).eq_ignore_ascii_case(GAME_EXE) {
            let pid = entry.th32ProcessID;
            match Handle::new(unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) })
                .and_then(|handle| identity(pid, &handle))
            {
                Ok(session) => games.sessions.push(session),
                Err(_) => games.inaccessible += 1,
            }
        }
        available = unsafe { Process32NextW(snapshot.0, &mut entry) };
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(ERROR_NO_MORE_FILES as i32) {
        Ok(games)
    } else {
        Err(error)
    }
}

pub fn launch(path: &Path) -> io::Result<Session> {
    let path = path.canonicalize()?;
    if !path.is_file()
        || !path
            .file_name()
            .is_some_and(|name| name.eq_ignore_ascii_case(GAME_EXE))
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "Choose AzerothSurvivorsGame.exe.",
        ));
    }
    if discover()?.running() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "Reborn is already running.",
        ));
    }
    let child = Command::new(&path)
        .current_dir(path.parent().expect("canonical executable parent"))
        .spawn()?;
    let pid = child.id();
    let handle = Handle::new(unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) })?;
    let session = identity(pid, &handle)?;
    if !same_path(&path, &session.path) {
        return Err(io::Error::other("The launched process identity changed."));
    }
    Ok(session)
}

pub fn stop(session: &Session) -> io::Result<()> {
    use windows_sys::Win32::System::Threading::{PROCESS_TERMINATE, TerminateProcess};

    let handle = Handle::new(unsafe {
        OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE,
            0,
            session.pid,
        )
    })?;
    if identity(session.pid, &handle)? != *session {
        return Err(io::Error::other("Reborn process identity changed."));
    }
    // The queried handle pins this process even if the PID is later reused.
    if unsafe { TerminateProcess(handle.0, 1) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub fn current_creation_time() -> io::Result<u64> {
    let pid = std::process::id();
    let handle = Handle::new(unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) })?;
    Ok(identity(pid, &handle)?.created)
}

pub fn wait_for_update_parent(pid: u32, created: u64, target: &Path) -> io::Result<()> {
    use windows_sys::Win32::{
        Foundation::{ERROR_INVALID_PARAMETER, WAIT_OBJECT_0, WAIT_TIMEOUT},
        System::Threading::WaitForSingleObject,
    };
    const SYNCHRONIZE: u32 = 0x0010_0000;
    if pid == 0 {
        return Ok(());
    }
    let process = match Handle::new(unsafe {
        OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, pid)
    }) {
        Ok(handle) => handle,
        Err(error) if error.raw_os_error() == Some(ERROR_INVALID_PARAMETER as i32) => return Ok(()),
        Err(error) => return Err(error),
    };
    let session = match identity(pid, &process) {
        Ok(session) => session,
        Err(_) if unsafe { WaitForSingleObject(process.0, 0) } == WAIT_OBJECT_0 => return Ok(()),
        Err(error) => return Err(error),
    };
    if session.created != created || !same_path(&session.path, target) {
        return Ok(());
    }
    match unsafe { WaitForSingleObject(process.0, 30_000) } {
        WAIT_OBJECT_0 => Ok(()),
        WAIT_TIMEOUT => Err(io::Error::other(
            "Timed out waiting for the trainer to close.",
        )),
        _ => Err(io::Error::last_os_error()),
    }
}

pub struct Reader(Handle);

#[repr(C)]
#[derive(Default)]
struct ThreadInfo {
    exit_status: i32,
    teb: usize,
    pid: usize,
    tid: usize,
    affinity: usize,
    priority: i32,
    base_priority: i32,
}

#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtQueryInformationThread(
        thread: HANDLE,
        class: u32,
        info: *mut ThreadInfo,
        size: u32,
        length: *mut u32,
    ) -> i32;
}

impl Reader {
    pub fn open(session: &Session) -> io::Result<Self> {
        let handle = Handle::new(unsafe {
            OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, 0, session.pid)
        })?;
        if identity(session.pid, &handle)? != *session {
            return Err(io::Error::other("Reborn process identity changed."));
        }
        Ok(Self(handle))
    }

    pub fn module_base(&self, session: &Session) -> io::Result<usize> {
        if identity(session.pid, &self.0)? != *session {
            return Err(io::Error::other("Reborn process identity changed."));
        }
        let snapshot =
            Handle::new(unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, session.pid) })?;
        let mut module = MODULEENTRY32W {
            dwSize: std::mem::size_of::<MODULEENTRY32W>() as u32,
            ..Default::default()
        };
        let mut available = unsafe { Module32FirstW(snapshot.0, &mut module) };
        while available != 0 {
            if same_path(Path::new(&wide_string(&module.szExePath)), &session.path) {
                return Ok(module.modBaseAddr as usize);
            }
            available = unsafe { Module32NextW(snapshot.0, &mut module) };
        }
        Err(io::Error::other(
            "Could not locate the running game module.",
        ))
    }

    pub fn read(&self, address: usize, bytes: &mut [u8]) -> io::Result<()> {
        let mut count = 0;
        // Windows writes to the provided local slice. No writable process handle is acquired.
        let ok = unsafe {
            ReadProcessMemory(
                self.0.0,
                address as _,
                bytes.as_mut_ptr().cast(),
                bytes.len(),
                &mut count,
            )
        };
        if ok == 0 {
            Err(io::Error::last_os_error())
        } else if count != bytes.len() {
            Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "Partial game memory read.",
            ))
        } else {
            Ok(())
        }
    }

    pub fn stacks(&self, session: &Session) -> io::Result<Vec<std::ops::Range<usize>>> {
        let snapshot = Handle::new(unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) })?;
        let size = std::mem::size_of::<THREADENTRY32>() as u32;
        let mut entry = THREADENTRY32 {
            dwSize: size,
            ..Default::default()
        };
        let mut available = unsafe { Thread32First(snapshot.0, &mut entry) };
        let mut stacks = Vec::new();
        while available != 0 {
            if entry.dwSize >= 16
                && entry.th32OwnerProcessID == session.pid
                && let Some(bounds) = self.stack(session.pid, entry.th32ThreadID)
            {
                stacks.push(bounds);
            }
            entry.dwSize = size;
            available = unsafe { Thread32Next(snapshot.0, &mut entry) };
        }
        Ok(stacks)
    }

    fn stack(&self, pid: u32, tid: u32) -> Option<std::ops::Range<usize>> {
        let thread = Handle::new(unsafe { OpenThread(THREAD_QUERY_INFORMATION, 0, tid) }).ok()?;
        let mut info = ThreadInfo::default();
        let status = unsafe {
            NtQueryInformationThread(
                thread.0,
                0,
                &mut info,
                std::mem::size_of::<ThreadInfo>() as u32,
                std::ptr::null_mut(),
            )
        };
        if status != 0 || info.teb == 0 || info.pid != pid as usize || info.tid != tid as usize {
            return None;
        }
        let mut raw = [0; 16];
        self.read(info.teb.checked_add(8)?, &mut raw).ok()?;
        let top = usize::from_le_bytes(raw[..8].try_into().ok()?);
        let bottom = usize::from_le_bytes(raw[8..].try_into().ok()?);
        let length = top.checked_sub(bottom)?;
        (length > 0 && length <= 16 * 1024 * 1024).then_some(bottom..top)
    }
}

/// Holds the same process-scoped mutex name as the Python trainer.
pub struct StatWriter {
    process: Handle,
    _lock: Handle,
}

impl StatWriter {
    pub fn open(session: &Session) -> io::Result<Self> {
        use sha2::{Digest, Sha256};
        use windows_sys::Win32::{
            Foundation::{ERROR_ALREADY_EXISTS, GetLastError, SetLastError},
            System::Threading::{CreateMutexW, PROCESS_VM_OPERATION, PROCESS_VM_WRITE},
        };
        let epoch = session
            .created
            .checked_sub(116_444_736_000_000_000)
            .ok_or_else(|| io::Error::other("Invalid process creation time."))?;
        let unix_seconds = epoch as f64 / 10_000_000.0;
        let identity_key = format!("{}_{unix_seconds:?}", session.pid);
        let digest = format!("{:x}", Sha256::digest(identity_key.as_bytes()));
        let name: Vec<_> = format!("Local\\RebornTrainer_{}", &digest[..24])
            .encode_utf16()
            .chain([0])
            .collect();
        let (mutex, status) = unsafe {
            SetLastError(0);
            let mutex = CreateMutexW(std::ptr::null(), 0, name.as_ptr());
            (mutex, GetLastError())
        };
        let lock = Handle::new(mutex)?;
        if status == ERROR_ALREADY_EXISTS {
            return Err(io::Error::other(
                "Another trainer is already controlling Reborn.",
            ));
        }
        let process = Handle::new(unsafe {
            OpenProcess(
                PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_WRITE,
                0,
                session.pid,
            )
        })?;
        if identity(session.pid, &process)? != *session {
            return Err(io::Error::other("Reborn process identity changed."));
        }
        Ok(Self {
            process,
            _lock: lock,
        })
    }

    pub(crate) fn write_value(&self, address: usize, value: &[u8]) -> io::Result<()> {
        use windows_sys::Win32::System::Diagnostics::Debug::WriteProcessMemory;

        let mut written = 0;
        // The caller has revalidated this single mapped field and session.
        let ok = unsafe {
            WriteProcessMemory(
                self.process.0,
                address as _,
                value.as_ptr().cast(),
                value.len(),
                &mut written,
            )
        };
        if ok == 0 {
            Err(io::Error::last_os_error())
        } else if written != value.len() {
            Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "Incomplete stat write.",
            ))
        } else {
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn session(pid: u32, path: &str) -> Session {
        Session {
            pid,
            created: 5,
            path: path.into(),
        }
    }

    #[test]
    fn ambiguous_processes_need_an_exact_selection() {
        let first = session(1, "C:/Game/AzerothSurvivorsGame.exe");
        let second = session(2, "D:/Game/AzerothSurvivorsGame.exe");
        let games = Games {
            sessions: vec![first.clone(), second],
            inaccessible: 0,
        };
        assert!(games.select(None, None).is_none());
        assert_eq!(
            games.select(Some(Path::new(r"c:\game\AzerothSurvivorsGame.exe")), None),
            Some(&first)
        );
        assert_eq!(games.select(None, Some(&first)), Some(&first));
    }

    #[test]
    fn inaccessible_process_blocks_automatic_selection() {
        let games = Games {
            sessions: vec![session(1, "C:/Game/AzerothSurvivorsGame.exe")],
            inaccessible: 1,
        };
        assert!(games.running());
        assert!(games.select(None, None).is_none());
    }

    #[test]
    fn writer_uses_one_process_lock_and_writes_exactly_four_bytes() {
        use std::cell::UnsafeCell;

        let pid = std::process::id();
        let handle =
            Handle::new(unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) }).unwrap();
        let session = identity(pid, &handle).unwrap();
        let writer = StatWriter::open(&session).unwrap();
        assert!(StatWriter::open(&session).is_err());
        let values = UnsafeCell::new([17u32, 12]);
        let address = values.get() as usize;
        writer.write_value(address, &18u32.to_le_bytes()).unwrap();
        let mut bytes = [0; 8];
        Reader::open(&session)
            .unwrap()
            .read(address, &mut bytes)
            .unwrap();
        assert_eq!(u32::from_le_bytes(bytes[..4].try_into().unwrap()), 18);
        assert_eq!(u32::from_le_bytes(bytes[4..].try_into().unwrap()), 12);
        drop(writer);
        assert!(StatWriter::open(&session).is_ok());
    }
}
