"""Rewrite open pairs from Alpaca's own position, so ledger == broker.

Run once on the Pi after deploying the broker-backed engine. Idempotent.
"""
import sqlite3

from alpaca_client import AlpacaClient

DB = "/var/lib/ares/ares_state.sqlite3"


def main() -> None:
    client = AlpacaClient()
    account = client.account()
    held = {p["symbol"].replace("/", ""): p for p in client.positions()}

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM dn_positions WHERE status='open'").fetchall()

    start = float(db.execute("SELECT value FROM kv WHERE key='dn_start_equity'").fetchone()[0])
    basis_total = 0.0
    modelled = 0.0
    funding = 0.0
    for row in rows:
        position = held.get(row["symbol"].replace("/", ""))
        if not position:
            print(f"NO BROKER POSITION {row['symbol']} - leaving the row alone")
            continue
        qty = float(position["qty"])
        entry = float(position["avg_entry_price"])
        basis = float(position["cost_basis"])
        perp_qty = basis / row["perp_entry"]
        db.execute(
            "UPDATE dn_positions SET spot_qty=?, spot_entry=?, notional=?, perp_qty=? WHERE id=?",
            (qty, entry, basis, perp_qty, row["id"]),
        )
        basis_total += basis
        modelled += basis * 0.00045
        funding += float(row["funding"] or 0)
        print(f"{row['coin']}: qty {qty:.9g}  entry {entry:.6g}  basis ${basis:,.2f}  "
              f"perp_qty {perp_qty:.9g}")

    cash = float(account["cash"])
    spot_fees = max(0.0, start - cash - basis_total)  # what Alpaca actually charged
    values = {
        "dn_cash": cash,
        "dn_fees_all": spot_fees + modelled,
        "dn_perp_fees": modelled,
        "dn_funding_all": funding,
        "dn_funding_today": funding,
        "dn_perp_realized": 0.0,
    }
    for key, value in values.items():
        db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, f"{value:.8f}"))
    db.commit()
    print(f"cash ${cash:,.2f}  spot fees ${spot_fees:,.2f}  perp fees ${modelled:,.2f}  "
          f"funding ${funding:,.4f}")


if __name__ == "__main__":
    main()
