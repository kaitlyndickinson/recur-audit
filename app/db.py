import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from app.merchants import display_name, merchant_key
from app.recurring import Charge

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    account TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (owner, sha256)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    import_id INTEGER NOT NULL REFERENCES imports(id),
    account TEXT NOT NULL,
    posted_on TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    description TEXT NOT NULL,
    merchant_key TEXT NOT NULL,
    -- Numbers identical rows within one file, so real repeats survive
    -- while the same row from an overlapping export is skipped.
    seq INTEGER NOT NULL,
    UNIQUE (owner, account, posted_on, amount_cents, description, seq)
);
CREATE INDEX IF NOT EXISTS transactions_owner_merchant ON transactions (owner, merchant_key);

CREATE TABLE IF NOT EXISTS merchant_aliases (
    owner TEXT NOT NULL,
    merchant_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    PRIMARY KEY (owner, merchant_key)
);

CREATE TABLE IF NOT EXISTS subscription_marks (
    owner TEXT NOT NULL,
    merchant TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('keep', 'cancel', 'cancelled', 'not_subscription')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (owner, merchant)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # One connection per request, but FastAPI may run its dependencies on a different thread than the route.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def save_import(conn, owner, account, filename, sha256, rows) -> tuple[int, int] | None:
    """Store parsed rows. Returns (added, skipped_duplicates), or None if this exact file was already imported."""
    try:
        import_id = conn.execute(
            "INSERT INTO imports (owner, filename, sha256, account) VALUES (?, ?, ?, ?)",
            (owner, filename, sha256, account),
        ).lastrowid
    except sqlite3.IntegrityError:
        return None
    added = add_rows(conn, owner, import_id, account, rows)
    conn.commit()
    return added, len(rows) - added


def add_rows(conn, owner, import_id, account, rows) -> int:
    """Insert rows not already stored; returns how many were new. The caller commits."""
    seen = Counter()
    added = 0
    for row in rows:
        identity = (row.posted_on, row.amount_cents, row.description)
        seq = seen[identity]
        seen[identity] += 1
        added += conn.execute(
            """INSERT OR IGNORE INTO transactions
               (owner, import_id, account, posted_on, amount_cents, description, merchant_key, seq)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner, import_id, account, row.posted_on.isoformat(), row.amount_cents,
             row.description, merchant_key(row.description), seq),
        ).rowcount
    return added


def imports(conn, owner) -> list[sqlite3.Row]:
    """Newest first, with how many transactions each one added and the dates they span."""
    return conn.execute(
        """SELECT i.id, i.filename, i.sha256, i.account, i.imported_at,
                  COUNT(t.id) AS added, MIN(t.posted_on) AS first_day, MAX(t.posted_on) AS last_day
           FROM imports i LEFT JOIN transactions t ON t.import_id = i.id
           WHERE i.owner = ? GROUP BY i.id ORDER BY i.id DESC""",
        (owner,),
    ).fetchall()


def delete_import(conn, owner, import_id) -> sqlite3.Row | None:
    """Remove an import and the transactions it added. Returns it, or None if it isn't this owner's. The caller commits."""
    row = conn.execute("SELECT * FROM imports WHERE id = ? AND owner = ?", (import_id, owner)).fetchone()
    if row:
        conn.execute("DELETE FROM transactions WHERE import_id = ?", (import_id,))
        conn.execute("DELETE FROM imports WHERE id = ?", (import_id,))
    return row


def file_in_use(conn, sha256) -> bool:
    """Whether anyone still has this file imported; both people may import the same joint statement."""
    return conn.execute("SELECT 1 FROM imports WHERE sha256 = ?", (sha256,)).fetchone() is not None


def accounts(conn, owner) -> list[str]:
    return [r["account"] for r in conn.execute(
        "SELECT DISTINCT account FROM imports WHERE owner = ? ORDER BY account", (owner,))]


def merchant_names(conn, owner) -> dict[str, str]:
    """merchant_key -> the name it's grouped under (the user's alias, else the cleaned-up key)."""
    keys = conn.execute("SELECT DISTINCT merchant_key FROM transactions WHERE owner = ?", (owner,))
    aliases = dict(conn.execute(
        "SELECT merchant_key, display_name FROM merchant_aliases WHERE owner = ?", (owner,)).fetchall())
    return {r["merchant_key"]: aliases.get(r["merchant_key"]) or display_name(r["merchant_key"]) for r in keys}


def spending_by_merchant(conn, owner) -> dict[str, list[Charge]]:
    names = merchant_names(conn, owner)
    grouped = defaultdict(list)
    for r in conn.execute(
        "SELECT posted_on, amount_cents, merchant_key FROM transactions WHERE owner = ? AND amount_cents < 0",
        (owner,),
    ):
        grouped[names[r["merchant_key"]]].append(Charge(date.fromisoformat(r["posted_on"]), -r["amount_cents"]))
    return dict(grouped)


def merchant_transactions(conn, owner, merchant) -> list[sqlite3.Row]:
    keys = [key for key, name in merchant_names(conn, owner).items() if name == merchant]
    return conn.execute(
        f"""SELECT posted_on, amount_cents, description, account FROM transactions
            WHERE owner = ? AND merchant_key IN ({",".join("?" * len(keys))})
            ORDER BY posted_on DESC""",
        (owner, *keys),
    ).fetchall()


def merchant_summary(conn, owner) -> list[dict]:
    names = merchant_names(conn, owner)
    rows = conn.execute(
        """SELECT merchant_key, COUNT(*) AS charges, -SUM(amount_cents) AS spent_cents,
                  MIN(description) AS example
           FROM transactions WHERE owner = ? AND amount_cents < 0
           GROUP BY merchant_key ORDER BY spent_cents DESC""",
        (owner,),
    )
    return [{**dict(r), "name": names[r["merchant_key"]]} for r in rows]


def rename_merchant(conn, owner, key, new_name) -> None:
    """Point a merchant key at a new group name; giving two keys the same name merges them."""
    old_name = merchant_names(conn, owner)[key]
    new_name = " ".join(new_name.split())
    if not new_name or new_name == display_name(key):
        conn.execute("DELETE FROM merchant_aliases WHERE owner = ? AND merchant_key = ?", (owner, key))
        new_name = display_name(key)
    else:
        conn.execute(
            """INSERT INTO merchant_aliases (owner, merchant_key, display_name) VALUES (?, ?, ?)
               ON CONFLICT (owner, merchant_key) DO UPDATE SET display_name = excluded.display_name""",
            (owner, key, new_name),
        )
    # Carry the keep/cancel decision over to the new name unless that group already has one.
    conn.execute(
        """INSERT OR IGNORE INTO subscription_marks (owner, merchant, status)
           SELECT owner, ?, status FROM subscription_marks WHERE owner = ? AND merchant = ?""",
        (new_name, owner, old_name),
    )
    conn.commit()


def marks(conn, owner) -> dict[str, str]:
    return dict(conn.execute(
        "SELECT merchant, status FROM subscription_marks WHERE owner = ?", (owner,)).fetchall())


def set_mark(conn, owner, merchant, status) -> None:
    if status == "review":
        conn.execute("DELETE FROM subscription_marks WHERE owner = ? AND merchant = ?", (owner, merchant))
    else:
        conn.execute(
            """INSERT INTO subscription_marks (owner, merchant, status) VALUES (?, ?, ?)
               ON CONFLICT (owner, merchant) DO UPDATE SET status = excluded.status, updated_at = datetime('now')""",
            (owner, merchant, status),
        )
    conn.commit()
