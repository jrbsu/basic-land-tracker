# Basic Land Tracker

A small local Flask app for visually tracking which Magic: The Gathering basic lands you own from each set.

> **Disclosure:** This is a personal desktop/web app intended for tracking my own basic land collection as a collaboration between [@jamesryanalexander](https://github.com/jamesryanalexander) and [@jrbsu](https://github.com/jrbsu). It was built with the help of Claude Code; all AI-generated components have been manually reviewed and tested to ensure stability and security.

It uses:

- **Scryfall bulk JSON** as the card/image catalogue.
- **A Moxfield-style CSV export** as the ownership overlay.
- **SQLite** for local storage.
- **Manual finish toggles** in the UI for cleanup and edge cases.
 
## What it tracks

By default the grid shows the five normal basic land names:

- Plains
- Island
- Swamp
- Mountain
- Forest

The app also imports Wastes and Snow-Covered basics. Use the `Include Wastes and snow basics` checkbox to show them.

Each printing appears once, with finish badges:

- `NF` = nonfoil
- `F` = foil
- `E` = etched

If you own any finish for that printing, the card image appears in colour. If you own none, it appears black-and-white.

## Setup

```bash
cd basic-land-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
flask --app app run --debug
```

Then open:

```text
http://127.0.0.1:5000
```

The SQLite database is created automatically at:

```text
data/basic_lands.sqlite3
```

## Import Scryfall data

Download Scryfall's Default Cards bulk JSON.

Then go to:

```text
/imports
```

You can either upload the JSON file or provide a local path such as:

```text
/Users/you/Downloads/default-cards.json
```

The importer can read:

- a normal Scryfall JSON array
- newline-delimited JSON
- `.gz` compressed input

The importer streams the file using `ijson`, so it should not need to load the whole Scryfall bulk file into memory.

## Import your collection

Go to:

```text
/imports
```

Upload a CSV collection export.

Best matching column:

```text
Scryfall ID
```

Fallback matching uses:

```text
Name + Set Code/Set Name + Collector Number
```

The importer tries to understand common column names, including:

- `Name`
- `Scryfall ID`
- `Set`, `Set Code`, `Edition`, `Set Name`
- `Collector Number`, `Number`
- `Quantity`, `Count`
- `Finish`, `Foil`, `Printing`, `Variant`

If a row cannot be matched to a Scryfall card in the local catalogue, it is skipped and counted as unmatched.

## Add lands manually

Go to:

```text
/collection
```

Enter a Scryfall set code (e.g. `znr`), a collector number, and a finish, then click
`Check card on Scryfall` to fetch the card live from the Scryfall API and preview its
image before adding it. Submitting the form looks the card up again server-side, adds
it to the local catalogue (using its Scryfall ID), and records it as owned. If you're confident that the card exists you can submit the form directly without checking with scryfall. This will not use the live API.

The same page lists your full manually tracked collection — name, set, collector
number, Scryfall ID, a small image linking to the card on Scryfall, and a delete
button for each entry. The `check card` button talks to the live Scryfall API, so it needs an internet
connection (unlike the rest of the app).

## Manual cleanup

Click a finish badge on any card to toggle ownership for that finish.

This is useful when:

- a collection export is missing finish data
- a card matched but the finish was wrong
- you want to track a few cards manually before importing a full collection

## Export Collection

You can export a CSV of the ownership information on either the Collection page or the Import page.

This is designed to let you move servers/systems easily and is comptabile with the import function.

## Notes and limitations

- This app does not use live APIs during normal use.
- It does not currently import the ManaBox JSON format with `product_id` / `tcgplayer_sku_id`, because that format does not include enough card identity data by itself.
- Pricing is intentionally not included yet, but the database can be extended later.
- Scryfall image URLs are hotlinked rather than downloaded locally.

## Possible next improvements

- Add a ManaBox importer if the export includes Scryfall IDs or card names/set/collector numbers.
- Add cached local card images.
- Add price snapshots from Scryfall or TCGplayer.
- Add a completion page for each land type.
- Add export to CSV.
