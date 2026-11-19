//! Discover the selected character and prepare independently verified fields.
use std::{
    collections::{BTreeMap, BTreeSet},
    fs, io,
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

use iced_x86::{Decoder, DecoderOptions, Instruction, Mnemonic, OpKind, Register};
use sha2::{Digest, Sha256};

use crate::{
    diagnostic_log::event,
    process::{DamageWriter, Reader, Session},
    session_role::{self, Role},
};

type Array = [usize; 4]; // selected index, count, vector pointer, stride
type Guard = (usize, Vec<u8>);

#[derive(Clone, Debug)]
struct Plan {
    build: String,
    digest: String,
    array: Array,
    name: usize,
    stats: usize,
    guards: Vec<Guard>,
}

#[derive(Debug)]
pub struct Snapshot {
    pub build: String,
    pub character: Option<Character>,
    pub damage: Option<VerifiedDamage>,
}

#[derive(Debug, Eq, PartialEq)]
pub struct Character {
    pub name: String,
    pub count: usize,
    pub index: usize,
    pub health_candidate: [usize; 2],
    game: usize,
    identity: usize,
}

fn machine_word(raw: &[u8], offset: usize) -> Option<usize> {
    Some(usize::from_le_bytes(
        raw.get(offset..offset.checked_add(8)?)?.try_into().ok()?,
    ))
}

fn scalar(raw: &[u8], offset: usize, width: usize) -> Option<usize> {
    let bytes = raw.get(offset..offset.checked_add(width)?)?;
    Some(
        bytes
            .iter()
            .rev()
            .fold(0, |value, &byte| (value << 8) | usize::from(byte)),
    )
}

fn text_section(blob: &[u8]) -> Option<(usize, &[u8])> {
    if blob.get(..2)? != b"MZ" {
        return None;
    }
    let pe = scalar(blob, 0x3c, 4)?;
    if blob.get(pe..pe + 4)? != b"PE\0\0" || scalar(blob, pe + 4, 2)? != 0x8664 {
        return None;
    }
    let count = scalar(blob, pe + 6, 2)?;
    if !(1..=96).contains(&count) {
        return None;
    }
    let start = pe + 24 + scalar(blob, pe + 20, 2)?;
    let mut sections = blob
        .get(start..start + 40 * count)?
        .chunks_exact(40)
        .filter(|section| section.starts_with(b".text\0\0\0"));
    let section = sections.next()?;
    if sections.next().is_some() {
        return None;
    }
    let rva = scalar(section, 12, 4)?;
    let length = scalar(section, 16, 4)?;
    let offset = scalar(section, 20, 4)?;
    Some((rva, blob.get(offset..offset + length)?))
}

fn register(instruction: &Instruction, index: u32) -> Option<Register> {
    (instruction.op_count() > index && instruction.op_kind(index) == OpKind::Register)
        .then(|| instruction.op_register(index))
}

fn load_register(instruction: &Instruction) -> Option<Register> {
    let destination = register(instruction, 0)?;
    (instruction.mnemonic() == Mnemonic::Mov
        && instruction.op1_kind() == OpKind::Memory
        && destination.is_gpr64()
        && instruction.memory_size().size() == 8)
        .then_some(destination)
}

fn load(instruction: &Instruction) -> Option<(Register, Register, usize)> {
    let destination = load_register(instruction)?;
    if instruction.memory_index() != Register::None {
        return None;
    }
    Some((
        destination,
        instruction.memory_base(),
        instruction.memory_displacement64() as usize,
    ))
}

#[derive(Clone, Copy)]
struct Root {
    array: Array,
    pointer: Register,
    scaled: Register,
    hero: Register,
}

impl Root {
    fn field(self, instruction: &Instruction) -> Option<usize> {
        let base = instruction.memory_base();
        let index = instruction.memory_index();
        ((base == self.hero && index == Register::None)
            || (instruction.memory_index_scale() == 1
                && ((base == self.pointer && index == self.scaled)
                    || (base == self.scaled && index == self.pointer))))
            .then(|| instruction.memory_displacement64() as usize)
    }
}

fn root(instructions: &[Instruction]) -> Option<Root> {
    let [a, b, compare, branch, pointer, multiply, lea] = instructions else {
        return None;
    };
    let (selected, _, index) = load(a)?;
    let (count_reg, game, count) = load(b)?;
    let (pointer_reg, pointer_game, pointer) = load(pointer)?;
    if pointer_game != game
        || selected == count_reg
        || selected == pointer_reg
        || [selected, count_reg].contains(&game)
        || count != pointer + 8
        || !(0x100..index).contains(&pointer)
        || index >= 0x10000
        || compare.mnemonic() != Mnemonic::Cmp
        || register(compare, 0)? != selected
        || register(compare, 1)? != count_reg
        || branch.mnemonic() != Mnemonic::Jae
        || multiply.mnemonic() != Mnemonic::Imul
        || register(multiply, 1)? != selected
        || multiply.op2_kind() != OpKind::Immediate32to64
        || lea.mnemonic() != Mnemonic::Lea
        || lea.op1_kind() != OpKind::Memory
    {
        return None;
    }
    let stride = multiply.immediate32to64() as usize;
    let scaled = register(multiply, 0)?;
    let mut root = Root {
        array: [index, count, pointer, stride],
        pointer: pointer_reg,
        scaled,
        hero: Register::None,
    };
    if !(0x200..=0x10000).contains(&stride)
        || !stride.is_multiple_of(8)
        || scaled == pointer_reg
        || root.field(lea)? != 0
    {
        return None;
    }
    root.hero = register(lea, 0)?;
    Some(root)
}

fn instructions(bytes: &[u8], rva: usize, limit: usize) -> impl Iterator<Item = Instruction> + '_ {
    Decoder::with_ip(64, bytes, rva as u64, DecoderOptions::NONE)
        .into_iter()
        .take_while(|instruction| !instruction.is_invalid())
        .filter(|instruction| instruction.mnemonic() != Mnemonic::Nop)
        .take(limit)
}

