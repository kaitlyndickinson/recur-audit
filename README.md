# household-app

A small self-hosted app for a household to track its finances together, replacing spreadsheet trackers. Each person signs in through Tailscale and sees their own data.

## Phase 1: find subscriptions to clean up

- Import bank and card exports (CSV, OFX/QFX). Overlapping exports are deduplicated, and the original files are kept untouched.
- Detect recurring charges: regular spacing between charges plus fairly stable amounts.
- Show cadence, latest amount, yearly cost, price increases, and charges that look like they've stopped, plus a 12-month spend chart.
- Mark each one keep, cancel, cancelled, or not a subscription.
- Rename or merge merchants when the automatic cleanup of bank descriptions gets it wrong.

## Phase 2: Gmail, for an email migration

- Read-only access to sender and subject only (no message bodies).
- On an old address: find which services still send there, so you know what to move to the new address, cancel, or just let go.

## Running it

Requires Python 3.10+.

```sh
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"        # macOS/Linux: .venv/bin/pip

# Local development: the env var stands in for the Tailscale identity header
HOUSEHOLD_DEV_USER=you@example.com .venv/Scripts/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On Windows PowerShell, set the variables first: `$env:HOUSEHOLD_DEV_USER="you@example.com"; .venv/Scripts/uvicorn app.main:app --host 127.0.0.1 --port 8000`.

### Demo data

`python scripts/make_demo_data.py` writes synthetic exports to `data/demo/`. Set `HOUSEHOLD_DATA_DIR=data/demo` to keep the demo database separate from the real one.

| File | Import as | Shows |
|---|---|---|
| `demo-card.csv` | you, account "Card" | Subscriptions, a price increase, one that stopped, a yearly charge |
| `demo-card-recent.csv` | you, account "Card" | A later download of the same card: every row is skipped as already imported |
| `demo-checking.csv` | you, account "Checking" | Debit/credit columns; rent to mark "Not a subscription"; an electric bill too variable to count |
| `demo-partner.ofx` | a second person | OFX import, and that each person only sees their own data |

### Trying it as two people

Locally, `HOUSEHOLD_DEV_USER` decides who you are. Run two servers on the same data, one per person, and open each in its own tab:

```sh
HOUSEHOLD_DATA_DIR=data/demo HOUSEHOLD_DEV_USER=you@example.com .venv/Scripts/uvicorn app.main:app --port 8000
HOUSEHOLD_DATA_DIR=data/demo HOUSEHOLD_DEV_USER=partner@example.com .venv/Scripts/uvicorn app.main:app --port 8001
```

On the server there's no env var: each request carries the Tailscale login of whoever's device sent it.

### On the server

On the Linux machine that holds the real data, clone to `~/dev/projects/household-app` and run the app as a systemd user service:

```sh
deploy/install.sh                                   # rerun after each git pull
systemctl --user status household-app
journalctl --user -u household-app -f
```

The service listens on `127.0.0.1:8000`, keeps its data in the checkout's `data/`, and creates files readable only by your user. User services stop when you log out unless lingering is on (`loginctl enable-linger`); the script tells you if it's off.

### On your phones

The app only listens on localhost. Publish it to your tailnet on its own HTTPS port, so it doesn't replace anything already served on 443:

```sh
tailscale serve --bg --https=8443 http://127.0.0.1:8000
tailscale serve status    # check nothing else got remapped
```

Then open `https://<machine>.<tailnet>.ts.net:8443` from any device signed in to the tailnet. Use `serve`, never `funnel`: funnel publishes to the whole internet. The app relies on Tailscale's `Tailscale-User-Login` header to know who's asking. Adding each person as a user on the tailnet is the dependable way to get it; if someone gets a 403, that header is missing. Don't set `HOUSEHOLD_DEV_USER` on the server.

## Data

Everything lives in `data/` (gitignored): `household.db` and `raw/` holds the original exports. Back up by copying the folder. Keep it out of cloud-synced folders and use full-disk encryption on the host.

## Open questions

- Shared finances: what's shared vs. solo, and how both people add things.
- Phase 2 Gmail integration details.

Decisions and their reasons are in [DECISIONS.md](DECISIONS.md).
