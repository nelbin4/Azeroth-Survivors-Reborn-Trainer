//! Verify the lobby role through the rooted Lua environment before a write.
use std::{
    io,
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

use crate::{diagnostic_log::event, process::Reader};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Solo,
    Host,
    Guest,
}

#[derive(Clone, Copy)]
pub struct RoleProof {
    state: usize,
    globals: usize,
}

trait Memory {
    fn read(&self, address: usize, bytes: &mut [u8]) -> io::Result<()>;
}

impl Memory for Reader {
    fn read(&self, address: usize, bytes: &mut [u8]) -> io::Result<()> {
        Reader::read(self, address, bytes)
    }
}

fn read<const N: usize>(memory: &impl Memory, address: usize) -> io::Result<[u8; N]> {
    let mut bytes = [0; N];
    memory.read(address, &mut bytes)?;
    Ok(bytes)
}

fn word(bytes: &[u8], offset: usize) -> usize {
    usize::from_le_bytes(bytes[offset..offset + 8].try_into().unwrap())
}

fn tag(bytes: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap())
}

fn pointer(address: usize) -> bool {
    (0x10000..0x7fff_ffff_f000).contains(&address) && address.is_multiple_of(8)
}

fn string(memory: &impl Memory, address: usize) -> io::Result<Vec<u8>> {
    if !pointer(address) {
        return Err(io::Error::other("Invalid Lua string pointer."));
    }
    let header = read::<24>(memory, address)?;
    let length = word(&header, 16);
    if header[8] != 4 || !(1..=128).contains(&length) {
        return Err(io::Error::other("Invalid Lua string header."));
    }
    let mut value = vec![0; length];
    memory.read(address + 24, &mut value)?;
    Ok(value)
}

#[derive(Clone, Copy, PartialEq, Eq)]
struct Table {
    nodes: usize,
    count: usize,
    free: usize,
}

fn table(memory: &impl Memory, address: usize) -> io::Result<Table> {
    if !pointer(address) {
        return Err(io::Error::other("Invalid Lua table pointer."));
    }
    let raw = read::<64>(memory, address)?;
    let nodes = word(&raw, 32);
    if raw[8] != 5 || raw[12] > 16 || !pointer(nodes) {
        return Err(io::Error::other("Invalid Lua table header."));
    }
    let count = 1usize << raw[12];
    let free = word(&raw, 40);
    if !(nodes..=nodes + count * 40).contains(&free) || !(free - nodes).is_multiple_of(40) {
        return Err(io::Error::other("Invalid Lua node bounds."));
    }
    Ok(Table { nodes, count, free })
}

fn lookup(memory: &impl Memory, address: usize, key: &[u8]) -> io::Result<(usize, [u8; 16])> {
    let layout = table(memory, address)?;
    let step = (key.len() >> 5) + 1;
    let hash =
        key.iter()
            .rev()
            .step_by(step)
            .take(key.len() / step)
            .fold(key.len() as u32, |hash, b| {
                hash ^ ((hash << 5)
                    .wrapping_add(hash >> 2)
                    .wrapping_add(u32::from(*b)))
            });
    let mut node = layout.nodes + (hash as usize & (layout.count - 1)) * 40;
    let mut found = None;
    for _ in 0..layout.count.min(256) {
        if !(layout.nodes..layout.nodes + layout.count * 40).contains(&node)
            || !(node - layout.nodes).is_multiple_of(40)
        {
            return Err(io::Error::other("Invalid Lua node chain."));
        }
        let raw = read::<40>(memory, node)?;
        if tag(&raw, 24) == 4
            && string(memory, word(&raw, 16)).is_ok_and(|name| name == key)
            && found
                .replace((node, raw[..16].try_into().unwrap()))
                .is_some()
        {
            return Err(io::Error::other("Lua key is ambiguous."));
        }
        node = word(&raw, 32);
        if node == 0 {
            break;
        }
    }
    if node != 0 || table(memory, address)? != layout {
        return Err(io::Error::other("Lua table changed during lookup."));
    }
    found.ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "Expected Lua key was not found."))
}

fn rooted_globals(memory: &impl Memory, state: usize) -> io::Result<usize> {
    let raw = read::<184>(memory, state)?;
    let global_state = word(&raw, 32);
    let globals = word(&raw, 120);
    if raw[8] != 8
        || !pointer(global_state)
        || tag(&raw, 128) != 5
        || word(&read::<8>(memory, global_state + 176)?, 0) != state
    {
        return Err(io::Error::other("Lua runtime is not rooted."));
    }
    let (_, value) = lookup(memory, globals, b"_G")?;
    if word(&value, 0) != globals || tag(&value, 8) != 5 {
        return Err(io::Error::other(
            "Lua global table does not reference itself.",
        ));
    }
    Ok(globals)
}