fn vector_offset(root: Root, vectors: &[Instruction], branch: &Instruction) -> Option<usize> {
    let mut offsets: Vec<_> = vectors
        .iter()
        .map(|instruction| {
            if !matches!(
                instruction.mnemonic(),
                Mnemonic::Movups | Mnemonic::Movaps | Mnemonic::Movdqu | Mnemonic::Movdqa
            ) || instruction.op1_kind() != OpKind::Memory
                || instruction.memory_size().size() != 16
            {
                return None;
            }
            root.field(instruction)
        })
        .collect::<Option<_>>()?;
    offsets.sort_unstable();
    let first = *offsets.first()?;
    (offsets.len() == 4
        && (0x160..=root.array[3] - 64).contains(&first)
        && first.is_multiple_of(4)
        && offsets == [first, first + 16, first + 32, first + 48]
        && branch.near_branch_target() >= vectors.last()?.next_ip())
    .then_some(first)
}

fn name_check(root: Root, instruction: &Instruction) -> Option<usize> {
    if instruction.mnemonic() == Mnemonic::Cmp
        && instruction.op0_kind() == OpKind::Memory
        && instruction.memory_size().size() == 8
        && instruction.try_immediate(1).ok()? as i64 == -1
    {
        return root.field(instruction);
    }
    None
}

fn name_drop(root: Root, instructions: &[Instruction]) -> Option<usize> {
    let [capacity, cmp, branch, test, empty, pointer, call, ..] = instructions else {
        return None;
    };
    let reg = load_register(capacity)?;
    let offset = root.field(capacity)?;
    if cmp.mnemonic() != Mnemonic::Cmp
        || register(cmp, 0)? != reg
        || cmp.try_immediate(1).ok()? as i64 != -1
        || branch.mnemonic() != Mnemonic::Je
        || test.mnemonic() != Mnemonic::Test
        || register(test, 0)? != reg
        || register(test, 1)? != reg
        || empty.mnemonic() != Mnemonic::Je
        || empty.near_branch_target() != branch.near_branch_target()
        || load_register(pointer)? != Register::RCX
        || pointer.memory_base() == reg
        || pointer.memory_index() == reg
        || root.field(pointer)? != offset.checked_add(8)?
        || call.mnemonic() != Mnemonic::Call
        || empty.near_branch_target() != call.next_ip()
    {
        return None;
    }
    Some(offset)
}

fn cancelled(stop: &AtomicBool) -> io::Result<()> {
    if stop.load(Ordering::Relaxed) {
        Err(io::Error::new(
            io::ErrorKind::Interrupted,
            "Search stopped.",
        ))
    } else {
        Ok(())
    }
}

