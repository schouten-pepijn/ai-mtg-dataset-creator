from __future__ import annotations

import csv
from enum import Enum
import gzip
import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Annotated, Any, Iterable

import requests
import typer


SCRYFALL_BULK_DEFAULT_CARDS_URL = "https://api.scryfall.com/bulk-data/default-cards"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


COLOR_ALIASES: dict[str, set[str]] = {
    "w": {"W"},
    "u": {"U"},
    "b": {"B"},
    "r": {"R"},
    "g": {"G"},
    "white": {"W"},
    "blue": {"U"},
    "black": {"B"},
    "red": {"R"},
    "green": {"G"},
    "azorius": {"W", "U"},
    "dimir": {"U", "B"},
    "rakdos": {"B", "R"},
    "gruul": {"R", "G"},
    "selesnya": {"G", "W"},
    "orzhov": {"W", "B"},
    "izzet": {"U", "R"},
    "golgari": {"B", "G"},
    "boros": {"R", "W"},
    "simic": {"G", "U"},
    "esper": {"W", "U", "B"},
    "grixis": {"U", "B", "R"},
    "jund": {"B", "R", "G"},
    "naya": {"R", "G", "W"},
    "bant": {"G", "W", "U"},
    "mardu": {"R", "W", "B"},
    "temur": {"G", "U", "R"},
    "abzan": {"W", "B", "G"},
    "jeskai": {"U", "R", "W"},
    "sultai": {"B", "G", "U"},
    "colorless": set(),
    "c": set(),
    "all": {"W", "U", "B", "R", "G"},
    "fivecolor": {"W", "U", "B", "R", "G"},
    "wubrg": {"W", "U", "B", "R", "G"},
}

ROLE_PATTERNS: dict[str, str] = {
    # Heuristic role tags are prompt helpers only; Scryfall remains the source
    # for rules text, types, and legalities.
    "counterspell": r"\bcounter target\b|\bcounter .* spell\b",
    "single_target_removal": (
        r"\bdestroy target\b|"
        r"\bexile target\b|"
        r"\breturn target .* to (its owner's )?hand\b|"
        r"\bdeals? \d+ damage to (any target|target creature|target planeswalker)"
    ),
    "board_wipe": (
        r"\bdestroy all\b|"
        r"\bexile all\b|"
        r"\bdestroy each\b|"
        r"\bexile each\b|"
        r"\ball creatures\b|"
        r"\beach creature\b|"
        r"\beach nonland permanent\b"
    ),
    "card_draw": (
        r"\bdraw (a|one|two|three|x|that many)? ?cards?\b|"
        r"\bwhenever .* draw\b|"
        r"\binvestigate\b"
    ),
    "impulse_draw": r"\bexile the top\b.*\bplay\b|\bexile .* from the top\b.*\bcast\b",
    "ramp": (
        r"\bsearch your library .* land\b|"
        r"\bput .* land .* battlefield\b|"
        r"\badd .* mana\b|"
        r"\btreasure token\b"
    ),
    "token_maker": r"\bcreate .* token\b",
    "blink": r"\bexile .* return .* battlefield\b|\bflicker\b",
    "recursion": (
        r"\breturn .* from your graveyard\b|"
        r"\bcast .* from your graveyard\b|"
        r"\bplay .* from your graveyard\b"
    ),
    "self_mill": r"\bmill\b|\bsurveil\b",
    "sacrifice": r"\bsacrifice\b",
    "sacrifice_outlet": r"\bsacrifice (a|another|one|this|an|target)? ?.*:",
    "lifegain": r"\bgain .* life\b",
    "protection": (
        r"\bindestructible\b|"
        r"\bhexproof\b|"
        r"\bward\b|"
        r"\bprotection from\b|"
        r"\bphase out\b|"
        r"\bprevent all damage\b"
    ),
    "anthem": r"\bcreatures you control get\b|\bother creatures you control get\b",
    "tap_control": r"\btap target\b|\bdoesn't untap\b|\btap up to\b",
    "untap_synergy": r"\buntap\b",
    "equipment_synergy": r"\bequipment\b|\battach\b|\bequip\b",
    "vehicle_synergy": r"\bvehicle\b|\bcrew\b",
    "graveyard_synergy": r"\bgraveyard\b|\bdescend\b|\bdelirium\b|\bescape\b|flashback",
    "spell_copy": r"\bcopy target\b|\bcopy .* spell\b",
    "spellslinger": r"\bwhenever you cast .* instant\b|\bwhenever you cast .* sorcery\b",
    "cost_reduction": r"\bcosts? .* less to cast\b",
    "stax": r"\bcan't attack\b|\bcan't block\b|\bcan't cast\b|\bplayers can't\b|\bopponents can't\b",
}


def normalize_key(value: str) -> str:
    return (
        value.strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("/", "_")
    )


def parse_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return default


def parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None


def parse_boolish(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"true", "1", "yes", "y", "foil"}