impl RoleProof {
    pub fn read(self, reader: &Reader) -> io::Result<Role> {
        self.read_memory(reader)
    }

    fn read_memory(self, memory: &impl Memory) -> io::Result<Role> {
        if rooted_globals(memory, self.state)? != self.globals {
            return Err(io::Error::other("Lua runtime ownership changed."));
        }
        let globals_header = table(memory, self.globals)?;
        let (lobby_slot, lobby_value) = lookup(memory, self.globals, b"AZS_RUN_LOBBY")?;
        let lobby = word(&lobby_value, 0);
        if tag(&lobby_value, 8) != 5 {
            return Err(io::Error::other("Lobby role table is unavailable."));
        }
        let lobby_header = table(memory, lobby)?;
        let (role_slot, role_value) = lookup(memory, lobby, b"role")?;
        if tag(&role_value, 8) != 4 {
            return Err(io::Error::other("Unexpected lobby role type."));
        }
        let role = match string(memory, word(&role_value, 0))?.as_slice() {
            b"none" => Role::Solo,
            b"host" => Role::Host,
            b"guest" => Role::Guest,
            _ => return Err(io::Error::other("Unknown lobby role.")),
        };
        if read::<16>(memory, role_slot)? != role_value
            || table(memory, lobby)? != lobby_header
            || read::<16>(memory, lobby_slot)? != lobby_value
            || table(memory, self.globals)? != globals_header
        {
            return Err(io::Error::other("Lobby role changed during verification."));
        }
        Ok(role)
    }
}

fn read_scan_chunk(memory: &impl Memory, start: usize, bytes: &mut [u8]) -> io::Result<()> {
    use windows_sys::Win32::Foundation::ERROR_PARTIAL_COPY;

    match memory.read(start, bytes) {
        Ok(()) => return Ok(()),
        Err(error) if error.raw_os_error() == Some(ERROR_PARTIAL_COPY as i32) => {}
        Err(error) => return Err(error),
    }
    // Recover every page, including the overlap used for boundary-spanning candidates.
    const PAGE_SIZE: usize = 4096;
    for (index, page) in bytes.chunks_mut(PAGE_SIZE).enumerate() {
        let address = start + index * PAGE_SIZE;
        if let Err(error) = memory.read(address, page) {
            if error.raw_os_error() != Some(ERROR_PARTIAL_COPY as i32) {
                return Err(error);
            }
            memory.read(address, page)?;
        }
    }
    Ok(())
}

pub fn find(reader: &Reader, cancel: &AtomicBool) -> io::Result<(RoleProof, Role)> {
    use windows_sys::Win32::Foundation::ERROR_PARTIAL_COPY;

    loop {
        match scan_role(reader, cancel) {
            Err(error)
                if error.raw_os_error() == Some(ERROR_PARTIAL_COPY as i32)
                    && !cancel.load(Ordering::Acquire) =>
            {
                event("apply.session_retry", format_args!("partial_read: {error}"));
                std::thread::sleep(Duration::from_millis(150));
            }
            result => return result,
        }
    }
}

fn scan_role(reader: &Reader, cancel: &AtomicBool) -> io::Result<(RoleProof, Role)> {
    let deadline = Instant::now() + Duration::from_secs(40);
    let mut found = None;
    let mut buffer = vec![0; 1024 * 1024 + 136];
    for region in reader.private_regions()? {
        for start in (region.start..region.end).step_by(1024 * 1024) {
            if cancel.load(Ordering::Acquire) {
                return Err(io::Error::new(
                    io::ErrorKind::Interrupted,
                    "Apply cancelled.",
                ));
            }
            if Instant::now() >= deadline {
                return Err(io::Error::other(
                    "Session verification timed out. Try Find again.",
                ));
            }
            let length = buffer.len().min(region.end - start);
            let raw = &mut buffer[..length];
            read_scan_chunk(reader, start, raw)?;
            let candidates = length.saturating_sub(131).min(1024 * 1024);
            if candidates == 0 {
                continue;
            }
            // Search for the one-byte Lua thread tag before reading candidate headers.
            for offset in memchr::memchr_iter(8, &raw[8..8 + candidates]) {
                if !offset.is_multiple_of(8)
                    || tag(raw, offset + 128) != 5
                    || !pointer(word(raw, offset + 32))
                    || !pointer(word(raw, offset + 120))
                {
                    continue;
                }
                let state = start + offset;
                let Ok(globals) = rooted_globals(reader, state) else {
                    continue;
                };
                match lookup(reader, globals, b"AZS_RUN_LOBBY") {
                    Ok(_) => {}
                    Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                    Err(error) => return Err(error),
                }
                if found.is_some() {
                    return Err(io::Error::other(
                        "More than one rooted lobby runtime matched.",
                    ));
                }
                found = Some(RoleProof { state, globals });
            }
        }
    }
    let proof = found.ok_or_else(|| io::Error::other("Session role could not be verified."))?;
    let role = proof.read(reader)?;
    event("apply.session_verified", format_args!("role={role:?}"));
    Ok((proof, role))
}