fn analyze(blob: &[u8], stop: &AtomicBool) -> io::Result<Plan> {
    let (text_rva, text) = text_section(blob).ok_or_else(|| {
        io::Error::other("Expected a Windows x64 game image with one code section.")
    })?;
    let mut arrays = Vec::new();
    let mut checks: BTreeMap<(Array, usize), Vec<Guard>> = BTreeMap::new();
    let mut drops: BTreeMap<(Array, usize), Vec<Guard>> = BTreeMap::new();
    for (offset, prefix) in text.windows(3).enumerate() {
        if offset % 0x40000 == 0 {
            cancelled(stop)?;
        }
        if !matches!(prefix, [0x48..=0x4f, 0x8b, 0x80..=0xbf]) {
            continue;
        }
        let start = text_rva + offset;
        let bytes = &text[offset..text.len().min(offset + 176)];
        let mut decoder = instructions(bytes, start, 15);
        let head: Vec<_> = decoder.by_ref().take(7).collect();
        let Some(root) = root(&head) else { continue };
        let tail: Vec<_> = decoder.collect();
        let guard = |end: u64| (start, bytes[..end as usize - start].to_vec());
        if let Some(stat) = tail.get(..4).and_then(|v| vector_offset(root, v, &head[3])) {
            arrays.push((root.array, stat, guard(tail[3].next_ip())));
        }
        if let Some(name) = tail.first().and_then(|i| name_check(root, i)) {
            checks
                .entry((root.array, name))
                .or_default()
                .push(guard(tail[0].next_ip()));
        }
        if let Some(name) = name_drop(root, &tail) {
            drops
                .entry((root.array, name))
                .or_default()
                .push(guard(tail[6].next_ip()));
        }
    }
    let [(array, stats, primary)] = arrays.as_slice() else {
        return Err(io::Error::other(format!(
            "Player array unavailable. Expected one stat serializer, found {}.",
            arrays.len()
        )));
    };
    let names: Vec<_> = checks
        .iter()
        .filter_map(|(&(owner, name), guard)| {
            (owner == *array && (0x160..=array[3] - 24).contains(&name) && name.is_multiple_of(8))
                .then(|| drops.get(&(owner, name)).map(|drop| (name, guard, drop)))
                .flatten()
        })
        .collect();
    let [(name, check, drop)] = names.as_slice() else {
        return Err(io::Error::other(
            "Player identity unavailable. The character routines did not identify one field.",
        ));
    };
    let guards = std::iter::once(primary.clone())
        .chain(check.iter().cloned())
        .chain(drop.iter().cloned())
        .collect();
    let digest = format!("{:x}", Sha256::digest(blob));
    Ok(Plan {
        build: digest[..12].into(),
        digest,
        array: *array,
        name: *name,
        stats: *stats,
        guards,
    })
}

fn plausible_pointer(address: usize) -> bool {
    (0x10000..0x7fff_ffff_f000).contains(&address) && address.is_multiple_of(8)
}

