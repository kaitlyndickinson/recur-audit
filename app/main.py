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

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["money"] = lambda cents: f"{'-' if cents < 0 else ''}${abs(cents) / 100:,.2f}"
templates.env.filters["money_short"] = lambda cents: f"${cents / 100:,.0f}" if cents % 100 == 0 or abs(cents) >= 10000 else f"${cents / 100:,.2f}"
templates.env.filters["day"] = lambda d: f"{d:%b} {d.day}, {d.year}"
templates.env.filters["iso"] = date.fromisoformat


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
def subscriptions(request: Request, user=Depends(current_user), conn=Depends(get_db)):
    return _render_subscriptions(request, user, conn)


@app.post("/status", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
def set_status(request: Request, merchant: str = Form(), status: str = Form(),
               user=Depends(current_user), conn=Depends(get_db)):
    if status not in STATUSES:
        raise HTTPException(400, "Unknown status")
    db.set_mark(conn, user, merchant, status)
    return _render_subscriptions(request, user, conn)


def _render_subscriptions(request, user, conn):
    today = date.today()
    subs = detect(db.spending_by_merchant(conn, user))
    marks = db.marks(conn, user)
    sections = {status: [] for status in STATUSES}
    for sub in subs:
        sections[marks.get(sub.merchant, "review")].append(sub)

    active = [s for s in subs if marks.get(s.merchant) not in ("cancelled", "not_subscription") and not s.is_stopped(today)]
    months = _last_months(today, 12)
    tracked = [s for s in subs if marks.get(s.merchant) != "not_subscription"]
    totals = monthly_totals(tracked, months)
    heights, ticks = charts.scale(totals)
    return templates.TemplateResponse(request, "subscriptions.html", {
        "user": user,
        "today": today,
        "statuses": STATUSES,
        "sections": sections,
        "has_data": bool(db.accounts(conn, user)),
        "monthly_cents": sum(s.monthly_cents for s in active),
        "yearly_cents": sum(s.yearly_cents for s in active),
        "active_count": len(active),
        "savings_cents": sum(s.yearly_cents for s in sections["cancel"]),
        "increases": [s for s in active if s.price_change],
        "chart": {"bars": list(zip(months, totals, heights)), "ticks": ticks},
    })


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
    return templates.TemplateResponse(request, "import.html", {"user": user, "accounts": db.accounts(conn, user)})


@app.post("/import", response_class=HTMLResponse, dependencies=[Depends(htmx_only)])
async def import_file(request: Request, file: UploadFile, account: str = Form(),
                      user=Depends(current_user), conn=Depends(get_db)):
    content = await file.read()
    account = " ".join(account.split())
    context = {"filename": file.filename}
    if not account:
        context["error"] = "Give the account a name, like “Checking” or “Travel card”."
        return templates.TemplateResponse(request, "_import_result.html", context)
    try:
        rows = parse(file.filename or "", content)
    except UnreadableExport as error:
        context["error"] = str(error)
        return templates.TemplateResponse(request, "_import_result.html", context)

    sha256 = hashlib.sha256(content).hexdigest()
    result = db.save_import(conn, user, account, file.filename or "upload", sha256, rows)
    if result is None:
        context["error"] = "This exact file was already imported."
    else:
        # Keep the original export untouched; everything downstream works from the parsed copy.
        raw_path = data_dir() / "raw" / f"{sha256}{Path(file.filename or '').suffix.lower()}"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(content)
        context["added"], context["skipped"] = result
    return templates.TemplateResponse(request, "_import_result.html", context)


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
