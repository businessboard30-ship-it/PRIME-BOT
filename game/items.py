"""
Heist Wars — item/equipment/cosmetic content (data-driven, code is source of
truth — same pattern as game/locations.py).

Two hard rules enforced structurally here, not just by convention:

  1. Cosmetic items (SKIN/MASK/BADGE/THEME) can never carry an `effects`
     dict — ItemDefinition.__post_init__ asserts this. There is no code
     path anywhere that could accidentally let a skin affect success
     chance, because a cosmetic item object literally cannot hold a
     modifier.
  2. Every gameplay item's effect is capped per-rarity at RARITY_MAX_MODIFIER
     and the *global* per-run total is capped separately in item_engine.py
     (ITEM_MODIFIER_CAP) — no single item, and no combination of equipped
     items, can push a run's item_modifier past that ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from game.models import HeistState


class Rarity(str, Enum):
    COMMON = "common"
    UNCOMMON = "uncommon"
    RARE = "rare"
    EPIC = "epic"
    LEGENDARY = "legendary"
    MYTHIC = "mythic"


RARITY_ORDER: dict[Rarity, int] = {
    Rarity.COMMON: 0, Rarity.UNCOMMON: 1, Rarity.RARE: 2,
    Rarity.EPIC: 3, Rarity.LEGENDARY: 4, Rarity.MYTHIC: 5,
}

RARITY_LABEL: dict[Rarity, str] = {
    Rarity.COMMON: "COMMON", Rarity.UNCOMMON: "UNCOMMON", Rarity.RARE: "RARE",
    Rarity.EPIC: "EPIC", Rarity.LEGENDARY: "LEGENDARY", Rarity.MYTHIC: "MYTHIC",
}

# Maximum |modifier| a single gameplay item of this rarity may define.
# Enforced at content-definition time (ItemDefinition.__post_init__), so a
# miswritten item in this file fails fast at import instead of silently
# shipping an overpowered tool.
RARITY_MAX_MODIFIER: dict[Rarity, int] = {
    Rarity.COMMON: 2, Rarity.UNCOMMON: 3, Rarity.RARE: 4,
    Rarity.EPIC: 5, Rarity.LEGENDARY: 6, Rarity.MYTHIC: 7,
}

# Weighted drop table for heist-reward item rolls (game/item_engine.resolve_drop).
# Values are relative weights, not required to sum to 100.
RARITY_DROP_WEIGHTS: dict[Rarity, int] = {
    Rarity.COMMON: 60, Rarity.UNCOMMON: 25, Rarity.RARE: 10,
    Rarity.EPIC: 4, Rarity.LEGENDARY: 1, Rarity.MYTHIC: 0,  # mythic: not obtainable via random drop in v1
}


class Category(str, Enum):
    TOOL = "tool"
    EQUIPMENT = "equipment"
    SKIN = "skin"
    MASK = "mask"
    BADGE = "badge"
    THEME = "theme"


GAMEPLAY_CATEGORIES = frozenset({Category.TOOL, Category.EQUIPMENT})
COSMETIC_CATEGORIES = frozenset({Category.SKIN, Category.MASK, Category.BADGE, Category.THEME})


class EffectType(str, Enum):
    SUCCESS_MODIFIER = "success_modifier"      # applies during a specific phase
    EVENT_CHOICE_UNLOCK = "event_choice_unlock"  # reserved for v2 content
    REWARD_MODIFIER = "reward_modifier"          # % bonus to cash reward on success
    XP_MODIFIER = "xp_modifier"                  # % bonus to xp reward on success


@dataclass(frozen=True)
class ItemEffect:
    type: EffectType
    phase: HeistState | None  # which phase this applies to (None = ESCAPE/global, for REWARD/XP modifiers)
    magnitude: int            # percentage points or success-chance points, always positive; sign is implied by type

    def __post_init__(self):
        assert self.magnitude >= 0
        if self.type == EffectType.SUCCESS_MODIFIER:
            assert self.phase is not None, "SUCCESS_MODIFIER effects must name a phase"


@dataclass(frozen=True)
class ItemDefinition:
    key: str
    name: str
    category: Category
    rarity: Rarity
    description: str
    flavor: str = ""
    required_level: int = 1
    stackable: bool = False          # True = consumable/quantity item, False = owned once
    effects: tuple[ItemEffect, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.category in COSMETIC_CATEGORIES:
            assert not self.effects, f"{self.key}: cosmetic items must not define gameplay effects"
        if self.category in GAMEPLAY_CATEGORIES:
            cap = RARITY_MAX_MODIFIER[self.rarity]
            for eff in self.effects:
                assert eff.magnitude <= cap, (
                    f"{self.key}: effect magnitude {eff.magnitude} exceeds "
                    f"{self.rarity.value} cap of {cap}"
                )

    @property
    def is_cosmetic(self) -> bool:
        return self.category in COSMETIC_CATEGORIES


# ---------------------------------------------------------------------------
# v1 catalog
# ---------------------------------------------------------------------------

ITEMS: dict[str, ItemDefinition] = {
    it.key: it
    for it in [
        # -- Tools (gameplay, contextual) --------------------------------
        ItemDefinition(
            key="lockpick_kit",
            name="Lockpick Kit",
            category=Category.TOOL,
            rarity=Rarity.COMMON,
            description="A compact kit for bypassing simple locks.",
            flavor="Standard-issue. Every operative starts somewhere.",
            required_level=1,
            effects=(ItemEffect(EffectType.SUCCESS_MODIFIER, HeistState.INFILTRATION, 2),),
        ),
        ItemDefinition(
            key="signal_jammer",
            name="Signal Jammer",
            category=Category.TOOL,
            rarity=Rarity.UNCOMMON,
            description="Disrupts local surveillance networks during entry.",
            flavor="Disrupt local surveillance networks.",
            required_level=3,
            effects=(ItemEffect(EffectType.SUCCESS_MODIFIER, HeistState.INFILTRATION, 3),),
        ),
        ItemDefinition(
            key="disguise_kit",
            name="Disguise Kit",
            category=Category.TOOL,
            rarity=Rarity.UNCOMMON,
            description="Convincing enough to buy a few extra seconds from a guard.",
            flavor="Nobody looks twice at a uniform.",
            required_level=5,
            effects=(ItemEffect(EffectType.SUCCESS_MODIFIER, HeistState.OBJECTIVE, 3),),
        ),
        ItemDefinition(
            key="vault_scanner",
            name="Vault Scanner",
            category=Category.TOOL,
            rarity=Rarity.RARE,
            description="Reads a vault's internals before you commit to cracking it.",
            flavor="See the lock before you touch it.",
            required_level=8,
            effects=(ItemEffect(EffectType.SUCCESS_MODIFIER, HeistState.OBJECTIVE, 4),),
        ),
        ItemDefinition(
            key="grappling_line",
            name="Grappling Line",
            category=Category.TOOL,
            rarity=Rarity.RARE,
            description="A retractable line rated for a fast, clean extraction.",
            flavor="Up, over, gone.",
            required_level=8,
            effects=(ItemEffect(EffectType.SUCCESS_MODIFIER, HeistState.LOOT, 4),),
        ),
        ItemDefinition(
            key="thermal_cutter",
            name="Thermal Cutter",
            category=Category.EQUIPMENT,
            rarity=Rarity.EPIC,
            description="Cuts through reinforced vault doors in seconds.",
            flavor="Nothing stays locked forever.",
            required_level=15,
            effects=(ItemEffect(EffectType.SUCCESS_MODIFIER, HeistState.OBJECTIVE, 5),),
        ),
        ItemDefinition(
            key="ghost_protocol_kit",
            name="Ghost Protocol Kit",
            category=Category.EQUIPMENT,
            rarity=Rarity.LEGENDARY,
            description="A full-spectrum evasion loadout for operatives who don't exist.",
            flavor="Built for operators who don't exist.",
            required_level=20,
            effects=(ItemEffect(EffectType.SUCCESS_MODIFIER, HeistState.LOOT, 6),),
        ),
        # -- Reward-modifier equipment (global, not phase-scoped) --------
        ItemDefinition(
            key="fence_contact",
            name="Fence Contact",
            category=Category.EQUIPMENT,
            rarity=Rarity.RARE,
            description="A reliable buyer who never lowballs you.",
            flavor="Everything has a price, if you know who to ask.",
            required_level=10,
            effects=(ItemEffect(EffectType.REWARD_MODIFIER, None, 4),),
        ),
        # -- Cosmetics (no gameplay effect, ever) ------------------------
        ItemDefinition(
            key="street_runner_skin",
            name="Street Runner",
            category=Category.SKIN,
            rarity=Rarity.COMMON,
            description="Clean and functional. No gameplay effect.",
            flavor="Fast, quiet, forgettable.",
        ),
        ItemDefinition(
            key="neon_ghost_skin",
            name="Neon Ghost",
            category=Category.SKIN,
            rarity=Rarity.RARE,
            description="Cosmetic only. No gameplay effect.",
            flavor="Seen for a second. Gone the next.",
        ),
        ItemDefinition(
            key="neon_phantom_skin",
            name="Neon Phantom",
            category=Category.SKIN,
            rarity=Rarity.LEGENDARY,
            description="Cosmetic only. No gameplay effect.",
            flavor="Built for operators who don't exist.",
        ),
        ItemDefinition(
            key="shadow_agent_mask",
            name="Shadow Agent",
            category=Category.MASK,
            rarity=Rarity.EPIC,
            description="Cosmetic only. No gameplay effect.",
            flavor="No face. No name. No trace.",
        ),
        ItemDefinition(
            key="first_score_badge",
            name="First Score",
            category=Category.BADGE,
            rarity=Rarity.COMMON,
            description="Awarded for your first completed heist. Cosmetic only.",
            flavor="Everyone remembers the first one.",
        ),
    ]
}


def get_item(key: str) -> ItemDefinition | None:
    return ITEMS.get(key)


def items_by_category(category: Category) -> list[ItemDefinition]:
    return [it for it in ITEMS.values() if it.category == category]


def gameplay_items() -> list[ItemDefinition]:
    return [it for it in ITEMS.values() if it.category in GAMEPLAY_CATEGORIES]


def droppable_items() -> list[ItemDefinition]:
    """Items eligible to drop from a successful heist (gameplay items only —
    cosmetics are unlocked through other systems, not random heist drops, in v1)."""
    return gameplay_items()
