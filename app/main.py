import hashlib
import os
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import charts, db
from app.importer import UnreadableExport, parse
from app.recurring import detect, monthly_totals

APP_DIR = Path(__file__).parent
STATUSES = {"review": "To review", "cancel": "To cancel", "keep": "Keeping",
            "cancelled": "Cancelled", "not_subscription": "Not subscriptions"}
PERIODS = {"3m": ("3M", "in the last 3 months"), "6m": ("6M", "in the last 6 months"),
           "12m": ("12M", "in the last 12 months"), "ytd": ("YTD", "this year"), "all": ("All", "in total")}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["money"] = lambda cents: f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"
templates.env.filters["money_short"] = lambda cents: f"${cents / 100:,.0f}" if cents % 100 == 0 or abs(cents) >= 10000 else f"${cents / 100:,.2f}"
templates.env.filters["day"] = lambda d: f"{d:%b} {d.day}, {d.year}"
templates.env.filters["iso"] = date.fromisoformat


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        # Without this, browsers keep using a stale stylesheet after a change. The ETag keeps rechecks cheap.
        response.headers["Cache-Control"] = "no-cache"
    return response


def data_dir() -> Path:
    return Path(os.environ.get("HOUSEHOLD_DATA_DIR", "data"))


def get_db():
    conn = db.connect(data_dir() / "household.db")
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request) -> str:
    # `tailscale serve` sets this header from the tailnet identity, and the app only listens on
    # localhost, so it can't be forged from outside. The env var is for local development only.
    user = request.headers.get("Tailscale-User-Login") or os.environ.get("HOUSEHOLD_DEV_USER")
    if not user:
        raise HTTPException(403, "Open this through Tailscale.")
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
    tracked = [s for s in subs if marks.get(s.merchant) != "not_subscription"]
    months = _last_months(today, _month_count(period, today, tracked))
    spent = {s.merchant: sum(c.amount_cents for c in s.charges if c.posted_on >= months[0]) for s in subs}

    sections = {status: [] for status in STATUSES}
    for sub in subs:
        if spent[sub.merchant]:
            sections[marks.get(sub.merchant, "review")].append(sub)
    counted = [s for s in tracked if spent[s.merchant]]
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
        "savings_cents": sum(s.yearly_cents for s in subs if marks.get(s.merchant) == "cancel"),
        "increases": [s for s in counted if s.price_change and marks.get(s.merchant) != "cancelled"],
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
    sub = next(iter(detect({name: charges})), None)
    heights, ticks = charts.scale([c.amount_cents for c in charges])
    xs, path = charts.step_path(heights)
    return templates.TemplateResponse(request, "merchant.html", {
        "user": user,
        "name": name,
        "sub": sub,
        "status": db.marks(conn, user).get(name, "review"),
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
async def import_file(request: Request, file: UploadFile, account: str = Form(),
                      user=Depends(current_user), conn=Depends(get_db)):
    content = await file.read()
    filename = file.filename or "upload"
    account = " ".join(account.split())
    context = {"filename": filename}
    if not account:
        context["error"] = "Give the account a name, like “Checking” or “Travel card”."
        return templates.TemplateResponse(request, "_import_result.html", context)
    try:
        rows = parse(filename, content)
    except UnreadableExport as error:
        context["error"] = str(error)
        return templates.TemplateResponse(request, "_import_result.html", context)

    sha256 = hashlib.sha256(content).hexdigest()
    result = db.save_import(conn, user, account, filename, sha256, rows)
    if result is None:
        context["error"] = "You've already imported this exact file."
    else:
        # Keep the original export untouched; everything downstream works from the parsed copy.
        path = raw_path(sha256, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        context["added"], context["skipped"] = result
        context["imports"] = db.imports(conn, user)
    return templates.TemplateResponse(request, "_import_result.html", context)


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
def merchants(request: Request, user=Depends(current_user), conn=Depends(get_db)):
    rows = db.merchant_summary(conn, user)
    return templates.TemplateResponse(request, "merchants.html", {
        "user": user, "merchants": rows, "names": sorted({r["name"] for r in rows})})


@app.post("/merchants/rename", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
def rename(request: Request, key: str = Form(), name: str = Form(""),
           user=Depends(current_user), conn=Depends(get_db)):
    if key not in db.merchant_names(conn, user):
        raise HTTPException(404, "Unknown merchant")
    db.rename_merchant(conn, user, key, name)
    return merchants(request, user, conn)