#[cfg(test)]
mod tests {
    use super::*;

    struct PartialRead {
        blocked_page: Option<usize>,
    }

    impl Memory for PartialRead {
        fn read(&self, address: usize, bytes: &mut [u8]) -> io::Result<()> {
            if bytes.len() > 4096 || self.blocked_page == Some(address) {
                return Err(io::Error::from_raw_os_error(
                    windows_sys::Win32::Foundation::ERROR_PARTIAL_COPY as i32,
                ));
            }
            bytes.fill(if address == 0x10000 { 1 } else { 2 });
            Ok(())
        }
    }

    #[test]
    fn partial_chunk_requires_every_page_including_overlap() {
        let mut bytes = [0; 4096 + 136];
        read_scan_chunk(&PartialRead { blocked_page: None }, 0x10000, &mut bytes).unwrap();
        assert!(bytes[..4096].iter().all(|&byte| byte == 1));
        assert!(bytes[4096..].iter().all(|&byte| byte == 2));
        assert!(
            read_scan_chunk(
                &PartialRead {
                    blocked_page: Some(0x11000)
                },
                0x10000,
                &mut bytes,
            )
            .is_err()
        );
    }

    struct Fake(Vec<u8>);

    impl Memory for Fake {
        fn read(&self, address: usize, bytes: &mut [u8]) -> io::Result<()> {
            let offset = address
                .checked_sub(0x10000)
                .ok_or_else(|| io::Error::other("unmapped"))?;
            bytes.copy_from_slice(
                self.0
                    .get(offset..offset + bytes.len())
                    .ok_or_else(|| io::Error::other("unmapped"))?,
            );
            Ok(())
        }
    }

    impl Fake {
        fn put(&mut self, address: usize, bytes: &[u8]) {
            let offset = address - 0x10000;
            self.0[offset..offset + bytes.len()].copy_from_slice(bytes);
        }

        fn word(&mut self, address: usize, value: usize) {
            self.put(address, &value.to_le_bytes());
        }

        fn node(&mut self, address: usize, value: usize, tag: u8, key: usize) {
            self.word(address, value);
            self.put(address + 8, &[tag]);
            self.word(address + 16, key);
            self.put(address + 24, &[4]);
        }

        fn table(&mut self, address: usize, nodes: usize, count_log: u8) {
            self.put(address + 8, &[5]);
            self.put(address + 12, &[count_log]);
            self.word(address + 32, nodes);
            self.word(address + 40, nodes + (1 << count_log) * 40);
        }

        fn text(&mut self, address: usize, text: &[u8]) {
            self.put(address + 8, &[4]);
            self.word(address + 16, text.len());
            self.put(address + 24, text);
        }
    }

    #[test]
    fn role_reads_require_a_live_root_and_distinguish_solo_from_coop() {
        let mut fake = Fake(vec![0; 0x9000]);
        let proof = RoleProof {
            state: 0x12000,
            globals: 0x11000,
        };
        fake.put(proof.state + 8, &[8]);
        fake.word(proof.state + 32, 0x13000);
        fake.word(proof.state + 120, proof.globals);
        fake.put(proof.state + 128, &[5]);
        fake.word(0x13000 + 176, proof.state);
        fake.table(proof.globals, 0x14000, 3);
        fake.table(0x15000, 0x16000, 1);
        fake.text(0x18000, b"_G");
        fake.text(0x18100, b"AZS_RUN_LOBBY");
        fake.text(0x18200, b"role");
        fake.text(0x18300, b"none");
        fake.text(0x18400, b"host");
        fake.text(0x18500, b"guest");
        fake.node(0x140c8, proof.globals, 5, 0x18000);
        fake.node(0x140a0, 0x15000, 5, 0x18100);
        for (address, role) in [
            (0x18300, Role::Solo),
            (0x18400, Role::Host),
            (0x18500, Role::Guest),
        ] {
            fake.node(0x16028, address, 4, 0x18200);
            assert_eq!(proof.read_memory(&fake).unwrap(), role);
        }
        fake.word(0x13000 + 176, 0);
        assert!(proof.read_memory(&fake).is_err());
    }
}
