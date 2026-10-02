#!/usr/bin/env python3
"""
Sýnir ákvörðunarsögu paper-botnsins beint úr paper_audit.sqlite -
hvert einasta kaup/sölutilvik sem botninn mat, hvort það var samþykkt
eða hafnað, og hvers vegna.

NOTKUN
------
python view_paper_audit.py
(keyrt úr sömu möppu og user_data liggur í, t.d. C:\\Users\\bjarn\\Documents\\freqtrade)
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path("user_data/paper_audit.sqlite")


def main():
    if not DB_PATH.exists():
        print(f"Fann ekki {DB_PATH} - er paper-botninn búinn að keyra eitthvað ennþá?")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT time, event, pair, accepted, payload FROM decisions ORDER BY id DESC LIMIT 20"
    ).fetchall()

    if not rows:
        print("Engar ákvarðanir skráðar ennþá - botninn bíður eftir fyrsta merkinu.")
        return

    print(f"Síðustu {len(rows)} ákvarðanir (nýjasta efst):\n")
    for r in rows:
        payload = json.loads(r["payload"])
        status = "SAMÞYKKT" if r["accepted"] else "HAFNAÐ"
        reason = payload.get("reason", "-")
        print(f"[{r['time']}] {r['event'].upper():6} {r['pair']:12} {status:9} ástæða={reason}")
        if r["event"] == "entry":
            print(
                f"    verð={payload.get('rate')}, RSI/líkur={payload.get('model_probability')}, "
                f"kostnaður={payload.get('proposed_cost')}, laust fé={payload.get('free_balance')}"
            )

    # Samantekt á höfnunarástæðum
    all_rows = conn.execute(
        "SELECT payload FROM decisions WHERE event='entry' AND accepted=0"
    ).fetchall()
    if all_rows:
        from collections import Counter
        reasons = Counter(json.loads(r["payload"]).get("reason", "?") for r in all_rows)
        print(f"\nHöfnunarástæður samtals ({len(all_rows)} höfnuð kaup):")
        for reason, count in reasons.most_common():
            print(f"  {reason}: {count}")

    conn.close()


if __name__ == "__main__":
    main()
