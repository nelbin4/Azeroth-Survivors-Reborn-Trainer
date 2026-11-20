use std::io;

use sha2::{Digest, Sha256};

use crate::{process::Reader, stats_ui::STATS};

const COMMON: &[Guard] = &[
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
const COMBAT: &[Guard] = &[
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
];
const RESOURCE: &[Guard] = &[
    (
        0x7335bb8,
        303,
        "876c6ba91ab093183063e521ba2cf0b765e0d55a8755a2e9c442c3c4bd1329cc",
    ),
    (
        0xad8f0ea,
        98,
        "103750833f0f5bd29644f12faad883c2aa6448dbbae4ba9e1b1db48d770e2fa2",
    ),
    (
        0x7324129,
        86,
        "e38630e969158985a5417d94d40ea12330b1f5f77afa0612ca12df1e9b101884",
    ),
];
// Independently verified against the resource formatter and selected-player
// resource reader in the newer player layout.
const RESOURCE_CURRENT: &[Guard] = &[
    (
        0x74bc296,
        321,
        "12e8ea584fbdf44e5d6dfd9da1feae5c1d817a3bd9ba79b093d1a49ef5dd37f2",
    ),
    (
        0xaabcb46,
        98,
        "103750833f0f5bd29644f12faad883c2aa6448dbbae4ba9e1b1db48d770e2fa2",
    ),
    (
        0x6084fd0,
        15,
        "996c7a23d204142f6dac72c8e5de8d4c19800c34cdc291e5f8d70e40a6b25c21",
    ),
    (
        0x6085ef0,
        18,
        "b816c681e97d089470f390291295f2c7a10be046fc956add12ac74e595dad0bd",
    ),
    (
        0x74a9899,
        86,
        "a46c36d15c0e1fda6c65d1ca8936798490ab17fac1522a03a94810b1a6bb5649",
    ),
];

// The current formatter inserts an extra argument before Echo. The proof
// includes the formatter, its description, and the scalar formatting routines.
const COMBAT_CURRENT: &[Guard] = &[
    (
        0x74c6a27,
        4112,
        "dff9ae784e000c83625ecbbbb21e7fb5568a2c63536ee3cb48360f1c228a2523",
    ),
    (
        0xaabcd2b,
        190,
        "fe9d71bcab81d18f1d98a008092c984d1c686cb7955155c4aa627204acb41133",
    ),
    (
        0x8058160,
        68,
        "b24097390346e6a1157d258615a98664327a63b749e11cd25eb15da7e81f2740",
    ),
    (
        0x80581b0,
        100,
        "aa8bb21c78776274b9cf958f54703bfa25508ab2c41b7944c57a65edcffb6e0a",
    ),
    (
        0x8501de0,
        4,
        "2238f30df488055daf5e8f362cf904e370011a172d20df193c1b7ae8e5db8e01",
    ),
    (
        0x6084fd0,
        15,
        "996c7a23d204142f6dac72c8e5de8d4c19800c34cdc291e5f8d70e40a6b25c21",
    ),
    (
        0x6085ef0,
        18,
        "b816c681e97d089470f390291295f2c7a10be046fc956add12ac74e595dad0bd",
    ),
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Profile {
    Original,
    Current,
    /// A build whose stat block was found by code analysis. Combat fields sit at
    /// fixed positions inside that block, in the order shared by the builds above.
    Block { stats: usize },
}

// Offsets of the combat stats from the start of the 16-entry stat block. The
// same positions hold in both verified layouts.
const BLOCK_FIELDS: [(&str, usize); 11] = [
    ("damage", 8),
    ("crit", 16),
    ("crit_damage", 20),
    ("speed", 24),
    ("armor", 28),
    ("regen", 32),
    ("pickup", 44),
    ("gold_bonus", 48),
    ("area", 52),
    ("targets", 56),
    ("luck", 60),
];

type Guard = (usize, usize, &'static str);

#[derive(Copy, Clone)]
enum Kind {
    Number { base: u32 },
    Health,
    MaximumHealth,
    Echo,
}

#[derive(Copy, Clone)]
pub struct Field {
    pub key: &'static str,
    offset: usize,
    kind: Kind,
    resource: bool,
}

const fn number(key: &'static str, offset: usize, base: u32, resource: bool) -> Field {
    Field {
        key,
        offset,
        kind: Kind::Number { base },
        resource,
    }
}

// The named-stat and resource proofs were verified by the v0.9.19 mapper
// against game build ab1d6c071b56. Each group rechecks its live instruction bytes.
pub const FIELDS: [Field; 17] = [
    number("damage", 0x1980, 0, false),
    number("crit", 0x1988, 8, false),
    number("crit_damage", 0x198c, 50, false),
    number("cooldown", 0x1984, 0, false),
    number("area", 0x19ac, 0, false),
    number("targets", 0x19b0, 0, false),
    Field {
        key: "echo",
        offset: 0x1880,
        kind: Kind::Echo,
        resource: false,
    },
    Field {
        key: "max_health",
        offset: 0x100,
        kind: Kind::MaximumHealth,
        resource: true,
    },
    Field {
        key: "health",
        offset: 0xf8,
        kind: Kind::Health,
        resource: true,
    },
    number("armor", 0x1994, 0, false),
    number("regen", 0x1998, 0, false),
    number("speed", 0x1990, 0, false),
    number("pickup", 0x19a4, 0, false),
    number("gold_bonus", 0x19a8, 0, false),
    number("luck", 0x19b4, 0, false),
    number("run_gold", 0x164, 0, true),
    number("rerolls", 0x1a3c, 0, true),
];

#[derive(Clone, Copy, Default)]
pub struct Groups {
    pub combat: bool,
    pub resource: bool,
}

impl Groups {
    pub fn map_field(self, profile: Profile, mut field: Field) -> Option<Field> {
        if field.resource {
            if !self.resource || matches!(profile, Profile::Block { .. }) {
                return None; // resource offsets are only proven for the two verified layouts
            }
            if profile == Profile::Current && field.key == "rerolls" {
                field.offset = 7564;
            }
        } else {
            if !self.combat {
                return None;
            }
            if let Profile::Block { stats } = profile {
                let (_, relative) = BLOCK_FIELDS.iter().find(|(key, _)| *key == field.key)?;
                field.offset = stats + relative;
                return Some(field);
            }
            if profile == Profile::Current {
                let relative = match field.key {
                    "damage" => 8,
                    "crit" => 16,
                    "crit_damage" => 20,
                    "speed" => 24,
                    "armor" => 28,
                    "regen" => 32,
                    "pickup" => 44,
                    "gold_bonus" => 48,
                    "area" => 52,
                    "targets" => 56,
                    "luck" => 60,
                    _ => return None,
                };
                field.offset = 7368 + relative;
            }
        }
        Some(field)
    }

    pub fn fields(self, profile: Profile) -> impl Iterator<Item = Field> {
        FIELDS
            .into_iter()
            .filter_map(move |field| self.map_field(profile, field))
    }
}

fn verified(reader: &Reader, base: usize, guards: &[Guard]) -> bool {
    guards.iter().all(|&(rva, len, digest)| {
        let mut bytes = vec![0; len];
        reader.read(base + rva, &mut bytes).is_ok()
            && format!("{:x}", Sha256::digest(&bytes)) == digest
    })
}

pub fn groups(reader: &Reader, base: usize, profile: Profile) -> Groups {
    if let Profile::Block { .. } = profile {
        // The stat block itself was located by code analysis; its position is the proof.
        return Groups {
            combat: true,
            resource: false,
        };
    }
    if profile == Profile::Current {
        return Groups {
            combat: verified(reader, base, COMBAT_CURRENT),
            resource: verified(reader, base, RESOURCE_CURRENT),
        };
    }
    let common = verified(reader, base, COMMON);
    Groups {
        combat: common && verified(reader, base, COMBAT),
        resource: common && verified(reader, base, RESOURCE),
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct Location {
    pub address: usize,
    width: usize,
    echo: Option<[u8; 32]>,
}

#[derive(Clone, Copy)]
pub struct RawValue {
    bytes: [u8; 8],
    width: usize,
}

impl RawValue {
    pub fn bytes(&self) -> &[u8] {
        &self.bytes[..self.width]
    }

    fn u32(self) -> u32 {
        u32::from_le_bytes(self.bytes[..4].try_into().unwrap())
    }

    fn u64(self) -> u64 {
        u64::from_le_bytes(self.bytes)
    }
}

fn word(raw: &[u8], offset: usize) -> usize {
    usize::from_le_bytes(raw[offset..offset + 8].try_into().unwrap())
}

impl Field {
    pub fn locate(&self, reader: &Reader, hero: usize) -> io::Result<Location> {
        let address = hero + self.offset;
        if !matches!(self.kind, Kind::Echo) {
            let width = if matches!(self.kind, Kind::Health | Kind::MaximumHealth) {
                8
            } else {
                4
            };
            return Ok(Location {
                address,
                width,
                echo: None,
            });
        }
        let mut header = [0; 32];
        reader.read(address, &mut header)?;
        let (control, mask, count) = (word(&header, 0), word(&header, 8), word(&header, 24));
        if !(0x10000..0x7fff_ffff_f000).contains(&control)
            || mask >= 1024
            || !(mask + 1).is_power_of_two()
            || !(1..=(mask + 1).min(64)).contains(&count)
        {
            return Err(io::Error::other("Waiting for an active Echo effect."));
        }
        let mut controls = vec![0; mask + 1];
        reader.read(control, &mut controls)?;
        if controls.iter().filter(|&&entry| entry < 0x80).count() != count {
            return Err(io::Error::other("Echo effect table changed."));
        }
        let mut found = None;
        for (index, &entry) in controls.iter().enumerate() {
            if entry >= 0x80 {
                continue;
            }
            let address = control - 8 * (index + 1);
            let mut bucket = [0; 8];
            reader.read(address, &mut bucket)?;
            if bucket[0] == 13 {
                if found.replace(address + 4).is_some() {
                    return Err(io::Error::other("Echo effect is ambiguous."));
                }
                let chance = f32::from_le_bytes(bucket[4..].try_into().unwrap());
                if !(0.0..=1.0).contains(&chance) {
                    return Err(io::Error::other("Echo effect value is invalid."));
                }
            }
        }
        Ok(Location {
            address: found.ok_or_else(|| io::Error::other("Waiting for an active Echo effect."))?,
            width: 4,
            echo: Some(header),
        })
    }

    pub fn read(&self, reader: &Reader, location: &Location) -> io::Result<RawValue> {
        let mut raw = RawValue {
            bytes: [0; 8],
            width: location.width,
        };
        reader.read(location.address, &mut raw.bytes[..raw.width])?;
        Ok(raw)
    }

    pub fn display(&self, raw: RawValue) -> u64 {
        match self.kind {
            Kind::Number { base } => u64::from(raw.u32()) + u64::from(base),
            Kind::Echo => (f32::from_bits(raw.u32()) * 100.0).round() as u64,
            Kind::Health | Kind::MaximumHealth => raw.u64(),
        }
    }

    pub fn encode(&self, value: u32, original: RawValue, max_health: u64) -> io::Result<RawValue> {
        let stat = STATS.iter().find(|stat| stat.key == self.key).unwrap();
        if !stat.valid_target(value) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("{} target is out of range.", stat.label),
            ));
        }
        let native = match self.kind {
            Kind::Number { base } => u64::from(value - base),
            Kind::Echo => u64::from((value as f32 / 100.0).to_bits()),
            Kind::MaximumHealth => original.u64().max(u64::from(value)),
            Kind::Health if (1..=1_000_000_000).contains(&max_health) => max_health,
            Kind::Health => return Err(io::Error::other("Maximum health is unavailable.")),
        };
        Ok(RawValue {
            bytes: native.to_le_bytes(),
            width: original.width,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn newer_resource_profile_exposes_only_proven_fields() {
        let fields: Vec<_> = Groups {
            combat: false,
            resource: true,
        }
        .fields(Profile::Current)
        .map(|field| field.key)
        .collect();
        assert_eq!(fields, ["max_health", "health", "run_gold", "rerolls"]);
    }

    #[test]
    fn current_combat_mapping_uses_verified_slots_only() {
        let fields: Vec<_> = Groups {
            combat: true,
            resource: false,
        }
        .fields(Profile::Current)
        .map(|field| (field.key, field.offset - 7368))
        .collect();
        assert_eq!(
            fields,
            [
                ("damage", 8),
                ("crit", 16),
                ("crit_damage", 20),
                ("area", 52),
                ("targets", 56),
                ("armor", 28),
                ("regen", 32),
                ("speed", 24),
                ("pickup", 44),
                ("gold_bonus", 48),
                ("luck", 60),
            ]
        );
    }

    #[test]
    fn block_layout_places_combat_fields_like_the_verified_builds() {
        let combat = Groups {
            combat: true,
            resource: false,
        };
        let original: Vec<_> = combat
            .fields(Profile::Original)
            .filter(|field| BLOCK_FIELDS.iter().any(|(key, _)| *key == field.key))
            .map(|field| (field.key, field.offset - 6520))
            .collect();
        assert_eq!(original.len(), BLOCK_FIELDS.len());
        let block: Vec<_> = combat
            .fields(Profile::Block { stats: 6520 })
            .map(|field| (field.key, field.offset - 6520))
            .collect();
        assert_eq!(block.len(), BLOCK_FIELDS.len());
        for (key, offset) in &block {
            assert!(
                original.contains(&(*key, *offset)),
                "{key} differs from the verified layout"
            );
        }
        let current: Vec<_> = combat
            .fields(Profile::Current)
            .map(|field| (field.key, field.offset - 7368))
            .collect();
        for (key, offset) in &current {
            assert!(block.contains(&(*key, *offset)), "{key} differs from Current");
        }
    }

    #[test]
    fn block_layout_leaves_unproven_fields_unmapped() {
        let block = Profile::Block { stats: 0x1000 };
        let resource = Groups {
            combat: false,
            resource: true,
        };
        assert_eq!(resource.fields(block).count(), 0);
        let fields: Vec<_> = Groups {
            combat: true,
            resource: false,
        }
        .fields(block)
        .map(|field| field.key)
        .collect();
        assert!(!fields.contains(&"echo"));
        assert!(!fields.contains(&"cooldown"));
        assert_eq!(fields.len(), BLOCK_FIELDS.len());
    }

    #[test]
    fn writes_display_targets_in_native_units() {
        let raw = RawValue {
            bytes: 3u64.to_le_bytes(),
            width: 4,
        };
        let crit = FIELDS.iter().find(|field| field.key == "crit").unwrap();
        assert_eq!(crit.display(raw), 11);
        assert_eq!(crit.encode(42, raw, 0).unwrap().u32(), 34);
        assert!(crit.encode(7, raw, 0).is_err());
        let echo = FIELDS.iter().find(|field| field.key == "echo").unwrap();
        assert_eq!(f32::from_bits(echo.encode(50, raw, 0).unwrap().u32()), 0.5);
    }

    #[test]
    fn health_follows_verified_maximum_without_lowering_it() {
        let raw = RawValue {
            bytes: 725u64.to_le_bytes(),
            width: 8,
        };
        let max = FIELDS
            .iter()
            .find(|field| field.key == "max_health")
            .unwrap();
        let health = FIELDS.iter().find(|field| field.key == "health").unwrap();
        assert_eq!(max.encode(500, raw, 0).unwrap().u64(), 725);
        assert_eq!(health.encode(0, raw, 750).unwrap().u64(), 750);
        assert!(health.encode(0, raw, 0).is_err());
    }
}