fn snapshot(
    reader: &Reader,
    plan: &Plan,
    game: usize,
    header: &[u8],
) -> Option<(usize, Character)> {
    let [index_offset, _, vector_pointer, stride] = plan.array;
    let cap = machine_word(header, 0)?;
    let pointer = machine_word(header, 8)?;
    let count = machine_word(header, 16)?;
    if !(1..=4).contains(&count) || !(count..=64).contains(&cap) || !plausible_pointer(pointer) {
        return None;
    }
    let mut index = [0; 8];
    reader
        .read(game.checked_add(index_offset)?, &mut index)
        .ok()?;
    let selected = usize::from_le_bytes(index);
    if selected >= count {
        return None;
    }
    let hero = pointer.checked_add(selected * stride)?;
    let mut raw = vec![0; stride];
    reader.read(hero, &mut raw).ok()?;
    let identity = machine_word(&raw, 0xF0)?;
    if identity == 0
        || (0..16).any(|i| scalar(&raw, plan.stats + 4 * i, 4).is_none_or(|n| n > 1_000_000))
    {
        return None;
    }
    let health_candidate = [machine_word(&raw, 0xF8)?, machine_word(&raw, 0x100)?];
    if !(1..=1_000_000_000).contains(&health_candidate[1])
        || health_candidate[0] > health_candidate[1]
    {
        return None;
    }
    let capacity = machine_word(&raw, plan.name)?;
    let name_address = machine_word(&raw, plan.name + 8)?;
    let length = machine_word(&raw, plan.name + 16)?;
    if !(1..=capacity).contains(&length)
        || capacity > 128
        || !(0x10000..0x7fff_ffff_f000).contains(&name_address)
    {
        return None;
    }
    let mut name = vec![0; length];
    reader.read(name_address, &mut name).ok()?;
    let name = String::from_utf8(name).ok()?;
    if name.trim().is_empty() || name.chars().any(char::is_control) {
        return None;
    }
    let mut observed_header = [0; 24];
    let mut observed_index = [0; 8];
    let mut observed_name = [0; 24];
    reader
        .read(game.checked_add(vector_pointer - 8)?, &mut observed_header)
        .ok()?;
    reader
        .read(game.checked_add(index_offset)?, &mut observed_index)
        .ok()?;
    reader
        .read(hero.checked_add(plan.name)?, &mut observed_name)
        .ok()?;
    (observed_header == header
        && observed_index == index
        && observed_name == raw[plan.name..plan.name + 24])
        .then_some((
            hero,
            Character {
                name,
                count,
                index: selected,
                health_candidate,
                game,
                identity,
            },
        ))
}

pub fn find(session: &Session, stop: &AtomicBool) -> io::Result<Snapshot> {
    let started = Instant::now();
    event(
        "hunt.opening_process",
        format_args!("game_pid={}", session.pid),
    );
    let reader = Reader::open(session)?;
    event(
        "hunt.locating_module",
        format_args!("game_pid={}", session.pid),
    );
    let base = reader.module_base(session)?;
    let file = fs::metadata(&session.path)?;
    event(
        "hunt.reading_image",
        format_args!("game_pid={} bytes={}", session.pid, file.len()),
    );
    let bytes = fs::read(&session.path)?;
    event(
        "hunt.analyzing_image",
        format_args!("game_pid={}", session.pid),
    );
    let plan = analyze(&bytes, stop)?;
    event(
        "hunt.plan_ready",
        format_args!(
            "game_pid={} build={} guards={} elapsed_ms={}",
            session.pid,
            plan.build,
            plan.guards.len(),
            started.elapsed().as_millis()
        ),
    );
    let verify = || -> io::Result<()> {
        let current = fs::metadata(&session.path)?;
        if current.len() != file.len() || current.modified()? != file.modified()? {
            return Err(io::Error::other(
                "Game executable changed during discovery.",
            ));
        }
        for (rva, expected) in &plan.guards {
            let mut raw = vec![0; expected.len()];
            reader.read(
                base.checked_add(*rva)
                    .ok_or_else(|| io::Error::other("Invalid module address."))?,
                &mut raw,
            )?;
            if raw != *expected {
                return Err(io::Error::other("Loaded game instructions changed."));
            }
        }
        Ok(())
    };
    verify()?;
    event(
        "hunt.code_verified",
        format_args!("game_pid={} build={}", session.pid, plan.build),
    );
    let stacks = reader.stacks(session)?;
    let total = stacks.len();
    event(
        "hunt.scanning_stacks",
        format_args!("game_pid={} stacks={total}", session.pid),
    );
    let mut seen = BTreeSet::new();
    let mut found = BTreeMap::new();
    let mut bytes_examined = 0;
    let mut next_report = Instant::now() + Duration::from_secs(1);
    for (stack_index, stack) in stacks.into_iter().enumerate() {
        for start in (stack.start..stack.end).step_by(256 * 1024) {
            cancelled(stop)?;
            let length = (stack.end - start).min(256 * 1024 + 16);
            bytes_examined += (stack.end - start).min(256 * 1024);
            if Instant::now() >= next_report {
                event(
                    "hunt.scan_progress",
                    format_args!(
                        "game_pid={} stack={}/{} bytes_examined={} candidate_headers={} matched_allocations={}",
                        session.pid,
                        stack_index + 1,
                        total,
                        bytes_examined,
                        seen.len(),
                        found.len()
                    ),
                );
                next_report = Instant::now() + Duration::from_secs(1);
            }
            let mut raw = vec![0; length];
            if reader.read(start, &mut raw).is_err() {
                // A guard page can split this chunk. Scan readable pages separately.
                for page in (start..(start + length).min(stack.end)).step_by(4096) {
                    let offset = page - start;
                    let read_length = (length - offset).min(4096);
                    if reader
                        .read(page, &mut raw[offset..offset + read_length])
                        .is_err()
                    {
                        raw[offset..offset + read_length].fill(0);
                    }
                }
            }
            let vector = plan.array[2] - 8;
            for offset in (0..raw.len().saturating_sub(23)).step_by(8) {
                if !(1..=64).contains(&raw[offset])
                    || raw[offset + 1..offset + 8] != [0; 7]
                    || !(1..=4).contains(&raw[offset + 16])
                    || raw[offset + 17..offset + 24] != [0; 7]
                {
                    continue;
                }
                let Some(game) = (start + offset).checked_sub(vector) else {
                    continue;
                };
                if game >= stack.start
                    && game + plan.array[0] + 8 <= stack.end
                    && seen.insert(game)
                    && let Some((address, character)) =
                        snapshot(&reader, &plan, game, &raw[offset..offset + 24])
                {
                    event(
                        "hunt.character_candidate",
                        format_args!(
                            "game_pid={} slot={} participants={}",
                            session.pid,
                            character.index + 1,
                            character.count
                        ),
                    );
                    found.insert(address, character);
                    if found.len() > 1 {
                        return Err(io::Error::other(
                            "Multiple live character allocations matched.",
                        ));
                    }
                }
            }
        }
    }
    verify()?;
    event(
        "hunt.scan_complete",
        format_args!(
            "game_pid={} stacks={total} bytes_examined={} candidate_headers={} matched_allocations={}",
            session.pid,
            bytes_examined,
            seen.len(),
            found.len()
        ),
    );
    let found = found.into_iter().next();
    let damage = found.as_ref().and_then(|(address, character)| {
        prepare_damage(&reader, session, base, &plan, *address, character)
    });
    Ok(Snapshot {
        build: plan.build,
        character: found.map(|(_, character)| character),
        damage,
    })
}

