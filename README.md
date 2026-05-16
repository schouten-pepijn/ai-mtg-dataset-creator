# MTG AI Dataset Builder

This project builds AI-ready Magic: The Gathering collection datasets from:

- `input/all_cards_16_05.csv`: a ManaBox CSV export of the owned collection.
- `input/scryfall_default_cards.json`: Scryfall bulk data, using the Default Cards file from the [Scryfall Bulk Data API](https://scryfall.com/docs/api/bulk-data).
- `input/millicent_edhrec.csv`: optional EDHREC recommendation export for a commander.

The Scryfall input may be either a JSON array or JSONL file, including `.gz` variants. In this repository the local file is named `input/scryfall_default_cards.json`, so examples should use that exact path.

EDHREC CSV input is used only as a candidate and recommendation signal. Scryfall remains the rules-data source for fields such as `mana_cost`, `oracle_text`, `color_identity`, `commander_legal`, prices, and legal filtering.

## Build Command

```powershell
uv run python .\build_mtg_ai_dataset.py `
  --manabox-csv .\input\all_cards_16_05.csv `
  --scryfall-json .\input\scryfall_default_cards.json `
  --edhrec-csv .\input\millicent_edhrec.csv `
  --commander-colors WU `
  --out-dir .\output
```

For a Bant/Galea pool, generate a different commander-color pool:

```powershell
uv run python .\build_mtg_ai_dataset.py `
  --manabox-csv .\input\all_cards_16_05.csv `
  --scryfall-json .\input\scryfall_default_cards.json `
  --commander-colors WUG `
  --out-dir .\output
```

That creates `pool_WUG_commander_ai.jsonl`.

## Output Files

### `owned_printings_ai.jsonl`

Grain: one row per owned ManaBox printing.

Use this when you care about the exact physical card copy.

Example use cases:

- Which printing do I own?
- Do I own foil or nonfoil?
- What set is this card from?
- How many physical copies do I own?
- What condition, language, or purchase price did ManaBox record?

This file preserves collection and inventory detail.

Example:

```json
{
  "name": "Kor Outfitter",
  "scryfall_id": "...",
  "oracle_id": "...",
  "quantity_owned": 1,
  "set": "zen",
  "collector_number": "21",
  "manabox": {
    "foil": "normal",
    "condition": "Near Mint",
    "language": "English"
  }
}
```

Use for collection management, pricing, duplicates, and printings.

Do not use this as the primary AI deck-building file because reprints can create duplicate playable cards.

### `owned_cards_ai.jsonl`

Grain: one row per playable card identity, grouped by `oracle_id`.

Use this for general AI deck-building across the full owned collection.

It deduplicates reprints. For example, if you own three different printings of Sol Ring, this file has one Sol Ring row with:

- `total_owned = 3`
- `commander_owned_playable_copies = 1`
- `owned_printings = [...]`

This file contains enriched gameplay fields, including:

- `mana_cost`
- `mana_value`
- `type_line`
- `oracle_text`
- `color_identity`
- `commander_legal`
- `tags`
- `is_land`
- `is_artifact`
- `is_creature`
- `is_double_faced`
- `has_land_face`
- `produced_mana`
- `edhrec_rank`
- `prices`

Use for general AI deck-building from the full owned collection.

Example prompt:

```text
Use owned_cards_ai.jsonl as my full MTG collection. Suggest commanders I can build with minimal purchases. Prioritize unique playstyles and strong synergy density.
```

### `pool_WU_commander_ai.jsonl`

Grain: one row per playable card identity, already filtered for Azorius Commander.

This is the best file for Azorius deck-building.

It includes cards where:

- `commander_legal == true`
- `color_identity` is a subset of `{"W", "U"}`

So it includes:

- white cards
- blue cards
- white-blue cards
- true colorless cards
- Azorius-legal lands
- colorless artifacts

It excludes:

- Forest
- green cards
- red cards
- black cards
- artifacts with off-color activated abilities
- lands with off-color color identity

Use for building Azorius decks specifically.

Example prompt:

```text
Use pool_WU_commander_ai.jsonl as my legal owned card pool for Azorius Commander. Build a Rhoda // Timin tap-control deck. Prefer cards I own. Suggest up to EUR 50 in upgrades separately.
```

### `summary_WU.json`

Grain: aggregate summary of the Azorius pool.

This is not the main deck-building dataset. It is a compact overview.

It contains things like:

- number of Azorius-legal cards
- total owned copies
- mana curve
- type counts
- land count
- creature count
- artifact count
- double-faced card count
- top role tags
- top EDHREC-ranked owned cards

Use for quick diagnostics and overview.

Example use cases:

- Do I have enough lands?
- How many removal cards are in my Azorius pool?
- How many counterspells?
- What does my mana curve look like?
- How many artifacts, vehicles, or equipment cards?

Example prompt:

```text
Use summary_WU.json to assess the strengths and gaps of my Azorius-owned pool. Then use pool_WU_commander_ai.jsonl to build the actual deck.
```

### `edhrec_candidates_ai.jsonl`

Grain: one row per deduplicated EDHREC candidate card name, enriched from local Scryfall data.

Use this when you want EDHREC recommendations as an AI-ready consideration list, while keeping Scryfall as the authoritative source for rules and legality.

Each row includes:

- Scryfall-enriched card fields such as `mana_cost`, `type_line`, `oracle_text`, `color_identity`, `commander_legal`, `prices`, and `tags`
- `edhrec`, containing recommendation metadata such as `decks`, `inclusion`, `potential_decks`, `inclusion_rate`, `synergy`, `salt`, and EDHREC market prices
- ownership fields such as `is_owned`, `total_owned`, `commander_owned_playable_copies`, `owned_printing_count`, and `owned_printings`
- `legal_for_selected_commander_identity`, computed from Scryfall color identity and Commander legality

Use for comparing EDHREC recommendations against your owned collection and identifying high-synergy cards you already own or may want to buy.

### `edhrec_candidates_WU_commander_ai.jsonl`

Grain: one row per EDHREC candidate that is legal for the selected Azorius Commander identity.

This is the best EDHREC-driven candidate file for Azorius Commander deck-building. It removes candidates that Scryfall says are not legal for the selected color identity or Commander format.

Example prompt:

```text
Use edhrec_candidates_WU_commander_ai.jsonl as EDHREC recommendation candidates for a Millicent Spirits deck. Prefer cards where is_owned is true, rank by synergy and role coverage, and suggest missing upgrades separately.
```
