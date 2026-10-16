"""Weekly chicken salad price check for fast food and fast casual chains in Orange County, CA.

Step 1: Claude researches current prices with web search and writes notes with sources.
Step 2: a second call turns those notes into structured rows (JSON schema enforced).
Step 3: rows are appended to data/prices.csv and rendered to reports/<date>.md and LATEST.md.

Usage:
    python tracker.py                    # full run (needs ANTHROPIC_API_KEY)
    python tracker.py --notes notes.md   # skip research, extract from saved notes
"""

import argparse
import csv
import datetime
import json
import pathlib
import sys
from zoneinfo import ZoneInfo

import anthropic

ROOT = pathlib.Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
CSV_PATH = ROOT / "data" / "prices.csv"
REPORTS = ROOT / "reports"
MODEL = "claude-opus-5-5"
CSV_FIELDS = [
    "date", "restaurant", "category", "item", "price_usd",
    "source_type", "confidence", "location_note", "source_url",
]
SOURCE_TYPES = ["official_site", "online_ordering", "delivery_app", "news_or_review", "other"]

ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "restaurant": {"type": "string"},
                    "category": {"type": "string", "enum": list(CONFIG["categories"])},
                    "item": {"type": "string"},
                    "price_usd": {"type": ["number", "null"]},
                    "source_type": {"type": "string", "enum": SOURCE_TYPES},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "location_note": {"type": "string"},
                    "source_url": {"type": "string"},
                },
                "required": [
                    "restaurant", "category", "item", "price_usd",
                    "source_type", "confidence", "location_note", "source_url",
                ],
                "additionalProperties": False,
            },
        },
        "not_found": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "restaurant": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["restaurant", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items", "not_found"],
    "additionalProperties": False,
}


def research_prompt(today: str) -> str:
    cats = "\n".join(f"- {name}: {desc}" for name, desc in CONFIG["categories"].items())
    chains = "\n".join(f"- {r}" for r in CONFIG["restaurants"])
    extra = (
        "Also add any other fast food or fast casual chains with locations in the area "
        "that sell these items, if you come across them."
        if CONFIG.get("discover_more") else ""
    )
    return f"""Today is {today}. Find current menu prices at fast food and fast casual
restaurants in {CONFIG['area']} (for example {', '.join(CONFIG['reference_cities'])}).

Track these two kinds of item:
{cats}

Restaurants to check:
{chains}
{extra}

For each restaurant, list every menu item that fits a category, with:
- the price in USD for a single regular-size item (no combo or meal)
- the URL where you saw that price
- the kind of source: official site, the chain's own online ordering, a delivery app,
  news/review, or other
- which location the price is for, if the source says (prices vary by store)
- how confident you are (high = seen on the chain's own site or ordering page for an OC
  store; medium = official but not OC-specific, or a recent third-party source;
  low = old or indirect)

Rules:
- Never guess a price. If you can't find one with a source, say so and give the reason
  (for example: no OC location, item not on the menu, prices not published online).
- Delivery app prices are usually marked up; use one only when nothing better exists,
  and label it.
- Prefer sources from the last few months.

Write your findings as plain notes, one restaurant at a time."""


def research(today: str) -> str:
    client = anthropic.Anthropic()
    user = {"role": "user", "content": research_prompt(today)}
    assistant_content = []
    tools = [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": CONFIG["max_web_searches"],
        "user_location": {
            "type": "approximate",
            "city": "Irvine",
            "region": "California",
            "country": "US",
            "timezone": "America/Los_Angeles",
        },
    }]

    # The server-side search loop can pause on long runs; resume it a few times.
    for _ in range(6):
        messages = [user]
        if assistant_content:
            messages.append({"role": "assistant", "content": assistant_content})
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high"},
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "refusal":
            sys.exit(f"Research request was declined: {resp.stop_details}")
        assistant_content.extend(resp.content)
        if resp.stop_reason != "pause_turn":
            break

    notes = "\n".join(b.text for b in assistant_content if b.type == "text").strip()
    if not notes:
        sys.exit("Research returned no notes.")
    return notes


