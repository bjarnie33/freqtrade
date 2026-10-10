#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_to_server.py - samstillir skrár úr freqtrade-möppunni á tölvunni yfir á netþjóninn (freqtrade-lon1).

Keyrt í C:\\Users\\bjarn\\Documents\\freqtrade:
  python sync_to_server.py            samanburður eingöngu - breytir engu
  python sync_to_server.py --push     sendir ólíkar skrár, tekur afrit af gömlu útgáfunum og endurræsir réttar þjónustur
  python sync_to_server.py --pull-db  sækir ferskt afrit af gagnagrunnunum af netþjóninum í from-server\\

Sendir ALDREI: .sqlite (netþjónninn á gögnin), .env (leyndarmál), logs, data.
"""
import argparse
import ast
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys

HOST = "freqtrade-lon1"
REMOTE = "~/freqtrade"

# skrá í strategies -> þjónusta sem þarf endurræsingu þegar hún breytist (óháð há-/lágstöfum)
STRATEGY_SERVICE = {
    "simplersistrategy.py": "freqtrade",
    "auditedpaperstrategy.py": "paper",
    "paper_research.py": "paper",
}
CONTAINER = {
    "freqtrade": "freqtrade",
    "paper": "freqtrade-paper-1",
    "dashboard": "freqtrade-dashboard",
    "dashboard_paper": "freqtrade-dashboard-paper",
}


def ssh(cmd, check=True):
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", HOST, cmd],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SystemExit(f"ssh mistókst ({r.returncode}): {(r.stderr or r.stdout).strip()}\n"
                         f"Er ssh {HOST} að virka? Prófaðu: ssh {HOST} \"echo ok\"")
    return r.stdout


def scp_put(local, remote_rel):
    subprocess.run(["scp", "-q", str(local), f"{HOST}:{REMOTE}/{remote_rel}"], check=True)


def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def build_items(root):
    items = []

    def add(local, remote, services, action):
        if local.is_file():
            items.append({"local": local, "remote": remote, "services": services, "action": action})

    add(root / "user_data" / "config.json", "user_data/config.json", ["freqtrade", "paper"], "restart")
    add(root / "user_data" / "config.paper.json", "user_data/config.paper.json", ["paper"], "restart")
    sdir = root / "user_data" / "strategies"
    if sdir.is_dir():
        for p in sorted(sdir.glob("*.py")):
            svc = STRATEGY_SERVICE.get(p.name.lower())
            add(p, f"user_data/strategies/{p.name}", [svc] if svc else [], "restart")
    add(root / "Freqtrade_dashboard.py", "dashboard/Freqtrade_dashboard.py", ["dashboard"], "build")
    add(root / "Freqtrade_dashboard_paper.py", "dashboard/Freqtrade_dashboard_paper.py", ["dashboard_paper"], "build")
    return items


def validate(item):
    """Skilar lista af villum. Ef eitthvað er athugavert er EKKERT sent."""
    p, errs = item["local"], []
    if p.stat().st_size == 0:
        return [f"{p.name} er tóm (0 bæti)"]
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix == ".json":
        try:
            d = json.loads(text.lstrip("\ufeff"))
        except ValueError as exc:
            return [f"{p.name} er ekki gilt JSON: {exc}"]
        for k in ("password", "jwt_secret_key"):
            v = str(d.get("api_server", {}).get(k, ""))
            if any(ord(c) > 127 for c in v):
                errs.append(f"{p.name}: api_server.{k} inniheldur íslenska/non-ASCII stafi (veldur 500-villu í innskráningu)")
    elif p.suffix == ".py":
        try:
            ast.parse(text)
        except SyntaxError as exc:
            errs.append(f"{p.name}: Python-villa í línu {exc.lineno}: {exc.msg}")
        if item["action"] == "build" and "DASHBOARD_HOST" not in text:
            errs.append(f"{p.name} er gamla útgáfan (les ekki DASHBOARD_HOST) og myndi bila í gámnum. "
                        f"Sæktu útgáfuna af netþjóninum fyrst: scp {HOST}:{REMOTE}/dashboard/{p.name} .")
    return errs


def remote_hashes(rels):
    cmd = f"cd {REMOTE} && for f in {' '.join(rels)}; do if [ -f $f ]; then sha256sum $f; else echo MISSING  $f; fi; done"
    out = {}
    for line in ssh(cmd).splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[1].strip()] = None if parts[0] == "MISSING" else parts[0]
    return out


def compare(items):
    rh = remote_hashes([i["remote"] for i in items])
    for i in items:
        r = rh.get(i["remote"])
        i["status"] = "vantar" if r is None else ("eins" if r == sha(i["local"]) else "ólík")
    return items


def warn_remote_only(items):
    remote_files = ssh(f"cd {REMOTE}/user_data/strategies 2>/dev/null && ls -1 *.py", check=False).split()
    local_names = {i["remote"].rsplit("/", 1)[1] for i in items if "/strategies/" in i["remote"]}
    for name in remote_files:
        if name not in local_names:
            same_ci = [n for n in local_names if n.lower() == name.lower()]
            if same_ci:
                print(f"  ! VARÚÐ: á netþjóninum heitir skráin {name} en hjá þér {same_ci[0]} (annað há/lágstafa). "
                      f"Báðar gætu endað á netþjóninum og Freqtrade hlaðið tveimur skrám með sama class-nafni.")
            else:
                print(f"  i Aðeins á netþjóninum (ekki snert): user_data/strategies/{name}")


MAX_RECOMMENDED_COINS = 200      # 2 GB vél: yfir þessu er hætta á minnisþurrð (óprófað, varfærið mat)


def pairlist_desc(cfg):
    pls = cfg.get("pairlists") or []
    first = pls[0] if pls and isinstance(pls[0], dict) else {}
    if first.get("method") == "VolumePairList":
        return f"VolumePairList({first.get('number_assets')})"
    n = len(((cfg.get("exchange") or {}).get("pair_whitelist")) or [])
    return f"föst pör({n})"


def coin_count(cfg):
    pls = cfg.get("pairlists") or []
    first = pls[0] if pls and isinstance(pls[0], dict) else {}
    if first.get("method") == "VolumePairList":
        try:
            return int(first.get("number_assets"))
        except (TypeError, ValueError):
            return 0
    return len(((cfg.get("exchange") or {}).get("pair_whitelist")) or [])


def key_settings(cfg):
    ex = cfg.get("exchange", {}) if isinstance(cfg.get("exchange"), dict) else {}
    return {
        "dry_run": cfg.get("dry_run"),
        "fee": cfg.get("fee"),
        "max_open_trades": cfg.get("max_open_trades"),
        "stake_amount": cfg.get("stake_amount"),
        "dry_run_wallet": cfg.get("dry_run_wallet"),
        "sandbox": ex.get("sandbox"),
        "pör (pairlist)": pairlist_desc(cfg),
        "api_server.listen_port": (cfg.get("api_server") or {}).get("listen_port"),
    }


def config_summary(items):
    """Sýnir lykilgildi í ólíkum config-skrám: netþjónn -> nýtt. Svo sést t.d. 'pör: 2 -> 126' ÁÐUR en nokkuð er sent."""
    for i in items:
        if i["status"] == "eins" or not i["remote"].endswith(".json"):
            continue
        try:
            new = key_settings(json.loads(i["local"].read_text(encoding="utf-8-sig")))
        except ValueError:
            continue
        old = None
        if i["status"] == "ólík":
            try:
                old = key_settings(json.loads(ssh(f"cat {REMOTE}/{i['remote']}", check=False)))
            except ValueError:
                old = None
        print(f"\n  Lykilgildi í {i['remote']} (netþjónn -> nýtt):")
        warns = []
        for k, v in new.items():
            o = old.get(k) if old else "(ný skrá)"
            mark = "!!" if old is not None and o != v else "  "
            print(f"   {mark} {k:26} {o} -> {v}")
        if i["remote"].endswith("config.paper.json"):
            cfg = json.loads(i["local"].read_text(encoding="utf-8-sig"))
            try:
                exposure = float(cfg.get("max_open_trades")) * float(cfg.get("stake_amount"))
                wallet = float(cfg.get("dry_run_wallet")) * float(cfg.get("tradable_balance_ratio", 0.99))
                if exposure > wallet:
                    warns.append(f"mesta áhætta er {exposure:.0f} USDT ({cfg.get('max_open_trades')} stöður x {cfg.get('stake_amount')}) "
                                 f"en veskið rúmar aðeins {wallet:.0f} USDT - síðustu stöðurnar komast ekki inn (balance_limit)")
            except (TypeError, ValueError):
                pass
            n = coin_count(cfg)
            if n > MAX_RECOMMENDED_COINS:
                warns.append(f"{n} myntir á 2 GB vél: hætta á minnisþurrð. Byrjaðu lægra og fylgstu með (docker stats)")
        for w in warns:
            print(f"   >>> ATHUGA: {w}")


DISABLED = set()   # þjónustur sem eru VILJANDI stöðvaðar (~/freqtrade/disabled-services.txt á netþjóninum)


def load_disabled():
    out = ssh(f"cat {REMOTE}/disabled-services.txt 2>/dev/null; true", check=False)
    return {l.strip() for l in out.splitlines() if l.strip() and not l.strip().startswith("#")}


def show(items):
    label = {"eins": "eins   ", "ólík": "ÓLÍK   ", "vantar": "VANTAR "}
    for i in items:
        todo = ""
        if i["status"] != "eins" and i["services"]:
            verb = "endurbyggir" if i["action"] == "build" else "endurræsir"
            act = [x for x in i["services"] if x not in DISABLED]
            off = [x for x in i["services"] if x in DISABLED]
            todo = f"   -> {verb}: {', '.join(act) if act else '-'}" + (f"  (óvirkt, sleppt: {', '.join(off)})" if off else "")
        print(f"  [{label[i['status']]}] {i['remote']}{todo}")


def push(items, assume_yes):
    changed = [i for i in items if i["status"] != "eins"]
    if not changed:
        print("\nAllt er eins og á netþjóninum. Ekkert að senda.")
        return 0
    errs = [e for i in changed for e in validate(i)]
    if errs:
        print("\nHætti við - EKKERT var sent. Lagaðu þetta fyrst:")
        for e in errs:
            print("  x", e)
        return 1
    restart = sorted({s for i in changed if i["action"] == "restart" for s in i["services"] if s not in DISABLED},
                     key=["freqtrade", "paper"].index)
    build = sorted({s for i in changed if i["action"] == "build" for s in i["services"] if s not in DISABLED})
    skipped = sorted({s for i in changed for s in i["services"] if s in DISABLED})
    config_summary(changed)
    print(f"\nÁ að senda {len(changed)} skrá/skrár. Endurræsir: {restart or '-'} | Endurbyggir: {build or '-'}"
          + (f" | Óvirkt, ekki snert: {skipped}" if skipped else ""))
    if not assume_yes and input("Halda áfram? (j/n) ").strip().lower() not in ("j", "y", "já", "ja", "yes"):
        print("Hætt við.")
        return 0

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    existing = [i["remote"] for i in changed if i["status"] == "ólík"]
    if existing:
        ssh(f"cd {REMOTE} && mkdir -p deploy-backups/{ts} && for f in {' '.join(existing)}; do "
            f"[ -f $f ] && cp -p --parents $f deploy-backups/{ts}/; done; true")
        print(f"  gamlar útgáfur vistaðar á netþjóninum: ~/freqtrade/deploy-backups/{ts}/")
    for i in changed:
        scp_put(i["local"], i["remote"])
        print(f"  sent: {i['remote']}")
    after = remote_hashes([i["remote"] for i in changed])
    bad = [i["remote"] for i in changed if after.get(i["remote"]) != sha(i["local"])]
    if bad:
        print("  x Sannprófun mistókst (innihald stemmir ekki):", ", ".join(bad))
        return 1
    print("  sannprófað: innihaldið á netþjóninum er nákvæmlega eins og hjá þér")

    if restart:
        print(f"  endurræsi: {' '.join(restart)}")
        ssh(f"cd {REMOTE} && docker compose restart {' '.join(restart)}")
    if build:
        print(f"  endurbyggi: {' '.join(build)} (tekur 1-2 mín.)")
        ssh(f"cd {REMOTE} && docker compose up -d --build {' '.join(build)}")

    names = " ".join(CONTAINER[s] for s in restart + build)
    if not names:
        print("\nEngin þjónusta var endurræst (allt sem breyttist tilheyrir óvirkum þjónustum).")
        return 0
    check = (f"sleep 20; docker ps --format 'table {{{{.Names}}}}\\t{{{{.Status}}}}'; "
             f"for c in {names}; do echo; echo -- $c; docker logs --since 2m $c 2>&1 | "
             f"grep -E 'Changing state to|keyrir|Configuration error|Fatal exception|Impossible to load|Invalid configuration|Gat ekki' | tail -3; done")
    print("\nStaða eftir 20 sekúndur:\n" + ssh(check, check=False))
    print("Athugaðu: 'RUNNING' fyrir botna og 'keyrir' fyrir mælaborð þýðir að allt fór vel. "
          "Gamla útgáfan er í deploy-backups ef þú þarft að snúa til baka.")
    return 0


def pull_db(root):
    out = ssh(f"bash {REMOTE}/backup.sh")
    files = [l.split("afrit:", 1)[1].strip() for l in out.splitlines() if "afrit:" in l]
    if not files:
        raise SystemExit("Fann engin afrit í úttaki backup.sh:\n" + out)
    dest = root / "from-server"
    dest.mkdir(exist_ok=True)
    for f in files:
        subprocess.run(["scp", "-q", f"{HOST}:{REMOTE}/{f}", str(dest)], check=True)
        print("  sótt:", dest / pathlib.PurePosixPath(f).name)
    print(f"\nAfritin eru í {dest}. Þetta eru öruggar skyndimyndir; netþjónninn heldur áfram að keyra.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Samstilla freqtrade-skrár við netþjóninn")
    ap.add_argument("--push", action="store_true", help="senda ólíkar skrár og endurræsa")
    ap.add_argument("--yes", action="store_true", help="sleppa staðfestingarspurningu")
    ap.add_argument("--pull-db", action="store_true", help="sækja afrit af gagnagrunnum af netþjóninum")
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parent), help=argparse.SUPPRESS)
    a = ap.parse_args()
    root = pathlib.Path(a.root)

    ssh("echo ok")  # prófar tenginguna strax
    if a.pull_db:
        return pull_db(root)
    DISABLED.update(load_disabled())
    if DISABLED:
        print(f"Óvirkar þjónustur á netþjóninum (ekki endurræstar né endurbyggðar): {', '.join(sorted(DISABLED))}\n")
    items = compare(build_items(root))
    print(f"Samanburður við {HOST}:~/freqtrade\n")
    show(items)
    warn_remote_only(items)
    if not a.push:
        config_summary(items)
    if a.push:
        return push(items, a.yes)
    n = sum(1 for i in items if i["status"] != "eins")
    print(f"\n{n} skrá/skrár eru ólíkar. Til að senda þær: python sync_to_server.py --push" if n else "\nAllt er eins.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
