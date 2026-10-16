# OC-lunch-tracker
lunch prices for a chicken salad at fast restaurants in the OC

Every Monday a GitHub Action asks Claude to look up current prices for two items at fast food and fast casual chains in Orange County, CA:

- **Salad**: a green salad topped with chicken
- **Sandwich**: a chicken salad sandwich

**See the latest table in [LATEST.md](LATEST.md).** Older weeks are in [`reports/`](reports), and every price ever recorded is in [`data/prices.csv`](data/prices.csv).

## How it works

1. `tracker.py` sends Claude (with web search) a list of chains from `config.json`. Claude writes notes with a source link for every price, saved as `reports/<date>-notes.md`.
2. A second call turns the notes into rows. Rows without a price or a source link are dropped into a "Not found" list.
3. The script appends the rows to `data/prices.csv`, writes `reports/<date>.md`, copies it to `LATEST.md`, and the workflow commits the result.

## Caveats

- Many chains don't post prices online, and prices vary by store. Delivery app prices are marked up. The confidence and source columns tell you how much to trust each number.
- Prices come from web search, so spot-check anything surprising by clicking the source link.

## Setup

1. Create an API key at <https://console.anthropic.com> (Settings → API keys). The account needs credits under Billing.
2. In this repo go to **Settings → Secrets and variables → Actions → New repository secret**. Name it `ANTHROPIC_API_KEY` and paste the key.
3. Go to **Actions → Weekly chicken salad prices → Run workflow** to run it now instead of waiting for Monday.

To change which restaurants are checked, edit `config.json`.

Run locally:

```sh
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
python tracker.py
```