def extract(notes: str) -> dict:
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": ROW_SCHEMA},
        },
        messages=[{
            "role": "user",
            "content": (
                "Turn these research notes into rows. Copy prices and URLs exactly as "
                "written; do not add anything that isn't in the notes. Items with no "
                "sourced price go in not_found instead of items.\n\n<notes>\n"
                + notes + "\n</notes>"
            ),
        }],
    )
    if resp.stop_reason == "refusal":
        sys.exit(f"Extraction request was declined: {resp.stop_details}")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def clean(data: dict) -> tuple[list[dict], list[dict]]:
    """Keep only rows with a price and an http(s) source; everything else is 'not found'."""
    rows, missing = [], list(data["not_found"])
    for row in data["items"]:
        if row["price_usd"] is None or not row["source_url"].startswith(("http://", "https://")):
            missing.append({"restaurant": row["restaurant"], "reason": f"no sourced price for {row['item']}"})
            continue
        row["price_usd"] = round(float(row["price_usd"]), 2)
        rows.append(row)
    return rows, missing


def load_history() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(newline="") as f:
        return list(csv.DictReader(f))


def save_history(history: list[dict], today: str, rows: list[dict]) -> None:
    # Re-running on the same day replaces that day's rows.
    kept = [r for r in history if r["date"] != today]
    kept += [{"date": today, **r} for r in rows]
    CSV_PATH.parent.mkdir(exist_ok=True)
    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(kept)


def key(row: dict) -> tuple:
    return (row["restaurant"].lower(), row["category"], row["item"].lower())


def previous_prices(history: list[dict], today: str) -> tuple[str | None, dict]:
    dates = sorted({r["date"] for r in history if r["date"] < today})
    if not dates:
        return None, {}
    last = dates[-1]
    return last, {key(r): float(r["price_usd"]) for r in history if r["date"] == last}


def render(today: str, rows: list[dict], missing: list[dict], prev_date, prev: dict) -> str:
    out = [
        f"# Chicken salad prices in {CONFIG['area']} ({today})",
        "",
        "Prices are for one regular-size item, before tax, and can vary by store. "
        "Every price links to where it was found. Check the confidence column before "
        "relying on a number.",
        "",
    ]
    if prev_date:
        out += [f"Change column compares with {prev_date}.", ""]

    for cat, desc in CONFIG["categories"].items():
        cat_rows = sorted((r for r in rows if r["category"] == cat), key=lambda r: r["price_usd"])
        out += [f"## {cat.capitalize()}: {desc}", ""]
        if not cat_rows:
            out += ["Nothing found this week.", ""]
            continue
        out += [
            "| Restaurant | Item | Price | Change | Confidence | Source |",
            "|---|---|---:|---:|---|---|",
        ]
        for r in cat_rows:
            old = prev.get(key(r))
            if old is None:
                change = "new" if prev_date else ""
            else:
                diff = r["price_usd"] - old
                change = "same" if abs(diff) < 0.005 else f"{diff:+.2f}"
            cell = lambda text: text.replace("|", "/")
            loc = f" ({cell(r['location_note'])})" if r["location_note"] else ""
            src = r["source_type"].replace("_", " ")
            out.append(
                f"| {cell(r['restaurant'])} | {cell(r['item'])}{loc} | ${r['price_usd']:.2f} | {change} "
                f"| {r['confidence']} | [{src}]({r['source_url']}) |"
            )
        out.append("")

    if missing:
        out += ["## Not found", ""]
        out += [f"- **{m['restaurant']}**: {m['reason']}" for m in missing]
        out.append("")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes", help="extract from a saved notes file instead of researching")
    args = parser.parse_args()

    today = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()

    notes = pathlib.Path(args.notes).read_text() if args.notes else research(today)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"{today}-notes.md").write_text(notes + "\n")

    rows, missing = clean(extract(notes))
    if not rows:
        sys.exit("No sourced prices found; leaving data unchanged.")

    history = load_history()
    prev_date, prev = previous_prices(history, today)
    save_history(history, today, rows)

    report = render(today, rows, missing, prev_date, prev)
    (REPORTS / f"{today}.md").write_text(report)
    (ROOT / "LATEST.md").write_text(report)
    print(f"Wrote {len(rows)} prices to reports/{today}.md")


if __name__ == "__main__":
    main()