const DAMAGE_MAX: u32 = 9_999;
// First Rust write preview. This fingerprint and these loaded-code proofs were
// independently identified by the verified v0.9.19 named-stat mapper.
const DAMAGE_BUILD: &str = "ab1d6c071b56644d0790a78b73b8c61904d0711836161c19ccd07928fb73b765";
const DAMAGE_GUARDS: &[(usize, usize, &str)] = &[
    (
        0x733f87f,
        3121,
        "596d3667a64f82718a1d9e38a12ae46588259d0f02e086de5633c89a425b75f2",
    ),
    (
        0xad8f2cf,
        180,
        "74fcd67203f838e514c3caa6496f3013622916331f6610587c92f822080753ba",
    ),
    (
        0x7e65ce0,
        68,
        "b24097390346e6a1157d258615a98664327a63b749e11cd25eb15da7e81f2740",
    ),
    (
        0x82fdd80,
        4,
        "2238f30df488055daf5e8f362cf904e370011a172d20df193c1b7ae8e5db8e01",
    ),
    (
        0x6077b10,
        15,
        "996c7a23d204142f6dac72c8e5de8d4c19800c34cdc291e5f8d70e40a6b25c21",
    ),
    (
        0x6078a30,
        18,
        "b816c681e97d089470f390291295f2c7a10be046fc956add12ac74e595dad0bd",
    ),
];

#[derive(Clone, Debug)]
pub struct VerifiedDamage {
    session: Session,
    plan: Plan,
    hero: usize,
    game: usize,
    identity: usize,
    name: String,
    pub current: u32,
}

fn damage_profile(plan: &Plan) -> Option<usize> {
    (plan.digest == DAMAGE_BUILD
        && plan.array == [4768, 3520, 3512, 6800]
        && plan.name == 4880
        && plan.stats == 6520)
        .then_some(6528)
}

fn verify_damage_code(reader: &Reader, base: usize) -> io::Result<()> {
    for &(rva, len, expected) in DAMAGE_GUARDS {
        let mut bytes = vec![0; len];
        reader.read(base + rva, &mut bytes)?;
        if format!("{:x}", Sha256::digest(&bytes)) != expected {
            return Err(io::Error::other(
                "Loaded Damage bonus code failed verification.",
            ));
        }
    }
    Ok(())
}

