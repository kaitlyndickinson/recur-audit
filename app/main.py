import hashlib
import logging
import os
import re
import threading
from datetime import date, timedelta
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import categories, charts, db, gmail, mail
from app.importer import UnreadableExport, parse
from app.recurring import detect, monthly_totals

APP_DIR = Path(__file__).parent
log = logging.getLogger("recur-audit")
STATUSES = {"review": "To review", "cancel": "To cancel", "keep": "Keeping",
            "cancelled": "Cancelled", "not_subscription": "Not subscriptions"}
PERIODS = {"3m": ("3M", "in the last 3 months"), "6m": ("6M", "in the last 6 months"),
           "12m": ("12M", "in the last 12 months"), "ytd": ("YTD", "this year"), "all": ("All", "in total")}


def load_env_file(path: Path) -> None:
    """Read KEY=value settings from a .env file, as Docker does, without overriding anything already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.startswith("#"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        os.environ.setdefault(key.strip(), value)


load_env_file(Path(".env"))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
# Only answer requests addressed to this machine or the tailnet. Without this, a web page could point
# its own hostname at 127.0.0.1 (DNS rebinding) and read the app as you.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "*.ts.net"])
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["money"] = lambda cents: f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"
templates.env.filters["money_short"] = lambda cents: f"${cents / 100:,.0f}" if cents % 100 == 0 or abs(cents) >= 10000 else f"${cents / 100:,.2f}"
templates.env.filters["day"] = lambda d: f"{d:%b} {d.day}, {d.year}"
templates.env.filters["iso"] = date.fromisoformat
# A sender's likely website, for changing your email there: news@mail.streamflix.com -> streamflix.com.
# Two-part country domains (shop.co.uk) come out wrong, but it's only a shortcut link.
templates.env.filters["website"] = lambda address: ".".join(address.rpartition("@")[2].split(".")[-2:])


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        # Without this, browsers keep using a stale stylesheet after a change. The ETag keeps rechecks cheap.
        response.headers["Cache-Control"] = "no-cache"
    return response


def data_dir() -> Path:
    return Path(os.environ.get("RECUR_AUDIT_DATA_DIR", "data"))


def get_db():
    conn = db.connect(data_dir() / "recur-audit.db")
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request) -> str:
    # `tailscale serve` sets this header from the tailnet identity, and the app is only reachable
    # through localhost, so it can't be forged from outside. The env var is for running it just on
    # your own computer, without Tailscale.
    user = request.headers.get("Tailscale-User-Login") or os.environ.get("RECUR_AUDIT_LOCAL_USER")
    if not user:
        raise HTTPException(403, "Open this through Tailscale, or set RECUR_AUDIT_LOCAL_USER to run it on your own computer.")
    return user


def htmx_only(request: Request) -> None:
    # A cross-site form can't set custom headers, so requiring htmx's header blocks CSRF without tokens.
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(403, "Forbidden")


@app.get("/", response_class=HTMLResponse)
def subscriptions(request: Request, period: str = "12m", account: str = "",
                  user=Depends(current_user), conn=Depends(get_db)):
    return _render_subscriptions(request, user, conn, period, account)


@app.post("/status", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
def set_status(request: Request, merchant: str = Form(), status: str = Form(),
               period: str = Form("12m"), account: str = Form(""),
               user=Depends(current_user), conn=Depends(get_db)):
    if status not in STATUSES:
        raise HTTPException(400, "Unknown status")
    db.set_mark(conn, user, merchant, status)
    return _render_subscriptions(request, user, conn, period, account)


def _render_subscriptions(request, user, conn, period, account):
    today = date.today()
    if period not in PERIODS:
        period = "12m"
    accounts = db.accounts(conn, user)
    if account not in accounts:
        account = ""
    subs = detect(db.spending_by_merchant(conn, user, account or None))
    marks = db.marks(conn, user)
    tracked = [s for s in subs if marks.get(s.name) != "not_subscription"]
    months = _last_months(today, _month_count(period, today, tracked))
    spent = {s.name: sum(c.amount_cents for c in s.charges if c.posted_on >= months[0]) for s in subs}

    sections = {status: [] for status in STATUSES}
    for sub in subs:
        if spent[sub.name]:
            sections[marks.get(sub.name, "review")].append(sub)
    counted = [s for s in tracked if spent[s.name]]
    totals = monthly_totals(tracked, months)
    heights, ticks = charts.scale(totals)
    return templates.TemplateResponse(request, "subscriptions.html", {
        "user": user,
        "today": today,
        "statuses": STATUSES,
        "sections": sections,
        "has_data": bool(accounts),
        "accounts": accounts,
        "account": account,
        "has_subscriptions": bool(subs),
        "spent": spent,
        "period_cents": sum(totals),
        "monthly_average_cents": sum(totals) // len(months),
        "counted": len(counted),
        # What cancelling saves from here on, so it doesn't depend on the period being viewed.
        "savings_cents": sum(s.yearly_cents for s in subs if marks.get(s.name) == "cancel"),
        "increases": [s for s in counted if s.price_change and marks.get(s.name) != "cancelled"],
        "chart": {"bars": list(zip(months, totals, heights)), "ticks": ticks},
        "periods": PERIODS,
        "period": period,
    })


def _month_count(period: str, today: date, subs) -> int:
    if period == "ytd":
        return today.month
    if period == "all":
        first = min((s.charges[0].posted_on for s in subs), default=today)
        return (today.year - first.year) * 12 + today.month - first.month + 1
    return int(period.removesuffix("m"))


def _last_months(today: date, count: int) -> list[date]:
    months = []
    year, month = today.year, today.month
    for _ in range(count):
        months.append(date(year, month, 1))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return months[::-1]


@app.get("/merchant", response_class=HTMLResponse)
def merchant_detail(request: Request, name: str, user=Depends(current_user), conn=Depends(get_db)):
    transactions = db.merchant_transactions(conn, user, name)
    if not transactions:
        raise HTTPException(404, "No charges for that merchant")
    charges = sorted(db.spending_by_merchant(conn, user).get(name, []), key=lambda c: c.posted_on)
    marks = db.marks(conn, user)
    subs = [(sub, marks.get(sub.name, "review")) for sub in detect({name: charges})]
    heights, ticks = charts.scale([c.amount_cents for c in charges])
    xs, path = charts.step_path(heights)
    return templates.TemplateResponse(request, "merchant.html", {
        "user": user,
        "name": name,
        "subs": subs,
        "statuses": STATUSES,
        "today": date.today(),
        "transactions": transactions,
        "chart": {"points": list(zip(charges, xs, heights)), "path": path, "ticks": ticks},
    })


@app.post("/merchant/status", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
def set_merchant_status(request: Request, merchant: str = Form(), status: str = Form(),
                        user=Depends(current_user), conn=Depends(get_db)):
    if status not in STATUSES:
        raise HTTPException(400, "Unknown status")
    db.set_mark(conn, user, merchant, status)
    return templates.TemplateResponse(request, "_status_picker.html", {
        "name": merchant, "status": status})


@app.get("/import", response_class=HTMLResponse)
def import_form(request: Request, user=Depends(current_user), conn=Depends(get_db)):
    return templates.TemplateResponse(request, "import.html", {
        "user": user, "accounts": db.accounts(conn, user), "imports": db.imports(conn, user)})


def raw_path(sha256: str, filename: str) -> Path:
    return data_dir() / "raw" / f"{sha256}{Path(filename).suffix.lower()}"


@app.post("/import", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
async def import_files(request: Request, file: list[UploadFile], account: str = Form(),
                       user=Depends(current_user), conn=Depends(get_db)):
    account = " ".join(account.split())
    if not account:
        results = [{"error": "Give the account a name, like “Checking” or “Travel card”."}]
    else:
        results = [import_one(conn, user, account, upload.filename or "upload", await upload.read()) for upload in file]
    imported = any("added" in r for r in results)
    return templates.TemplateResponse(request, "_import_result.html", {
        "results": results, "imports": db.imports(conn, user) if imported else None})


def import_one(conn, user: str, account: str, filename: str, content: bytes) -> dict:
    """Import one statement; a bad file only fails itself."""
    try:
        rows = parse(filename, content)
    except UnreadableExport as error:
        return {"filename": filename, "error": str(error)}
    sha256 = hashlib.sha256(content).hexdigest()
    result = db.save_import(conn, user, account, filename, sha256, rows)
    if result is None:
        return {"filename": filename, "error": "You've already imported this exact file."}
    # Keep the original export untouched; everything downstream works from the parsed copy.
    path = raw_path(sha256, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"filename": filename, "added": result[0], "skipped": result[1]}


@app.post("/import/delete", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
def delete_import(request: Request, import_id: int = Form(), user=Depends(current_user), conn=Depends(get_db)):
    deleted = db.delete_import(conn, user, import_id)
    if deleted is None:
        raise HTTPException(404, "No such import")
    # Overlapping statements share rows, and each row belongs to whichever import added it first.
    # Re-reading the remaining files, oldest first, restores rows the deleted import was holding for them.
    for kept in reversed(db.imports(conn, user)):
        path = raw_path(kept["sha256"], kept["filename"])
        if path.exists():
            db.add_rows(conn, user, kept["id"], kept["account"], parse(kept["filename"], path.read_bytes()))
    conn.commit()
    if not db.file_in_use(conn, deleted["sha256"]):
        raw_path(deleted["sha256"], deleted["filename"]).unlink(missing_ok=True)
    return templates.TemplateResponse(request, "_imports.html", {"imports": db.imports(conn, user)})


@app.get("/merchants", response_class=HTMLResponse)
def merchants(request: Request, category: str = "", user=Depends(current_user), conn=Depends(get_db)):
    return _render_merchants(request, user, conn, category)


def _render_merchants(request, user, conn, category="", error=None, suggested=None):
    """`category` filters the list: one of CATEGORIES, "none" for uncategorized, or empty for all."""
    rows = db.merchant_summary(conn, user)
    chosen = db.categories(conn, user)
    counts = {name: sum(chosen.get(r["name"]) == name for r in rows) for name in categories.CATEGORIES}
    counts["none"] = sum(r["name"] not in chosen for r in rows)
    if category not in counts:
        category = ""
    if category:
        rows = [r for r in rows if chosen.get(r["name"], "none") == category]
    return templates.TemplateResponse(request, "merchants.html", {
        "user": user, "merchants": rows, "chosen": chosen, "categories": categories.CATEGORIES,
        "counts": counts, "category": category, "can_suggest": categories.configured(), "error": error,
        "suggested": suggested})


@app.post("/merchants/rename", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
def rename(request: Request, key: str = Form(), name: str = Form(""), filter: str = Form(""),
           user=Depends(current_user), conn=Depends(get_db)):
    if key not in db.merchant_names(conn, user):
        raise HTTPException(404, "Unknown merchant")
    db.rename_merchant(conn, user, key, name)
    return _render_merchants(request, user, conn, filter)


@app.post("/merchants/category", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
def set_category(request: Request, merchant: str = Form(), category: str = Form(""), filter: str = Form(""),
                 user=Depends(current_user), conn=Depends(get_db)):
    if category and category not in categories.CATEGORIES:
        raise HTTPException(400, "Unknown category")
    if merchant not in db.merchant_names(conn, user).values():
        raise HTTPException(404, "Unknown merchant")
    db.set_categories(conn, user, {merchant: category})
    return _render_merchants(request, user, conn, filter)


@app.post("/merchants/suggest", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
def suggest_categories(request: Request, filter: str = Form(""), user=Depends(current_user), conn=Depends(get_db)):
    if not categories.configured():
        raise HTTPException(400, "No model is set up on this server.")
    chosen = db.categories(conn, user)
    todo = {}
    for row in db.merchant_summary(conn, user):
        if row["name"] not in chosen:
            todo.setdefault(row["name"], row["example"])
    try:
        found = categories.suggest(list(todo.items()))
    except categories.ModelUnavailable as error:
        log.warning("category suggestions failed: %s", error)
        return _render_merchants(request, user, conn, filter, error="Couldn’t reach the model. Is it running?")
    db.set_categories(conn, user, found)
    message = f"Suggested a category for {len(found)} merchant{'' if len(found) == 1 else 's'}."
    if len(found) < len(todo):
        message += f" {len(todo) - len(found)} still need one; pick those yourself."
    return _render_merchants(request, user, conn, filter, suggested=message)


# ---------- Email cleanup ----------

# One background job (scan or cleanup) per person, kept in memory: there's a single server process.
JOBS: dict[str, dict] = {}


def start_thread(work) -> None:
    threading.Thread(target=work, daemon=True).start()


def run_job(owner: str, kind: str, work) -> None:
    if owner in JOBS and not JOBS[owner]["finished"]:
        raise HTTPException(409, "Still working on the last request.")
    job = JOBS[owner] = {"kind": kind, "done": 0, "total": 0, "error": None, "failures": [], "finished": False}

    def progress(done, total):
        job["done"], job["total"] = done, total

    def run():
        conn = db.connect(data_dir() / "recur-audit.db")
        try:
            job["failures"] = work(conn, progress) or []
        except gmail.SignInExpired:
            db.delete_mail_account(conn, owner)
            job["error"] = "Google ended the sign-in. Connect again to continue."
        except gmail.GoogleError as error:
            job["error"] = f"Google said: {error}"
        except Exception as error:  # shown on the page instead of dying silently in a thread
            log.exception("email %s failed", kind)
            job["error"] = f"Something went wrong ({type(error).__name__}). Details are in the server log."
        finally:
            job["finished"] = True
            conn.close()

    start_thread(run)


def redirect_uri(request: Request) -> str:
    # Behind `tailscale serve` the app can't see its public address, so the server sets it.
    return os.environ.get("RECUR_AUDIT_PUBLIC_URL", str(request.base_url)).rstrip("/") + "/email/callback"


@app.get("/email", response_class=HTMLResponse)
def email(request: Request, user=Depends(current_user), conn=Depends(get_db)):
    senders = db.mail_senders(conn, user)
    moved = [s for s in senders if s["action"] == "move" and s["result"] == MOVED]
    grouped = {action: [s for s in senders if s["action"] == action and s not in moved] for action in mail.ACTIONS}
    pending = [s for s in senders if s["action"] in CLEAN_UP and not s["result"]]
    new_address = db.new_address(conn, user)
    # Senders billing you on repeat. Scans saved before this was worked out only kept each sender's latest
    # subject, so for those: a non-marketing sender with several emails whose latest one reads like a bill.
    charging = [s for s in senders if s["costs_money"] or (
        s["messages"] >= mail.MIN_BILLS and mail.looks_like_billing([s["latest_subject"]], s["category"], needed=1))]
    costs = {s["address"] for s in charging}
    for rows in grouped.values():
        rows.sort(key=lambda s: s["address"] not in costs)  # $ senders first; the sort keeps email-count order within each
    job = JOBS.get(user)
    return templates.TemplateResponse(request, "email.html", {
        "user": user,
        "configured": gmail.configured(),
        "account": db.mail_account(conn, user),
        "job": job if job and not job["finished"] else None,
        "error": job["error"] if job else None,
        "failures": job["failures"] if job and job["finished"] else [],
        "new_address": new_address,
        "actions": {**mail.ACTIONS, **({"move": f"Move to {new_address}"} if new_address else {})},
        "grouped": grouped,
        "moved": moved,
        "charging": charging,
        "costs": costs,
        "has_senders": bool(senders),
        "pending_unsubscribe": sum(s["action"] == "unsubscribe" for s in pending),
        "pending_spam": sum(s["action"] == "spam" for s in pending),
    })


SCAN_MONTHS = (1, 3, 12)
CLEAN_UP = ("unsubscribe", "spam")  # the actions Clean up carries out; "move" is a checklist, "keep" does nothing
MOVED = "Moved"  # a "move" row's result once its Done box is ticked
WRITE_STATE = "write:"  # marks the one-time Clean up approval, as opposed to connecting


@app.get("/email/connect")
def email_connect(request: Request, user=Depends(current_user), conn=Depends(get_db)):
    if not gmail.configured():
        raise HTTPException(400, "Google sign-in isn't set up on this server yet.")
    return RedirectResponse(gmail.sign_in_url(db.new_oauth_state(conn, user), redirect_uri(request)), 303)


@app.get("/email/callback")
def email_callback(request: Request, state: str = "", code: str = "",
                   user=Depends(current_user), conn=Depends(get_db)):
    # The state proves this redirect finishes a sign-in this person started here, not one forged elsewhere.
    if not db.take_oauth_state(conn, state, user):
        raise HTTPException(400, "This sign-in link expired. Start again from the Email page.")
    if not code:  # cancelled on Google's side
        return RedirectResponse("/email", 303)
    address, tokens = gmail.finish_sign_in(code, redirect_uri(request))
    if state.startswith(WRITE_STATE):
        account = db.mail_account(conn, user)
        if not account or address.lower() != account["email"].lower():
            raise HTTPException(400, f"That approval was for {address}, not the connected address. Try Clean up again.")
        start_clean(user, tokens["access_token"])  # handed to the job only; never saved
    else:
        db.save_mail_account(conn, user, address, tokens["refresh_token"])
    return RedirectResponse("/email", 303)


@app.post("/email/disconnect", dependencies=[Depends(htmx_only)])
def email_disconnect(user=Depends(current_user), conn=Depends(get_db)):
    account = db.mail_account(conn, user)
    if account:
        try:
            gmail.revoke(account["refresh_token"])
        except httpx.HTTPError:
            pass  # forgetting it here matters more; it can also be revoked from the Google account
    db.forget_mail(conn, user)
    return HTMLResponse(headers={"HX-Refresh": "true"})


@app.post("/email/scan", dependencies=[Depends(htmx_only)])
def email_scan(months: int = Form(3), user=Depends(current_user), conn=Depends(get_db)):
    # Gmail allows about 270 header reads a minute, so shorter periods scan much faster. Senders still
    # emailing you show up within a few months, and Clean up trashes their mail from every year anyway.
    if months not in SCAN_MONTHS:
        raise HTTPException(400, "Scan the last 1, 3 or 12 months.")
    account = db.mail_account(conn, user)
    if not account:
        raise HTTPException(400, "Connect your email first.")

    def work(conn, progress):
        with gmail.http() as client:
            box = gmail.Gmail(client, gmail.read_access(client, account["refresh_token"]))
            messages = box.scan(date.today() - timedelta(days=round(30.44 * months)), progress)
        paid = {sub.merchant for sub in detect(db.spending_by_merchant(conn, user))}
        db.save_senders(conn, user, [(s, mail.propose(s, paid)) for s in mail.group(messages)])

    run_job(user, "scan", work)
    return HTMLResponse(headers={"HX-Refresh": "true"})


@app.post("/email/new-address", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
def email_new_address(request: Request, new_address: str = Form(""),
                      user=Depends(current_user), conn=Depends(get_db)):
    new_address = new_address.strip()
    if new_address and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", new_address):
        raise HTTPException(400, "That doesn't look like an email address.")
    db.set_new_address(conn, user, new_address)
    return email(request, user, conn)


@app.post("/email/done", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
def email_done(request: Request, address: str = Form(), done: bool = Form(),
               user=Depends(current_user), conn=Depends(get_db)):
    db.set_sender_result(conn, user, address, MOVED if done else None)
    return email(request, user, conn)


@app.post("/email/action", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
def email_action(request: Request, address: str = Form(), action: str = Form(),
                 user=Depends(current_user), conn=Depends(get_db)):
    if action not in mail.ACTIONS:
        raise HTTPException(400, "Unknown action")
    db.set_sender_action(conn, user, address, action)
    return email(request, user, conn)


@app.post("/email/clean", dependencies=[Depends(htmx_only)])
def email_clean(request: Request, user=Depends(current_user), conn=Depends(get_db)):
    """Ask Google for one-time write access; the cleanup starts when it comes back to /email/callback."""
    account = db.mail_account(conn, user)
    if not account:
        raise HTTPException(400, "Connect your email first.")
    if user in JOBS and not JOBS[user]["finished"]:
        raise HTTPException(409, "Still working on the last request.")
    state = db.new_oauth_state(conn, user, prefix=WRITE_STATE)
    url = gmail.sign_in_url(state, redirect_uri(request), write=True, account=account["email"])
    return HTMLResponse(headers={"HX-Redirect": url})


def start_clean(user: str, access_token: str) -> None:
    def work(conn, progress):
        todo = [s for s in db.mail_senders(conn, user) if s["action"] in CLEAN_UP and not s["result"]]
        progress(0, len(todo))
        failures = []
        with gmail.http() as client:
            box = gmail.Gmail(client, access_token)
            for done, sender in enumerate(todo, start=1):
                # One sender Gmail refuses shouldn't stop the rest. It keeps no result, so the next run retries it.
                try:
                    db.set_sender_result(conn, user, sender["address"], clean_up_sender(box, sender))
                except gmail.GoogleError as error:
                    log.warning("clean up of %s failed: %s", sender["address"], error)
                    failures.append(f"{sender['address']}: {error}")
                progress(done, len(todo))
        return failures

    run_job(user, "clean", work)


def clean_up_sender(box: gmail.Gmail, sender) -> str:
    """Carry out one sender's action and describe what happened."""
    notes = []
    unsubscribed = bool(sender["one_click"]) and gmail.one_click_unsubscribe(sender["unsubscribe_url"])
    if sender["action"] == "unsubscribe":
        if unsubscribed:
            notes.append("Unsubscribed")
        else:
            notes.append("Unsubscribe yourself" if sender["unsubscribe_url"] else "No unsubscribe link")
        moved = box.move_from(sender["address"], "TRASH")
        notes.append(f"{moved} email{'' if moved == 1 else 's'} trashed")
    else:  # spam: like Gmail's Report spam, only verified one-click senders also get unsubscribed
        if unsubscribed:
            notes.append("Unsubscribed")
        moved = box.move_from(sender["address"], "SPAM")
        notes.append(f"{moved} email{'' if moved == 1 else 's'} moved to Spam")
    return " · ".join(notes)


@app.get("/email/progress", response_class=HTMLResponse)
def email_progress(request: Request, user=Depends(current_user)):
    job = JOBS.get(user)
    if not job or job["finished"]:
        return HTMLResponse(headers={"HX-Refresh": "true"})
    return templates.TemplateResponse(request, "_email_progress.html", {"job": job})
