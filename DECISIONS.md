# Decisions

How the app is built and why. Edit an entry when it changes; this describes the current state, not history.

## Stack

FastAPI rendering Jinja templates, htmx for in-place updates, one hand-written CSS file, stdlib `sqlite3` with plain SQL in `app/db.py`. One language, no JS build, nothing to administer; backup is copying one folder. httpx is the only HTTP client (Google and unsubscribe links), with no Google libraries.

## Identity: Tailscale, no login code

The app is only reachable through the host's `127.0.0.1`; `tailscale serve` publishes it and sets `Tailscale-User-Login`, which the app trusts. Requests without it get a 403, unless `RECUR_AUDIT_LOCAL_USER` is set for a single-person install on your own computer; that must never be set when the app is served to a household. State-changing requests require htmx's `HX-Request` header, which blocks cross-site posts without tokens or cookies. Requests are only answered for `localhost`, `127.0.0.1` and `*.ts.net` hosts, which blocks DNS rebinding. The app owns no passwords or sessions. Settings and secrets come from environment variables or a gitignored `.env`, never the command line.

## Each person sees only their own data

Every row has an `owner` (the Tailscale login) and every query filters on it: statements, subscriptions, merchant names, email cleanup. There's no shared or combined view.

## Statements

Uploaded files are stored byte-for-byte at `data/raw/<sha256>.<ext>`; cleaning happens only on parsed rows. The same file can't be imported twice, and rows repeated across overlapping statements are skipped by `(owner, account, date, amount, description, n-th occurrence in the file)`. Deleting a statement removes its transactions and stored file (unless someone else imported the same file), then re-reads the remaining files so shared rows survive.

## Merchants and subscriptions

`merchants.merchant_key()` cleans bank descriptions; renames on the Merchants page override it, and giving two keys one name merges them. Subscription detection is plain math with thresholds as constants in `app/recurring.py`: 3+ charges, regular gaps (weekly to yearly, ±20%), stable amounts (75% within 25%). It judges the latest regular run of charges, as long as that's a third of them, so a move or plan change doesn't hide a subscription. A merchant that bills several plans under one name (Apple, Google Play) is split by price and each plan is checked on its own. Monthly-or-longer charges that are regular 6+ times but change in amount, like a card autopay, count as variable bills. No AI is involved in detection.

## Categories

Each merchant name has one of a fixed list of categories (`app/categories.py`), set on the Merchants page. Suggest categories asks a language model you host yourself (any OpenAI-compatible server, set by `RECUR_AUDIT_LLM_URL`) to fill in the uncategorized ones; it only runs when you click it. The model sees each merchant's name and one bank description, never amounts, dates or accounts, and answers outside the list are dropped. The URL must be a server you run, never a hosted AI service. In the container, reaching a model on the host's `127.0.0.1` needs Podman's `pasta:--map-host-loopback` network option, which also lets the app reach the host's other local services.

## Email cleanup

Two separate Google permissions, so the saved credential can never read your mail:

- **Read** (`gmail.metadata`): the only sign-in that's saved, as a refresh token in the database. Google itself limits it to headers and labels; it can't fetch bodies or search, so the scan pages through the mailbox newest first and stops at the chosen period (1, 3 or 12 months; 3 by default). Gmail's budget is 6,000 quota units a minute per mailbox and each header read costs 20, about 300 emails a minute, so calls are paced by cost to 90% of it.
- **Write** (`gmail.modify`): requested when you click Clean up, with `access_type=online` so Google issues no refresh token. The access token is held in memory by that one job and expires within an hour. It's never revoked early, because Google revokes every grant for the project at once, which would also disconnect the read sign-in. Approval from a different account than the connected one is refused.

Each sender gets one of four actions (`app/mail.py`, provider-neutral rules): **Move to** your new address (a checklist you tick Done; changing your email at a service can't be automated), **Unsubscribe & trash**, **Spam** (moved to Spam; only RFC 8058 one-click senders also get unsubscribed, as Gmail does), or **Keep**. Senders billing you on repeat are grouped under **$ Charging you**: at least 2 subjects with billing phrases (receipt, invoice, renewal, autopay, payment received) from a sender Gmail doesn't file as marketing. Prices and words like "subscription" alone don't count, since ads are full of them. Clean up carries out the unsubscribe, trash and spam actions, one sender at a time: if Gmail refuses a sender, the rest still go through, and that sender stays pending for the next run. It never sends mail or deletes permanently. Unsubscribe URLs come from emails, so the app only POSTs to public HTTPS hosts, never private, loopback or tailnet addresses. Disconnecting revokes access and deletes what the scan stored.

## Look

Dark only: ink-navy, cyan accent, pink for price increases. Space Grotesk for text and JetBrains Mono for amounts, dates and labels, served from `app/static/fonts/` (SIL Open Font License), never a CDN.

## Deploy: one container

`Dockerfile` and `compose.yaml` are the only install, so it runs the same on Linux, Windows, macOS and NAS boxes, with Docker or Podman. The app runs as a non-root user, keeps data in a named volume at `/data`, and is published on `127.0.0.1:8000` only. Settings and secrets come from a gitignored `.env`. `tailscale serve` on the host publishes it on its own HTTPS port; never `funnel`.
