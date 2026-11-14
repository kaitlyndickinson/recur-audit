# Decisions

## Stack: FastAPI + Jinja templates + htmx

Python backend that renders HTML on the server, with htmx for in-place updates and one hand-written CSS file.

**Why:** Least code and fewest moving parts for a small self-hosted app. One language, no JS build step, and the data processing (parsing exports, detecting recurring charges) is natural in Python.

**Rules out:** A separate single-page frontend (React/Vite), a JS build toolchain, and reusing another project's stack.

## Auth: Tailscale identity, no login code in the app

The app listens on `127.0.0.1` only. `tailscale serve` publishes it on the tailnet and sets the `Tailscale-User-Login` header, which the app trusts as the user's identity. Requests without it get a 403. State-changing requests also require htmx's `HX-Request` header, which blocks cross-site form posts (CSRF) without tokens or cookies.

**Why:** Hand-rolled auth is a liability for an app holding financial data. Tailscale gives device-level access control and identity-provider sign-in (with MFA) without the app owning passwords or sessions. Traffic is end-to-end encrypted; Tailscale's coordination servers never see app data.

**Rules out:** Exposing the app on the LAN or internet directly, any access path where Tailscale doesn't set the identity header (those requests get a 403; check how a user reached through node sharing appears before relying on sharing), and passwords, sessions or OAuth in the app.

## Every row has an owner

Imports, transactions, merchant names and subscription statuses are all keyed by `owner` (the Tailscale login), and every query filters on it.

**Why:** Each person brings their own bank data now, and shared finances may come later. Owning rows from day one means "shared" becomes a visibility setting rather than a data migration.

**Rules out:** One combined dataset that everyone sees.

## SQLite via the standard library, no ORM

One file at `data/household.db`, accessed with `sqlite3` and plain SQL in `app/db.py`.

**Why:** Two users, nothing to run or administer. Backup means copying a file.

**Rules out:** Postgres and an ORM, for now. Revisit if we ever need concurrent writers at scale.

## Merchant names: automatic cleanup plus an editable name table

`merchants.merchant_key()` strips processor prefixes, dates, reference numbers, state codes and bank noise words. In the Merchants page you can rename any key; giving two keys the same name merges them. Your names always beat the automatic ones.

**Why:** Bank descriptions are too messy for rules alone, and fixing a grouping shouldn't require a code change.

**Rules out:** Regex-only rules and AI-based merchant matching.

## Recurring-charge detection is plain math

A merchant is a subscription when it has:
- at least 3 charges;
- a median gap within ±20% of weekly, biweekly, monthly, quarterly or yearly, with at least 75% of gaps within that tolerance;
- at least 75% of charges within 25% of the median amount. The 75% leaves room for a price hike.

A price change is flagged only when a fixed price (two or more equal charges) changed by more than 2% within the last year, so variable bills don't trigger it. A subscription "looks stopped" after 1.5 periods without a charge. Thresholds are constants in `app/recurring.py`.

**Why:** Transparent, testable, and no data leaves the machine.

**Rules out:** Sending transactions to any AI model for detection or analysis.

## No AI in Phase 1

The analysis is computed in the app. A self-hosted model on the tailnet (OpenAI-compatible API behind Open WebUI) could later power an optional "explain this" feature, but the app must keep working without it.

**Why:** Financial data stays on hardware we control, and the core features shouldn't depend on a model being up.

**Rules out:** Cloud LLM APIs for anything that sees transaction data.

## Raw imports are kept untouched

Each uploaded file is stored byte-for-byte at `data/raw/<sha256>.<ext>` and never modified. Cleaning happens only on the parsed rows in the database. The same file can't be imported twice. Rows repeated across overlapping exports are skipped by a unique key, `(owner, account, date, amount, description, n-th occurrence in the file)`.

**Why:** Imports can always be replayed or audited, and re-importing overlapping date ranges is safe.

**Rules out:** Editing transactions in place, for now.

## Deploy: a systemd user service, no container

`deploy/install.sh` makes a venv in the checkout and links `deploy/household-app.service` as a user unit, which runs uvicorn on `127.0.0.1:8000` with `UMask=0077`. `tailscale serve` publishes it on its own HTTPS port.

**Why:** It's one Python process with a SQLite file. A user service matches how other self-hosted services on the host run, needs no root, and keeps the data under the user's home with private permissions.

**Rules out:** Containers and a reverse proxy, for now, plus running as root or as a system-wide service.
