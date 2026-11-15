import secrets
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

CREATE TABLE IF NOT EXISTS mail_accounts (
    owner TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    connected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Ties a Google sign-in redirect back to the person who started it.
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mail_senders (
    owner TEXT NOT NULL,
    address TEXT NOT NULL,
    name TEXT NOT NULL,
    messages INTEGER NOT NULL,
    last_seen TEXT NOT NULL,
    category TEXT NOT NULL,
    latest_subject TEXT NOT NULL,
    unsubscribe_url TEXT,
    one_click INTEGER NOT NULL,
    costs_money INTEGER NOT NULL DEFAULT 0,
    proposed TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT,
    PRIMARY KEY (owner, address)
);

CREATE TABLE IF NOT EXISTS mail_settings (
    owner TEXT PRIMARY KEY,
    new_address TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchant_categories (
    owner TEXT NOT NULL,
    merchant TEXT NOT NULL,
    category TEXT NOT NULL,
    PRIMARY KEY (owner, merchant)
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
    if "costs_money" not in {c["name"] for c in conn.execute("PRAGMA table_info(mail_senders)")}:
        conn.execute("ALTER TABLE mail_senders ADD COLUMN costs_money INTEGER NOT NULL DEFAULT 0")
    # Email actions were renamed (move_new, move_spam, trash); convert scans saved under the old names.
    conn.execute("""UPDATE mail_senders SET
        action = CASE action WHEN 'move_new' THEN 'move' WHEN 'move_spam' THEN 'keep' WHEN 'trash' THEN 'spam' ELSE action END,
        proposed = CASE proposed WHEN 'move_new' THEN 'move' WHEN 'move_spam' THEN 'keep' WHEN 'trash' THEN 'spam' ELSE proposed END
        WHERE action IN ('move_new', 'move_spam', 'trash') OR proposed IN ('move_new', 'move_spam', 'trash')""")
    conn.commit()
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


def spending_by_merchant(conn, owner, account: str | None = None) -> dict[str, list[Charge]]:
    """Money out, grouped by merchant name; only from `account` when one is given."""
    names = merchant_names(conn, owner)
    grouped = defaultdict(list)
    for r in conn.execute(
        """SELECT posted_on, amount_cents, merchant_key, account FROM transactions
           WHERE owner = ? AND amount_cents < 0 AND (? IS NULL OR account = ?)""",
        (owner, account, account),
    ):
        grouped[names[r["merchant_key"]]].append(
            Charge(date.fromisoformat(r["posted_on"]), -r["amount_cents"], r["account"]))
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
    # Carry the keep/cancel decision and category over to the new name unless that group already has them.
    conn.execute(
        """INSERT OR IGNORE INTO subscription_marks (owner, merchant, status)
           SELECT owner, ?, status FROM subscription_marks WHERE owner = ? AND merchant = ?""",
        (new_name, owner, old_name),
    )
    conn.execute(
        """INSERT OR IGNORE INTO merchant_categories (owner, merchant, category)
           SELECT owner, ?, category FROM merchant_categories WHERE owner = ? AND merchant = ?""",
        (new_name, owner, old_name),
    )
    conn.commit()


def categories(conn, owner) -> dict[str, str]:
    return dict(conn.execute(
        "SELECT merchant, category FROM merchant_categories WHERE owner = ?", (owner,)).fetchall())


def set_categories(conn, owner, chosen: dict[str, str]) -> None:
    """Set each merchant's category; an empty category clears it."""
    for merchant, category in chosen.items():
        if category:
            conn.execute(
                """INSERT INTO merchant_categories (owner, merchant, category) VALUES (?, ?, ?)
                   ON CONFLICT (owner, merchant) DO UPDATE SET category = excluded.category""",
                (owner, merchant, category),
            )
        else:
            conn.execute("DELETE FROM merchant_categories WHERE owner = ? AND merchant = ?", (owner, merchant))
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


def save_mail_account(conn, owner, email, refresh_token) -> None:
    conn.execute(
        """INSERT INTO mail_accounts (owner, email, refresh_token) VALUES (?, ?, ?)
           ON CONFLICT (owner) DO UPDATE SET email = excluded.email, refresh_token = excluded.refresh_token,
                                             connected_at = datetime('now')""",
        (owner, email, refresh_token),
    )
    conn.commit()


def mail_account(conn, owner) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM mail_accounts WHERE owner = ?", (owner,)).fetchone()


def delete_mail_account(conn, owner) -> None:
    conn.execute("DELETE FROM mail_accounts WHERE owner = ?", (owner,))
    conn.commit()


def new_oauth_state(conn, owner, prefix="") -> str:
    state = prefix + secrets.token_urlsafe(24)
    conn.execute("DELETE FROM oauth_states WHERE created_at < datetime('now', '-10 minutes')")
    conn.execute("INSERT INTO oauth_states (state, owner) VALUES (?, ?)", (state, owner))
    conn.commit()
    return state


def take_oauth_state(conn, state, owner) -> bool:
    """True once for a state this owner created in the last 10 minutes."""
    taken = conn.execute(
        "DELETE FROM oauth_states WHERE state = ? AND owner = ? AND created_at >= datetime('now', '-10 minutes')",
        (state, owner),
    ).rowcount
    conn.commit()
    return bool(taken)


def save_senders(conn, owner, senders: list[tuple]) -> None:
    """Replace this owner's senders with a fresh scan, keeping any action they chose by hand and past results."""
    previous = {r["address"]: r for r in conn.execute("SELECT * FROM mail_senders WHERE owner = ?", (owner,))}
    conn.execute("DELETE FROM mail_senders WHERE owner = ?", (owner,))
    for sender, proposed in senders:
        old = previous.get(sender.address)
        chosen = old["action"] if old and old["action"] != old["proposed"] else proposed
        conn.execute(
            """INSERT INTO mail_senders (owner, address, name, messages, last_seen, category, latest_subject,
                                         unsubscribe_url, one_click, costs_money, proposed, action, result)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner, sender.address, sender.name, sender.messages, sender.last_seen.isoformat(), sender.category,
             sender.subjects[0] if sender.subjects else "", sender.unsubscribe_url, sender.one_click,
             sender.costs_money, proposed, chosen, old["result"] if old else None),
        )
    conn.commit()


def mail_senders(conn, owner) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM mail_senders WHERE owner = ? ORDER BY messages DESC, address", (owner,)).fetchall()


def set_sender_action(conn, owner, address, action) -> None:
    conn.execute("UPDATE mail_senders SET action = ? WHERE owner = ? AND address = ?", (action, owner, address))
    conn.commit()


def set_sender_result(conn, owner, address, result) -> None:
    conn.execute("UPDATE mail_senders SET result = ? WHERE owner = ? AND address = ?", (result, owner, address))
    conn.commit()


def forget_mail(conn, owner) -> None:
    """Disconnecting removes the sign-in and everything the scan stored."""
    conn.execute("DELETE FROM mail_accounts WHERE owner = ?", (owner,))
    conn.execute("DELETE FROM mail_senders WHERE owner = ?", (owner,))
    conn.execute("DELETE FROM mail_settings WHERE owner = ?", (owner,))
    conn.commit()


def new_address(conn, owner) -> str | None:
    row = conn.execute("SELECT new_address FROM mail_settings WHERE owner = ?", (owner,)).fetchone()
    return row["new_address"] if row else None


def set_new_address(conn, owner, address: str) -> None:
    if address:
        conn.execute("""INSERT INTO mail_settings (owner, new_address) VALUES (?, ?)
                        ON CONFLICT (owner) DO UPDATE SET new_address = excluded.new_address""", (owner, address))
    else:
        conn.execute("DELETE FROM mail_settings WHERE owner = ?", (owner,))
    conn.commit()