def is_missing_text(value: Any) -> bool:
    if value is None:
        return True
    # EDHREC exports include separator rows where every field is "undefined".
    return str(value).strip().lower() in {"", "undefined", "none", "null"}


def clean_text(value: Any) -> str | None:
    if is_missing_text(value):
        return None
    return str(value).strip()


def normalize_name_for_match(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def parse_commander_colors(value: str) -> set[str]:
    raw = value.strip().lower().replace(" ", "")

    if raw in COLOR_ALIASES:
        return set(COLOR_ALIASES[raw])

    raw_upper = raw.upper()
    allowed = {"W", "U", "B", "R", "G"}

    if all(char in allowed for char in raw_upper):
        return set(raw_upper)

    raise ValueError(
        f"Invalid commander colors: {value!r}. "
        "Use examples like WU, UB, WUBRG, azorius, esper, colorless."
    )


def color_slug(colors: set[str]) -> str:
    order = ["W", "U", "B", "R", "G"]
    if not colors:
        return "C"
    return "".join(c for c in order if c in colors)


def read_manabox_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = []

        for row in reader:
            normalized = {normalize_key(k): v for k, v in row.items()}
            rows.append(normalized)

    return rows


def parse_edhrec_candidate(row: dict[str, Any], row_number: int) -> dict[str, Any] | None:
    name = clean_text(row.get("name"))
    if name is None:
        return None

    # Treat EDHREC as a recommendation signal. Rules fields are filled later
    # from the local Scryfall bulk data after the name match succeeds.
    decks = parse_int(row.get("decks"), default=0)
    potential_decks = parse_int(row.get("potential_decks"), default=0)

    return {
        "source": "edhrec_csv",
        "source_row_numbers": [row_number],
        "name": name,
        "colors": clean_text(row.get("colors")),
        "cmc": parse_float(row.get("cmc")),
        "rarity": clean_text(row.get("rarity")),
        "type": clean_text(row.get("type")),
        "market_prices": {
            "card_kingdom": parse_float(row.get("card_kingdom")),
            "tcgplayer": parse_float(row.get("tcgplayer")),
            "face_to_face": parse_float(row.get("face_to_face")),
            "cardmarket": parse_float(row.get("cardmarket")),
            "cardhoarder": parse_float(row.get("cardhoarder")),
        },
        "salt": parse_float(row.get("salt")),
        "decks": decks,
        "inclusion": parse_int(row.get("inclusion"), default=0),
        "potential_decks": potential_decks,
        "inclusion_rate": (
            round(decks / potential_decks, 6) if potential_decks else None
        ),
        "synergy": parse_float(row.get("synergy")),
    }


def prefer_edhrec_candidate(
    current: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    # The same card can appear in multiple EDHREC sections; keep the strongest
    # recommendation row while preserving source row numbers for traceability.
    current_score = (
        parse_float(current.get("synergy")) or 0,
        parse_int(current.get("decks"), default=0),
    )
    candidate_score = (
        parse_float(candidate.get("synergy")) or 0,
        parse_int(candidate.get("decks"), default=0),
    )

    preferred = candidate if candidate_score > current_score else current
    preferred["source_row_numbers"] = sorted(
        {
            *current.get("source_row_numbers", []),
            *candidate.get("source_row_numbers", []),
        }
    )
    preferred["duplicate_source_count"] = len(preferred["source_row_numbers"])
    return preferred


def read_edhrec_candidates(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        candidates_by_name: dict[str, dict[str, Any]] = {}

        for row_number, row in enumerate(reader, start=2):
            normalized_row = {normalize_key(k): v for k, v in row.items()}
            candidate = parse_edhrec_candidate(normalized_row, row_number)
            if candidate is None:
                continue

            name_key = normalize_name_for_match(candidate["name"])
            if name_key in candidates_by_name:
                candidates_by_name[name_key] = prefer_edhrec_candidate(
                    candidates_by_name[name_key],
                    candidate,
                )
                continue

            candidate["duplicate_source_count"] = 1
            candidates_by_name[name_key] = candidate

    return list(candidates_by_name.values())


def open_text(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def load_scryfall_cards(path: Path) -> list[dict[str, Any]]:
    """
    Supports:
    - Scryfall default-cards JSON array
    - JSONL file with one Scryfall card object per line
    - .gz variants
    """
    with open_text(path) as f:
        first = f.read(1)
        f.seek(0)

        # Scryfall bulk downloads are JSON arrays, but JSONL is useful for
        # locally converted files. Detect the shape instead of relying on suffix.
        if first == "[":
            data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("Expected Scryfall JSON array.")
            return data

        cards = []
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                cards.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSONL at line {line_number} in {path}"
                ) from exc

        return cards


def download_scryfall_default_cards(out_path: Path) -> Path:
    headers = {
        "User-Agent": "manabox-ai-dataset-builder/1.0",
        "Accept": "application/json",
    }

    logging.info("Fetching Scryfall bulk-data metadata.")
    metadata_response = requests.get(
        SCRYFALL_BULK_DEFAULT_CARDS_URL,
        headers=headers,
        timeout=30,
    )
    metadata_response.raise_for_status()
    download_uri = metadata_response.json()["download_uri"]

    logging.info("Downloading Scryfall default-cards bulk file.")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with requests.get(download_uri, headers=headers, stream=True, timeout=300) as response:
        response.raise_for_status()
        with out_path.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    return out_path


def get_card_faces(card: dict[str, Any]) -> list[dict[str, Any]]:
    faces = card.get("card_faces") or []

    result = []
    for face in faces:
        result.append(
            {
                "name": face.get("name"),
                "mana_cost": face.get("mana_cost") or "",
                "type_line": face.get("type_line") or "",
                "oracle_text": face.get("oracle_text") or "",
                "colors": face.get("colors") or [],
                "power": face.get("power"),
                "toughness": face.get("toughness"),
                "loyalty": face.get("loyalty"),
                "defense": face.get("defense"),
            }
        )

    return result


def get_combined_oracle_text(card: dict[str, Any]) -> str:
    pieces: list[str] = []

    if card.get("oracle_text"):
        pieces.append(card["oracle_text"])

    # Double-faced and split-style cards often store the useful rules text on
    # card faces, so include face labels to keep the AI context understandable.
    for face in get_card_faces(card):
        face_text = face.get("oracle_text") or ""
        if face_text:
            pieces.append(
                "\n".join(
                    part
                    for part in [
                        str(face.get("name") or ""),
                        str(face.get("type_line") or ""),
                        face_text,
                    ]
                    if part
                )
            )

    return "\n\n".join(pieces).strip()


def get_combined_mana_cost(card: dict[str, Any]) -> str:
    if card.get("mana_cost"):
        return card["mana_cost"]

    faces = get_card_faces(card)
    costs = [face["mana_cost"] for face in faces if face.get("mana_cost")]
    return " // ".join(costs)


def type_contains(type_line: str, value: str) -> bool:
    return value.lower() in type_line.lower()


def has_land_face(card: dict[str, Any]) -> bool:
    if type_contains(card.get("type_line") or "", "Land"):
        return True

    for face in get_card_faces(card):
        if type_contains(face.get("type_line") or "", "Land"):
            return True

    return False


def has_spell_face(card: dict[str, Any]) -> bool:
    faces = get_card_faces(card)
    if not faces:
        return not type_contains(card.get("type_line") or "", "Land")

    return any(
        not type_contains(face.get("type_line") or "", "Land")
        for face in faces
    )


def detect_special_copy_rule(card: dict[str, Any]) -> str:
    type_line = card.get("type_line") or ""
    text = get_combined_oracle_text(card).lower()

    if "basic land" in type_line.lower():
        return "basic_land_unlimited"

    if "a deck can have any number of cards named" in text:
        return "any_number_allowed_by_card_text"

    if re.search(r"a deck can have up to \w+ cards named", text):
        return "limited_number_allowed_by_card_text"

    return "commander_singleton"


def classify_roles(card: dict[str, Any]) -> list[str]:
    type_line = card.get("type_line") or ""
    oracle_text = get_combined_oracle_text(card)
    text = f"{type_line}\n{oracle_text}".lower()
    tags: set[str] = set()

    type_tag_map = {
        "Artifact": "artifact",
        "Battle": "battle",
        "Creature": "creature",
        "Enchantment": "enchantment",
        "Instant": "instant",
        "Land": "land",
        "Planeswalker": "planeswalker",
        "Sorcery": "sorcery",
        "Equipment": "equipment",
        "Vehicle": "vehicle",
        "Aura": "aura",
        "Saga": "saga",
    }

    for type_fragment, tag in type_tag_map.items():
        if type_contains(type_line, type_fragment):
            tags.add(tag)

    if "basic land" in type_line.lower():
        tags.add("basic_land")

    if has_land_face(card) and has_spell_face(card):
        tags.add("modal_land_or_spell")

    produced_mana = set(card.get("produced_mana") or [])
    if produced_mana:
        tags.add("produces_mana")

    if type_contains(type_line, "Artifact") and produced_mana:
        tags.add("mana_rock")

    if type_contains(type_line, "Land"):
        if len(produced_mana) >= 2 or "any color" in text:
            tags.add("fixing_land")
        if "enters tapped" in text:
            tags.add("enters_tapped_land")

    if "when " in text and " enters" in text:
        tags.add("etb")

    if "dies" in text or "whenever another creature dies" in text:
        tags.add("death_trigger")

    for keyword in card.get("keywords") or []:
        tags.add(f"keyword:{str(keyword).lower().replace(' ', '_')}")

    for tag, pattern in ROLE_PATTERNS.items():
        if re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
            tags.add(tag)

    return sorted(tags)


def extract_prices(card: dict[str, Any]) -> dict[str, Any]:
    prices = card.get("prices") or {}
    return {
        "usd": prices.get("usd"),
        "usd_foil": prices.get("usd_foil"),
        "usd_etched": prices.get("usd_etched"),
        "eur": prices.get("eur"),
        "eur_foil": prices.get("eur_foil"),
        "tix": prices.get("tix"),
    }


def get_image_uri(card: dict[str, Any]) -> str | None:
    image_uris = card.get("image_uris") or {}
    if image_uris.get("normal"):
        return image_uris["normal"]

    faces = card.get("card_faces") or []
    for face in faces:
        face_images = face.get("image_uris") or {}
        if face_images.get("normal"):
            return face_images["normal"]

    return None


def build_common_card_fields(card: dict[str, Any]) -> dict[str, Any]:
    type_line = card.get("type_line") or ""
    oracle_text = get_combined_oracle_text(card)
    color_identity = card.get("color_identity") or []
    produced_mana = card.get("produced_mana") or []
    legalities = card.get("legalities") or {}
    special_copy_rule = detect_special_copy_rule(card)

    is_basic_land = "basic land" in type_line.lower()
    commander_legal = legalities.get("commander") == "legal"

    return {
        "scryfall_id": card.get("id"),
        "oracle_id": card.get("oracle_id"),
        "name": card.get("name"),
        "layout": card.get("layout"),
        "mana_cost": get_combined_mana_cost(card),
        "mana_value": card.get("cmc"),
        "type_line": type_line,
        "oracle_text": oracle_text,
        "colors": card.get("colors") or [],
        "color_identity": color_identity,
        "keywords": card.get("keywords") or [],
        "produced_mana": produced_mana,
        "power": card.get("power"),
        "toughness": card.get("toughness"),
        "loyalty": card.get("loyalty"),
        "defense": card.get("defense"),
        "faces": get_card_faces(card),
        "is_double_faced": bool(card.get("card_faces")),
        "has_land_face": has_land_face(card),
        "has_spell_face": has_spell_face(card),
        "is_land": type_contains(type_line, "Land"),
        "is_basic_land": is_basic_land,
        "is_nonbasic_land": type_contains(type_line, "Land") and not is_basic_land,
        "is_creature": type_contains(type_line, "Creature"),
        "is_artifact": type_contains(type_line, "Artifact"),
        "is_enchantment": type_contains(type_line, "Enchantment"),
        "is_instant": type_contains(type_line, "Instant"),
        "is_sorcery": type_contains(type_line, "Sorcery"),
        "is_planeswalker": type_contains(type_line, "Planeswalker"),
        "is_battle": type_contains(type_line, "Battle"),
        "is_equipment": type_contains(type_line, "Equipment"),
        "is_vehicle": type_contains(type_line, "Vehicle"),
        "is_aura": type_contains(type_line, "Aura"),
        "is_saga": type_contains(type_line, "Saga"),
        "commander_legal": commander_legal,
        "legalities": legalities,
        "deck_copy_rule": special_copy_rule,
        "reserved": card.get("reserved"),
        "digital": card.get("digital"),
        "game_changer": card.get("game_changer"),
        "rarity": card.get("rarity"),
        "set": card.get("set"),
        "set_name": card.get("set_name"),
        "collector_number": card.get("collector_number"),
        "released_at": card.get("released_at"),
        "edhrec_rank": card.get("edhrec_rank"),
        "prices": extract_prices(card),
        "eur_price": extract_prices(card).get("eur"),
        "scryfall_uri": card.get("scryfall_uri"),
        "image_uri_normal": get_image_uri(card),
        "tags": classify_roles(card),
    }


def build_printing_row(manabox_row: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    common = build_common_card_fields(card)

    quantity = parse_int(manabox_row.get("quantity"), default=1)

    return {
        **common,
        "quantity_owned": quantity,
        "manabox": {
            "manabox_id": manabox_row.get("manabox_id"),
            "name": manabox_row.get("name"),
            "set_code": manabox_row.get("set_code"),
            "set_name": manabox_row.get("set_name"),
            "collector_number": manabox_row.get("collector_number"),
            "foil": manabox_row.get("foil"),
            "is_foil": parse_boolish(manabox_row.get("foil")),
            "rarity": manabox_row.get("rarity"),
            "condition": manabox_row.get("condition"),
            "language": manabox_row.get("language"),
            "purchase_price": parse_float(manabox_row.get("purchase_price")),
            "purchase_price_currency": manabox_row.get("purchase_price_currency"),
            "misprint": parse_boolish(manabox_row.get("misprint")),
            "altered": parse_boolish(manabox_row.get("altered")),
            "added": manabox_row.get("added"),
        },
    }


def aggregate_by_oracle_id(printing_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in printing_rows:
        group_key = row.get("oracle_id") or row.get("scryfall_id")
        if not group_key:
            continue
        groups[str(group_key)].append(row)

    aggregated: list[dict[str, Any]] = []

    for oracle_id, rows in groups.items():
        canonical = rows[0]
        total_owned = sum(parse_int(row.get("quantity_owned"), default=0) for row in rows)

        copy_rule = canonical.get("deck_copy_rule")
        if copy_rule in {
            "basic_land_unlimited",
            "any_number_allowed_by_card_text",
            "limited_number_allowed_by_card_text",
        }:
            commander_owned_playable_copies = total_owned
        else:
            commander_owned_playable_copies = min(total_owned, 1)

        all_tags = sorted({tag for row in rows for tag in row.get("tags", [])})
        all_scryfall_ids = sorted({str(row["scryfall_id"]) for row in rows if row.get("scryfall_id")})

        owned_printings = []
        for row in rows:
            manabox = row.get("manabox") or {}
            owned_printings.append(
                {
                    "scryfall_id": row.get("scryfall_id"),
                    "set": row.get("set"),
                    "set_name": row.get("set_name"),
                    "collector_number": row.get("collector_number"),
                    "quantity": row.get("quantity_owned"),
                    "foil": manabox.get("foil"),
                    "is_foil": manabox.get("is_foil"),
                    "condition": manabox.get("condition"),
                    "language": manabox.get("language"),
                    "purchase_price": manabox.get("purchase_price"),
                    "purchase_price_currency": manabox.get("purchase_price_currency"),
                    "added": manabox.get("added"),
                }
            )

        aggregated.append(
            {
                "oracle_id": oracle_id,
                "name": canonical.get("name"),
                "total_owned": total_owned,
                "commander_owned_playable_copies": commander_owned_playable_copies,
                "owned_printing_count": len(rows),
                "scryfall_ids": all_scryfall_ids,
                "mana_cost": canonical.get("mana_cost"),
                "mana_value": canonical.get("mana_value"),
                "type_line": canonical.get("type_line"),
                "oracle_text": canonical.get("oracle_text"),
                "colors": canonical.get("colors"),
                "color_identity": canonical.get("color_identity"),
                "keywords": canonical.get("keywords"),
                "produced_mana": canonical.get("produced_mana"),
                "power": canonical.get("power"),
                "toughness": canonical.get("toughness"),
                "loyalty": canonical.get("loyalty"),
                "defense": canonical.get("defense"),
                "layout": canonical.get("layout"),
                "faces": canonical.get("faces"),
                "is_double_faced": canonical.get("is_double_faced"),
                "has_land_face": canonical.get("has_land_face"),
                "has_spell_face": canonical.get("has_spell_face"),
                "is_land": canonical.get("is_land"),
                "is_basic_land": canonical.get("is_basic_land"),
                "is_nonbasic_land": canonical.get("is_nonbasic_land"),
                "is_creature": canonical.get("is_creature"),
                "is_artifact": canonical.get("is_artifact"),
                "is_enchantment": canonical.get("is_enchantment"),
                "is_instant": canonical.get("is_instant"),
                "is_sorcery": canonical.get("is_sorcery"),
                "is_planeswalker": canonical.get("is_planeswalker"),
                "is_battle": canonical.get("is_battle"),
                "is_equipment": canonical.get("is_equipment"),
                "is_vehicle": canonical.get("is_vehicle"),
                "is_aura": canonical.get("is_aura"),
                "is_saga": canonical.get("is_saga"),
                "commander_legal": canonical.get("commander_legal"),
                "legalities": canonical.get("legalities"),
                "deck_copy_rule": canonical.get("deck_copy_rule"),
                "reserved": canonical.get("reserved"),
                "digital": canonical.get("digital"),
                "game_changer": canonical.get("game_changer"),
                "rarity": canonical.get("rarity"),
                "edhrec_rank": canonical.get("edhrec_rank"),
                "prices": canonical.get("prices"),
                "eur_price": canonical.get("eur_price"),
                "scryfall_uri": canonical.get("scryfall_uri"),
                "image_uri_normal": canonical.get("image_uri_normal"),
                "tags": all_tags,
                "owned_printings": owned_printings,
            }
        )

    return sorted(aggregated, key=lambda row: (str(row.get("name") or "").lower()))


def is_card_legal_for_commander_identity(
    card_row: dict[str, Any],
    commander_identity: set[str],
) -> bool:
    if not card_row.get("commander_legal"):
        return False

    color_identity = set(card_row.get("color_identity") or [])
    return color_identity.issubset(commander_identity)


def add_color_filter_fields(
    rows: list[dict[str, Any]],
    commander_identity: set[str],
) -> list[dict[str, Any]]:
    result = []

    for row in rows:
        color_identity = set(row.get("color_identity") or [])

        enriched = {
            **row,
            "is_colorless_identity": color_identity == set(),
            "is_mono_white_identity": color_identity == {"W"},
            "is_mono_blue_identity": color_identity == {"U"},
            "is_mono_black_identity": color_identity == {"B"},
            "is_mono_red_identity": color_identity == {"R"},
            "is_mono_green_identity": color_identity == {"G"},
            "commander_identity_filter": sorted(commander_identity),
            "legal_for_selected_commander_identity": is_card_legal_for_commander_identity(
                row,
                commander_identity,
            ),
        }

        result.append(enriched)

    return result


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")


def write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )


def summarize_pool(rows: list[dict[str, Any]], commander_identity: set[str]) -> dict[str, Any]:
    type_flags = [
        "is_land",
        "is_basic_land",
        "is_nonbasic_land",
        "has_land_face",
        "is_creature",
        "is_artifact",
        "is_enchantment",
        "is_instant",
        "is_sorcery",
        "is_planeswalker",
        "is_equipment",
        "is_vehicle",
        "is_aura",
        "is_saga",
        "is_double_faced",
    ]

    type_counts = {
        flag: sum(1 for row in rows if row.get(flag))
        for flag in type_flags
    }

    tag_counter: Counter[str] = Counter()
    color_identity_counter: Counter[str] = Counter()
    mana_curve: Counter[str] = Counter()

    for row in rows:
        for tag in row.get("tags") or []:
            tag_counter[tag] += 1

        ci = row.get("color_identity") or []
        ci_key = "".join(ci) if ci else "C"
        color_identity_counter[ci_key] += 1

        mana_value = row.get("mana_value")
        if mana_value is None:
            mana_curve["unknown"] += 1
        else:
            mana_curve[str(int(float(mana_value)))] += 1

    return {
        "commander_identity": sorted(commander_identity),
        "commander_identity_slug": color_slug(commander_identity),
        "total_cards": len(rows),
        "total_owned_copies": sum(parse_int(row.get("total_owned"), 0) for row in rows),
        "type_counts": type_counts,
        "color_identity_counts": dict(sorted(color_identity_counter.items())),
        "mana_curve": dict(sorted(mana_curve.items(), key=lambda item: item[0])),
        "top_role_tags": dict(tag_counter.most_common(100)),
        "top_edhrec_cards_owned": [
            {
                "name": row.get("name"),
                "edhrec_rank": row.get("edhrec_rank"),
                "mana_value": row.get("mana_value"),
                "type_line": row.get("type_line"),
                "tags": row.get("tags"),
            }
            for row in sorted(
                [r for r in rows if r.get("edhrec_rank") is not None],
                key=lambda r: r["edhrec_rank"],
            )[:50]
        ],
    }


def build_scryfall_index(cards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index = {}

    for card in cards:
        scryfall_id = card.get("id")
        if scryfall_id:
            index[str(scryfall_id)] = card

    return index


def select_preferred_scryfall_card(cards: list[dict[str, Any]]) -> dict[str, Any]:
    # Name matching can hit multiple printings; prefer an English, physical,
    # recent printing for stable image/pricing metadata.
    return max(
        cards,
        key=lambda card: (
            card.get("lang") == "en",
            not bool(card.get("digital")),
            str(card.get("released_at") or ""),
            str(card.get("id") or ""),
        ),
    )


def build_scryfall_name_index(cards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for card in cards:
        name = clean_text(card.get("name"))
        if name is not None:
            grouped[normalize_name_for_match(name)].append(card)

        # Index face names too so EDHREC names can match modal, adventure,
        # split, and other multi-face cards.
        for face in get_card_faces(card):
            face_name = clean_text(face.get("name"))
            if face_name is not None:
                grouped[normalize_name_for_match(face_name)].append(card)

    return {
        name_key: select_preferred_scryfall_card(matches)
        for name_key, matches in grouped.items()
    }


def build_edhrec_candidate_row(
    edhrec_candidate: dict[str, Any],
    scryfall_card: dict[str, Any],
    owned_cards_by_oracle_id: dict[str, dict[str, Any]],
    commander_identity: set[str],
) -> dict[str, Any]:
    common = build_common_card_fields(scryfall_card)
    enriched = add_color_filter_fields([common], commander_identity)[0]
    owned_card = owned_cards_by_oracle_id.get(str(enriched.get("oracle_id")))

    # Ownership is an annotation on the Scryfall-enriched candidate, not a
    # replacement for the EDHREC recommendation or Scryfall rules data.
    return {
        **enriched,
        "candidate_source": "edhrec",
        "edhrec": edhrec_candidate,
        "is_owned": owned_card is not None,
        "total_owned": parse_int(
            owned_card.get("total_owned") if owned_card else None,
            default=0,
        ),
        "commander_owned_playable_copies": parse_int(
            (
                owned_card.get("commander_owned_playable_copies")
                if owned_card
                else None
            ),
            default=0,
        ),
        "owned_printing_count": parse_int(
            owned_card.get("owned_printing_count") if owned_card else None,
            default=0,
        ),
        "owned_printings": owned_card.get("owned_printings") if owned_card else [],
    }


def build_edhrec_outputs(
    edhrec_csv: Path,
    scryfall_name_index: dict[str, dict[str, Any]],
    owned_card_rows: list[dict[str, Any]],
    commander_identity: set[str],
    out_dir: Path,
) -> dict[str, Any]:
    logging.info("Reading EDHREC CSV: %s", edhrec_csv)
    edhrec_candidates = read_edhrec_candidates(edhrec_csv)
    owned_cards_by_oracle_id = {
        str(row["oracle_id"]): row
        for row in owned_card_rows
        if row.get("oracle_id")
    }

    candidate_rows: list[dict[str, Any]] = []
    unmatched_rows: list[dict[str, Any]] = []

    for candidate in edhrec_candidates:
        name_key = normalize_name_for_match(candidate["name"])
        scryfall_card = scryfall_name_index.get(name_key)

        if scryfall_card is None:
            unmatched_rows.append(
                {
                    "reason": "scryfall_name_not_found",
                    "edhrec": candidate,
                }
            )
            continue

        candidate_rows.append(
            build_edhrec_candidate_row(
                candidate,
                scryfall_card,
                owned_cards_by_oracle_id,
                commander_identity,
            )
        )

    # Keep both the full EDHREC candidate list and a commander-filtered list:
    # the full list is useful for explaining exclusions and upgrade context.
    commander_legal_rows = [
        row
        for row in candidate_rows
        if row.get("legal_for_selected_commander_identity")
    ]
    slug = color_slug(commander_identity)

    write_jsonl(out_dir / "edhrec_candidates_ai.jsonl", candidate_rows)
    write_jsonl(
        out_dir / f"edhrec_candidates_{slug}_commander_ai.jsonl",
        commander_legal_rows,
    )

    if unmatched_rows:
        write_jsonl(out_dir / "unmatched_edhrec_rows.jsonl", unmatched_rows)

    return {
        "csv": str(edhrec_csv),
        "candidate_names": len(edhrec_candidates),
        "matched_candidates": len(candidate_rows),
        "unmatched_candidates": len(unmatched_rows),
        "selected_commander_legal_candidates": len(commander_legal_rows),
        "owned_candidates": sum(1 for row in candidate_rows if row.get("is_owned")),
        "output": {
            "all_candidates": str(out_dir / "edhrec_candidates_ai.jsonl"),
            "selected_commander_legal_candidates": str(
                out_dir / f"edhrec_candidates_{slug}_commander_ai.jsonl"
            ),
            "unmatched": (
                str(out_dir / "unmatched_edhrec_rows.jsonl")
                if unmatched_rows
                else None
            ),
        },
    }


def build_dataset(
    manabox_csv: Path,
    scryfall_json: Path,
    out_dir: Path,
    commander_colors: str,
    edhrec_csv: Path | None = None,
    strict: bool = False,
) -> None:
    commander_identity = parse_commander_colors(commander_colors)
    slug = color_slug(commander_identity)

    logging.info("Reading ManaBox CSV: %s", manabox_csv)
    manabox_rows = read_manabox_csv(manabox_csv)

    logging.info("Reading Scryfall data: %s", scryfall_json)
    scryfall_cards = load_scryfall_cards(scryfall_json)
    scryfall_index = build_scryfall_index(scryfall_cards)
    scryfall_name_index = build_scryfall_name_index(scryfall_cards)

    # ManaBox rows identify exact owned printings by Scryfall id.
    printing_rows: list[dict[str, Any]] = []
    unmatched_rows: list[dict[str, Any]] = []

    for row in manabox_rows:
        scryfall_id = row.get("scryfall_id")

        if not scryfall_id:
            unmatched_rows.append(
                {
                    "reason": "missing_scryfall_id",
                    "manabox_row": row,
                }
            )
            continue

        card = scryfall_index.get(str(scryfall_id))

        if not card:
            unmatched_rows.append(
                {
                    "reason": "scryfall_id_not_found",
                    "scryfall_id": scryfall_id,
                    "manabox_row": row,
                }
            )
            continue

        printing_rows.append(build_printing_row(row, card))

    if strict and unmatched_rows:
        raise RuntimeError(
            f"{len(unmatched_rows)} ManaBox rows could not be matched to Scryfall."
        )

    # Oracle ids collapse reprints into one playable card identity for deck
    # building while preserving printing detail under owned_printings.
    card_rows = aggregate_by_oracle_id(printing_rows)
    card_rows = add_color_filter_fields(card_rows, commander_identity)

    commander_pool = [
        row
        for row in card_rows
        if row.get("legal_for_selected_commander_identity")
    ]

    out_dir.mkdir(parents=True, exist_ok=True)

    write_jsonl(out_dir / "owned_printings_ai.jsonl", printing_rows)
    write_jsonl(out_dir / "owned_cards_ai.jsonl", card_rows)
    write_jsonl(out_dir / f"pool_{slug}_commander_ai.jsonl", commander_pool)

    if unmatched_rows:
        write_jsonl(out_dir / "unmatched_manabox_rows.jsonl", unmatched_rows)

    # Optional EDHREC data is joined after owned-card aggregation so candidate
    # rows can say whether each recommendation is already in the collection.
    edhrec_summary = None
    if edhrec_csv is not None:
        edhrec_summary = build_edhrec_outputs(
            edhrec_csv=edhrec_csv,
            scryfall_name_index=scryfall_name_index,
            owned_card_rows=card_rows,
            commander_identity=commander_identity,
            out_dir=out_dir,
        )

    summary = {
        "input": {
            "manabox_csv": str(manabox_csv),
            "scryfall_json": str(scryfall_json),
            "edhrec_csv": str(edhrec_csv) if edhrec_csv else None,
            "commander_colors": commander_colors,
            "commander_identity": sorted(commander_identity),
        },
        "counts": {
            "manabox_rows": len(manabox_rows),
            "matched_printing_rows": len(printing_rows),
            "unmatched_rows": len(unmatched_rows),
            "unique_playable_cards": len(card_rows),
            "selected_commander_pool_cards": len(commander_pool),
            "selected_commander_pool_owned_copies": sum(
                parse_int(row.get("total_owned"), 0)
                for row in commander_pool
            ),
        },
        "selected_commander_pool_summary": summarize_pool(
            commander_pool,
            commander_identity,
        ),
        "edhrec": edhrec_summary,
    }

    write_json(out_dir / f"summary_{slug}.json", summary)

    logging.info("Done.")
    logging.info("Owned printings: %s", out_dir / "owned_printings_ai.jsonl")
    logging.info("Owned cards: %s", out_dir / "owned_cards_ai.jsonl")
    logging.info("Commander pool: %s", out_dir / f"pool_{slug}_commander_ai.jsonl")
    logging.info("Summary: %s", out_dir / f"summary_{slug}.json")
    if edhrec_summary is not None:
        logging.info("EDHREC candidates: %s", out_dir / "edhrec_candidates_ai.jsonl")
        logging.info(
            "EDHREC commander candidates: %s",
            out_dir / f"edhrec_candidates_{slug}_commander_ai.jsonl",
        )

    if unmatched_rows:
        logging.warning(
            "%s rows could not be matched. See %s",
            len(unmatched_rows),
            out_dir / "unmatched_manabox_rows.jsonl",
        )


def main(
    manabox_csv: Annotated[
        Path,
        typer.Option(
            "--manabox-csv",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Path to ManaBox CSV export.",
        ),
    ],
    scryfall_json: Annotated[
        Path | None,
        typer.Option(
            "--scryfall-json",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Path to Scryfall default-cards JSON/JSONL file.",
        ),
    ] = None,
    download_scryfall: Annotated[
        bool,
        typer.Option(
            "--download-scryfall",
            help="Download Scryfall default-cards JSON if --scryfall-json is not supplied.",
        ),
    ] = False,
    out_dir: Annotated[
        Path,
        typer.Option(
            "--out-dir",
            file_okay=False,
            dir_okay=True,
            help="Output directory.",
        ),
    ] = Path("out"),
    commander_colors: Annotated[
        str,
        typer.Option(
            "--commander-colors",
            help=(
                "Commander color identity filter. Examples: WU, UB, WUBRG, "
                "azorius, esper, colorless."
            ),
        ),
    ] = "WU",
    edhrec_csv: Annotated[
        Path | None,
        typer.Option(
            "--edhrec-csv",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Optional EDHREC recommendation CSV to enrich from Scryfall.",
        ),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Fail if any ManaBox row cannot be matched to Scryfall.",
        ),
    ] = False,
    log_level: Annotated[
        LogLevel,
        typer.Option("--log-level", help="Logging level."),
    ] = LogLevel.INFO,
) -> None:
    """Build an AI-ready MTG deck-building dataset from ManaBox CSV + Scryfall data."""
    logging.basicConfig(
        level=getattr(logging, log_level.value),
        format="%(levelname)s: %(message)s",
    )

    if scryfall_json is None:
        if not download_scryfall:
            logging.error(
                "Provide --scryfall-json or use --download-scryfall."
            )
            raise typer.Exit(1)

        scryfall_json = out_dir / "scryfall_default_cards.json"
        download_scryfall_default_cards(scryfall_json)

    try:
        build_dataset(
            manabox_csv=manabox_csv,
            scryfall_json=scryfall_json,
            out_dir=out_dir,
            commander_colors=commander_colors,
            edhrec_csv=edhrec_csv,
            strict=strict,
        )
    except Exception as exc:
        logging.exception("Dataset build failed.")
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    typer.run(main)
