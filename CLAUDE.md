# CLAUDE.md

Shared household app that replaces spreadsheet trackers. See README.md for what it does and DECISIONS.md for what's been decided and why.

## Workflow

- Architectural and stack decisions aren't yours to make. If something isn't in DECISIONS.md, propose options and ask.
- When a decision gets made, add it to DECISIONS.md: the decision, why, and what it rules out.

## Code style

- Lean. Write the least code that does the job well. No abstractions, layers, or config for things we don't need yet.
- Prefer the standard library and boring, well-known dependencies. Ask before adding a new one.
- Clear names over comments. Comment the *why*, not the what.
- Money is integer cents, never floats.

## Tests

- Test behavior, not implementation details.
- When fixing a bug, write the test first and confirm it fails without the fix.
- Run the full test suite before saying something is done, and say what you ran.

## Data and privacy

- Never read, open, or copy real financial data (bank exports, the database) unless explicitly asked. Use synthetic data for tests, fixtures, and examples.
- Bank exports, databases, `.env`, and OAuth tokens never go in git. Check .gitignore covers anything new.
- Raw imports stay untouched; cleaning and normalizing happens on a copy.
- Gmail access is read-only, sender and subject only. Never request broader scopes.
- Everything is self-hosted. No third-party services that would see financial data.

## Stack

Python 3.10+, FastAPI, Jinja templates + htmx (vendored in `app/static/`, no CDN), stdlib `sqlite3`, and hand-written CSS. Auth is Tailscale identity headers. See DECISIONS.md.

## Commands

- Install: `python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"`
- Run (dev): `HOUSEHOLD_DEV_USER=dev@example.com .venv/Scripts/uvicorn app.main:app --host 127.0.0.1 --port 8000`
- Demo data: `python scripts/make_demo_data.py`, then run with `HOUSEHOLD_DATA_DIR=data/demo`
- Test: `.venv/Scripts/python -m pytest`
- uvicorn `--reload` doesn't reliably restart on Windows; restart the server by hand after changes.