fn read_damage(reader: &Reader, address: usize) -> io::Result<u32> {
    let mut raw = [0; 4];
    reader.read(address, &mut raw)?;
    Ok(u32::from_le_bytes(raw))
}

fn prepare_damage(
    reader: &Reader,
    session: &Session,
    base: usize,
    plan: &Plan,
    hero: usize,
    character: &Character,
) -> Option<VerifiedDamage> {
    let offset = damage_profile(plan)?;
    if character.count != 1 {
        return None;
    }
    verify_damage_code(reader, base).ok()?;
    let current = read_damage(reader, hero.checked_add(offset)?).ok()?;
    event(
        "apply.damage_ready",
        format_args!("game_pid={} current={current}", session.pid),
    );
    Some(VerifiedDamage {
        session: session.clone(),
        plan: plan.clone(),
        hero,
        game: character.game,
        identity: character.identity,
        name: character.name.clone(),
        current,
    })
}

pub fn apply_damage(proof: &VerifiedDamage, target: u32, cancel: &AtomicBool) -> io::Result<u32> {
    if target > DAMAGE_MAX {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "Enter a whole number from 0 to 9,999.",
        ));
    }
    let offset = damage_profile(&proof.plan)
        .ok_or_else(|| io::Error::other("This build has no verified Rust Damage bonus mapping."))?;
    event(
        "apply.started",
        format_args!("game_pid={} target={target}", proof.session.pid),
    );
    let reader = Reader::open(&proof.session)?;
    let (role_proof, role) = session_role::find(&reader, cancel)?;
    if role != Role::Solo {
        return Err(io::Error::other(
            "Apply is supported only in verified solo runs.",
        ));
    }
    let image = fs::read(&proof.session.path)?;
    if format!("{:x}", Sha256::digest(&image)) != DAMAGE_BUILD {
        return Err(io::Error::other(
            "Game executable changed. Click Find again.",
        ));
    }
    drop(image);
    let base = reader.module_base(&proof.session)?;
    cancelled(cancel)?;
    let writer = DamageWriter::open(&proof.session)?;
    verify_damage_code(&reader, base)?;
    for (rva, expected) in &proof.plan.guards {
        let mut bytes = vec![0; expected.len()];
        reader.read(base + rva, &mut bytes)?;
        if bytes != *expected {
            return Err(io::Error::other(
                "Player identity code changed. Click Find again.",
            ));
        }
    }
    let mut header = [0; 24];
    reader.read(proof.game + proof.plan.array[2] - 8, &mut header)?;
    let current = snapshot(&reader, &proof.plan, proof.game, &header);
    let same_player = current.is_some_and(|(address, character)| {
        address == proof.hero
            && character.identity == proof.identity
            && character.name == proof.name
            && character.count == 1
            && character.index == 0
    });
    if !same_player {
        return Err(io::Error::other(
            "Selected character changed. Click Find again.",
        ));
    }
    let address = proof.hero + offset;
    let previous = read_damage(&reader, address)?;
    if role_proof.read(&reader)? != Role::Solo {
        return Err(io::Error::other("Session changed before Apply."));
    }
    cancelled(cancel)?;
    if previous != target {
        let mut current_header = [0; 24];
        reader.read(proof.game + proof.plan.array[2] - 8, &mut current_header)?;
        let mut current_id = [0; 8];
        let mut index = [0; 8];
        reader.read(proof.hero + 0xF0, &mut current_id)?;
        reader.read(proof.game + proof.plan.array[0], &mut index)?;
        if current_header != header
            || usize::from_le_bytes(current_id) != proof.identity
            || index != [0; 8]
            || read_damage(&reader, address)? != previous
        {
            return Err(io::Error::other(
                "The character or stat changed during Apply. Try again.",
            ));
        }
        writer.write_value(address, target)?;
    }
    let value = read_damage(&reader, address)?;
    if value != target {
        return Err(io::Error::other(
            "Damage bonus did not match the requested value after Apply.",
        ));
    }
    event(
        "apply.verified",
        format_args!(
            "game_pid={} previous={previous} current={value}",
            proof.session.pid
        ),
    );
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_and_truncated_images_are_rejected() {
        assert!(analyze(b"MZ", &AtomicBool::new(false)).is_err());
    }

    #[test]
    fn candidate_search_stops_when_cancelled() {
        assert_eq!(
            cancelled(&AtomicBool::new(true)).unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
    }

    #[test]
    fn damage_mapping_requires_the_full_build_digest_and_matching_layout() {
        let mut plan = Plan {
            build: DAMAGE_BUILD[..12].into(),
            digest: DAMAGE_BUILD.into(),
            array: [4768, 3520, 3512, 6800],
            name: 4880,
            stats: 6520,
            guards: vec![],
        };
        assert_eq!(damage_profile(&plan), Some(6528));
        plan.stats += 4;
        assert!(damage_profile(&plan).is_none());
        plan.stats -= 4;
        plan.digest.pop();
        assert!(damage_profile(&plan).is_none());
    }

    #[test]
    fn relocated_array_requires_both_independent_identity_routines() {
        let mut image = vec![0; 0x400];
        image[..2].copy_from_slice(b"MZ");
        image[0x3c] = 0x80;
        image[0x80..0x88].copy_from_slice(b"PE\0\0\x64\x86\x01\x00");
        image[0x98..0xa0].copy_from_slice(b".text\0\0\0");
        image[0xa4..0xb0].copy_from_slice(&[0, 0x10, 0, 0, 0, 2, 0, 0, 0, 2, 0, 0]);
        let header = b"\x48\x8b\x89\x38\x0e\0\0\x49\x8b\x95\x90\x0b\0\0\x48\x39\xd1\x0f\x83\x64\0\0\0\x49\x8b\x95\x88\x0b\0\0\x48\x69\xc9\x58\x18\0\0\x48\x8d\x1c\x0a";
        let vectors = b"\x0f\x10\x84\x0a\x40\x17\0\0\xf3\x0f\x6f\x8c\x0a\x50\x17\0\0\xf3\x0f\x6f\x94\x0a\x60\x17\0\0\xf3\x0f\x6f\x9c\x0a\x70\x17\0\0";
        let check = b"\x48\x83\xbb\xd8\x11\0\0\xff";
        let drop = b"\x48\x8b\x83\xd8\x11\0\0\x48\x83\xf8\xff\x74\x11\x48\x85\xc0\x74\x0c\x48\x8b\x8b\xe0\x11\0\0\xe8\0\0\0\0";
        for (offset, tail) in [
            (0x200, vectors.as_slice()),
            (0x280, check.as_slice()),
            (0x300, drop.as_slice()),
        ] {
            image[offset..offset + header.len()].copy_from_slice(header);
            image[offset + header.len()..offset + header.len() + tail.len()].copy_from_slice(tail);
        }
        let stop = AtomicBool::new(false);
        let plan = analyze(&image, &stop).unwrap();
        assert_eq!(plan.array, [0xE38, 0xB90, 0xB88, 0x1858]);
        assert_eq!(
            (plan.name, plan.stats, plan.guards.len()),
            (0x11D8, 0x1740, 3)
        );
        image[0x300] = 0;
        assert!(
            analyze(&image, &stop)
                .unwrap_err()
                .to_string()
                .contains("Player identity unavailable")
        );
    }

    #[test]
    fn identity_drop_accepts_indexed_capacity_and_keeps_the_hero_alias() {
        // RAX initially holds the vector pointer and RCX the scaled index.
        // The capacity load replaces RAX, but RBX still holds the hero.
        let owner = Root {
            array: [0x12A0, 0xDC0, 0xDB8, 0x1A90],
            pointer: Register::RAX,
            scaled: Register::RCX,
            hero: Register::RBX,
        };
        let bytes = b"\x48\x8b\x84\x08\x10\x13\0\0\x48\x83\xf8\xff\x74\x11\x48\x85\xc0\x74\x0c\x48\x8b\x8b\x18\x13\0\0\xe8\0\0\0\0";
        let routine: Vec<_> = instructions(bytes, 0x1000, 7).collect();
        assert_eq!(name_drop(owner, &routine), Some(0x1310));

        let clobbered = b"\x48\x8b\x84\x08\x10\x13\0\0\x48\x83\xf8\xff\x74\x12\x48\x85\xc0\x74\x0d\x48\x8b\x8c\x08\x18\x13\0\0\xe8\0\0\0\0";
        let routine: Vec<_> = instructions(clobbered, 0x1000, 7).collect();
        assert_eq!(name_drop(owner, &routine), None);
    }
}
