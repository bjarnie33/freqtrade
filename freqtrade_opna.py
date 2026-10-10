#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
freqtrade_opna.py - EIN skipun sem opnar gönguna til netþjónsins, sýnir stöðu verkefnisins
og opnar mælaborðin í vafranum.

  python freqtrade_opna.py                    opnar gönguna, sýnir stöðu, opnar vafra
  python freqtrade_opna.py --no-browser       sama án þess að opna vafra
  python freqtrade_opna.py --auto             fyrir sjálfvirka ræsingu (bíður lengur eftir neti)
  python freqtrade_opna.py --install-autostart     ræsist sjálfkrafa þegar þú skráir þig inn í Windows
  python freqtrade_opna.py --uninstall-autostart   fjarlægir sjálfvirku ræsinguna

Skriftan breytir engu á netþjóninum - hún les aðeins stöðu.
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser

HOST = "freqtrade-lon1"
ROOT = pathlib.Path(__file__).resolve().parent
URLS = {"Aðalbotn": "http://localhost:15050", "Paper-botn": "http://localhost:15051"}
EXPECTED = ["freqtrade", "freqtrade-paper-1", "freqtrade-dashboard", "freqtrade-dashboard-paper"]

# Keyrt Á NETÞJÓNINUM (sent inn á stdin, svo engin gæsalappavandamál): les bara stöðu, skrifar ekkert.
REMOTE_SCRIPT = r'''
import json, os, sqlite3, subprocess
os.chdir(os.path.expanduser("~/freqtrade"))
def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()
out = {}
out["uptime"] = sh("uptime -p")
out["mem"] = sh("free -m | awk '/Mem/ {print $3 \"/\" $2}'")
out["swap"] = sh("free -m | awk '/Swap/ {print $3 \"/\" $2}'")
out["disk"] = sh("df -h / | awk 'NR==2 {print $5}'")
out["containers"] = [l.split("|", 1) for l in sh("docker ps --format '{{.Names}}|{{.Status}}'").splitlines() if "|" in l]
def beats(name):
    r = sh("docker logs --since 3m %s 2>&1 | grep -c 'Bot heartbeat'" % name)
    return int(r) if r.isdigit() else 0
out["heartbeats"] = {"freqtrade": beats("freqtrade"), "freqtrade-paper-1": beats("freqtrade-paper-1")}
def trades(db):
    try:
        c = sqlite3.connect("file:user_data/%s?mode=ro" % db, uri=True)
        n, pnl, wins = c.execute("select count(*), coalesce(sum(close_profit_abs),0), coalesce(sum(close_profit_abs>0),0) from trades where is_open=0").fetchone()
        opn = c.execute("select pair, substr(open_date,1,16) from trades where is_open=1 order by id").fetchall()
        last = c.execute("select pair, substr(open_date,1,16), substr(coalesce(close_date,''),1,16), round(close_profit_abs,2), coalesce(exit_reason,'opin') from trades order by id desc limit 3").fetchall()
        return {"closed": n, "pnl": round(pnl, 2), "wins": wins, "open": opn, "last": last}
    except Exception as e:
        return {"error": str(e)}
out["main"] = trades("tradesv3.sqlite")
out["paper"] = trades("paper_trades.sqlite")
try:
    c = sqlite3.connect("file:user_data/paper_audit.sqlite?mode=ro", uri=True)
    out["candles"] = c.execute("select count(*) from candles").fetchone()[0]
    out["last_candle"] = c.execute("select max(date) from candles").fetchone()[0]
except Exception as e:
    out["candles_error"] = str(e)
out["backups"] = sh("ls backups/*.sqlite 2>/dev/null | wc -l")
print(json.dumps(out))
'''


def say(msg=""):
    print(msg, flush=True)


def tunnel_ok(port=15050):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://localhost:{port}/", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start_tunnel():
    """Ræsir freqtrade-tunnel.bat í lágmörkuðum glugga (heldur gönguna uppi og endurtengir)."""
    bat = ROOT / "freqtrade-tunnel.bat"
    if not bat.is_file():
        raise SystemExit(f"Vantar {bat}. Pakkaðu út opna-pakki.zip í {ROOT}.")
    log = ROOT / "tunnel.log"
    if log.is_file() and log.stat().st_size > 200_000:
        log.unlink()
    subprocess.Popen(["cmd", "/c", "start", "", "/min", str(bat)])


def port_states():
    return {15050: tunnel_ok(15050), 15051: tunnel_ok(15051)}


def wait_for_tunnel(seconds):
    """Bíður þar til AÐ MINNSTA KOSTI eitt mælaborð svarar. Skilar stöðu hvers ports."""
    deadline = time.time() + seconds
    states = port_states()
    while not any(states.values()) and time.time() < deadline:
        time.sleep(2)
        print(".", end="", flush=True)
        states = port_states()
    return states


SSH_HINTS = [
    ("Permission denied", "ssh-lykillinn er ekki samþykktur á netþjóninum (eða lykillinn finnst ekki)"),
    ("Connection timed out", "netið kemst ekki til netþjónsins (net niðri, VPN eða eldveggur)"),
    ("Could not resolve", "nafnið finnst ekki - athugaðu HostName í ~/.ssh/config"),
    ("REMOTE HOST IDENTIFICATION", "fingrafar netþjónsins breyttist - ssh-keygen -R 165.227.237.206"),
    ("Address already in use", "port á tölvunni er upptekið af öðru ferli"),
    ("cannot listen to port", "port á tölvunni er upptekið af öðru ferli"),
    ("Bad configuration option", "villa í ~/.ssh/config"),
    ("no such identity", "lykilskrá vantar - athugaðu IdentityFile í ~/.ssh/config"),
    ("Host key verification failed", "netþjónninn er ekki í known_hosts - keyrðu einu sinni: ssh freqtrade-lon1"),
]


def tunnel_log_tail(n=6):
    f = ROOT / "tunnel.log"
    if not f.is_file():
        return None
    return f.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]


def explain_tunnel_failure(states):
    say("\n    Gangan opnaðist ekki.")
    say("    Port: " + ", ".join(f"{p} = {'opið' if ok else 'lokað'}" for p, ok in states.items()))
    tail = tunnel_log_tail()
    if tail is None:
        say("    tunnel.log er ekki til: göngu-glugginn (freqtrade-tunnel.bat) ræsti ekki.")
        say("    Prófaðu að keyra hann beint:  .\\freqtrade-tunnel.bat")
    elif not tail:
        say("    tunnel.log er tómt: ssh skrifaði enga villu. Keyrðu:  ssh -N freqtrade-view")
    else:
        say("    Síðustu línur úr tunnel.log (villur frá ssh):")
        for line in tail:
            say("      " + line[:150])
        text = "\n".join(tail)
        for needle, hint in SSH_HINTS:
            if needle.lower() in text.lower():
                say(f"    -> Líkleg ástæða: {hint}")
                break


def fetch_status(tries):
    last_err = ""
    for attempt in range(tries):
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", HOST, "python3", "-"], input=REMOTE_SCRIPT,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            try:
                return json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                last_err = "óvænt svar: " + r.stdout[-200:]
        else:
            last_err = (r.stderr or r.stdout).strip()[-300:]
        if attempt < tries - 1:
            time.sleep(10)
    raise RuntimeError(last_err or "óþekkt villa")


def fmt_trades(name, t):
    if "error" in t:
        return [f"  {name}: villa við lestur gagnagrunns ({t['error']})"]
    lines = [f"  {name}: {t['closed']} lokuð viðskipti, {t['wins']} unnin, samtals {t['pnl']:+.2f} USDT, {len(t['open'])} opin(n)"]
    for pair, since in t["open"]:
        lines.append(f"      opin staða: {pair} (síðan {since})")
    for pair, opened, closed, pnl, reason in t["last"]:
        pnl_s = f"{pnl:+7.2f} USDT" if pnl is not None else "   (opin)    "
        lines.append(f"      síðast: {pair:10} {opened} -> {closed or '-':16} {pnl_s}  {reason}")
    return lines


def report(s):
    problems = []
    say("\n=== STAÐA VERKEFNISINS Á NETÞJÓNINUM ===")
    say(f"Netþjónn: {s['uptime']} | minni {s['mem']} MB | swap {s['swap']} MB | diskur {s['disk']} | afrit: {s['backups']} skrár")
    running = {n: st for n, st in s["containers"]}
    say("\nGámar:")
    for name in EXPECTED:
        st = running.get(name)
        ok = bool(st and st.startswith("Up"))
        say(f"  [{'OK' if ok else '!!'}] {name:28} {st or 'KEYRIR EKKI'}")
        if not ok:
            problems.append(f"gámurinn {name} keyrir ekki")
    say("\nBotar (hjartsláttur síðustu 3 mín - á að vera 2 eða 3):")
    for label, name in (("Aðalbotn", "freqtrade"), ("Paper-botn", "freqtrade-paper-1")):
        n = s["heartbeats"][name]
        say(f"  [{'OK' if n >= 2 else '!!'}] {label:11} {n}")
        if n < 2:
            problems.append(f"{label} sendir ekki hjartslátt - hann gæti verið hangandi")
    say("\nViðskipti:")
    for line in fmt_trades("Aðalbotn", s["main"]) + fmt_trades("Paper-botn", s["paper"]):
        say(line)
    if "candles" in s:
        say(f"\nPaper-botn hefur vistað {s['candles']} kerti. Síðasta kerti: {s['last_candle']}")
        try:
            age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(str(s["last_candle"]))
            if age > datetime.timedelta(minutes=20):
                problems.append(f"síðasta kerti er {int(age.total_seconds() // 60)} mínútna gamalt")
        except (ValueError, TypeError):
            pass
    say()
    if problems:
        say("ATHUGA:")
        for p in problems:
            say(f"  !! {p}")
        say("  Nánari stöðu færðu með: ssh freqtrade-lon1 \"bash ~/freqtrade/status.sh\"")
    else:
        say("ALLT Í LAGI: gámar keyra, botnarnir senda hjartslátt og gögnum er safnað.")
    return problems


def ssh_g(alias):
    """Les virku ssh-stillinguna fyrir alias (ssh -G). Skilar (fyrsta gildi hvers lykils, localforward-port, rc)."""
    r = subprocess.run(["ssh", "-G", alias], capture_output=True, text=True, encoding="utf-8", errors="replace")
    vals, ports = {}, set()
    for line in r.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        key = parts[0].lower()
        if key == "localforward":
            try:
                ports.add(int(parts[1].split()[0]))
            except ValueError:
                pass
        else:
            vals.setdefault(key, parts[1].strip())
    return vals, ports, r.returncode


WANTED_PORTS = {18081, 18082, 15050, 15051}


def ssh_config_path():
    return pathlib.Path.home() / ".ssh" / "config"


def fix_ssh_config():
    """Endurskrifar Host freqtrade-view í ~/.ssh/config: eitt hreint blokk með réttum portum.
    Tekur afrit (config.bak) og snertir ekkert annað. Notar sömu tengingu og freqtrade-lon1."""
    lon1, _, rc = ssh_g(HOST)
    if rc != 0 or lon1.get("hostname", HOST) == HOST:
        raise SystemExit(f"Finn ekki stillinguna fyrir {HOST} í ~/.ssh/config. Hún þarf að vera til (HostName, User, IdentityFile).")
    key = lon1.get("identityfile", "").replace("\\", "/")
    cfg = ssh_config_path()
    text = cfg.read_text(encoding="utf-8-sig") if cfg.is_file() else ""
    if cfg.is_file():
        shutil.copy2(cfg, cfg.with_name("config.bak"))
    blocks = re.split(r"(?im)^(?=[ \t]*Host[ \t])", text)
    kept = [b for b in blocks if not re.match(r"(?i)\s*Host\s+freqtrade-view\s*$", (b.strip().splitlines() or [""])[0])]
    removed = len(blocks) - len(kept)
    new_block = (
        "Host freqtrade-view\n"
        f"    HostName {lon1['hostname']}\n"
        f"    User {lon1['user']}\n"
        + (f"    IdentityFile {key}\n" if key else "")
        + "    LocalForward 18081 localhost:8081\n"
        "    LocalForward 18082 localhost:8082\n"
        "    LocalForward 15050 localhost:5050\n"
        "    LocalForward 15051 localhost:5051\n"
        "    ServerAliveInterval 30\n"
    )
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("".join(kept).rstrip() + "\n\n" + new_block, encoding="utf-8")
    say(f"    Lagaði {cfg}: fjarlægði {removed} gamla freqtrade-view blokk(ir) og skrifaði eina nýja (afrit: config.bak)")


def ensure_ssh_config():
    vals, ports, rc = ssh_g("freqtrade-view")
    if rc != 0 or vals.get("hostname", "freqtrade-view") == "freqtrade-view":
        say("    ssh-stillingin freqtrade-view vantar - bý hana til")
        fix_ssh_config()
    elif ports != WANTED_PORTS:
        say(f"    ssh-stillingin freqtrade-view opnar {sorted(ports)} en á að opna {sorted(WANTED_PORTS)}")
        fix_ssh_config()


def startup_file():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SystemExit("APPDATA fannst ekki - er þetta Windows?")
    return pathlib.Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def install_autostart():
    d = startup_file()
    bat = ROOT / "Freqtrade-OPNA.bat"
    if not bat.is_file():
        raise SystemExit(f"Vantar {bat}")
    if not str(bat).isascii():
        raise SystemExit(f"Slóðin {bat} inniheldur sérstafi. Færðu möppuna á slóð með ASCII-stöfum.")
    d.mkdir(parents=True, exist_ok=True)
    target = d / "freqtrade-opna.bat"
    target.write_text(f'@echo off\nstart "" /min "{bat}" --auto\n', encoding="ascii")
    say(f"Sjálfvirk ræsing sett upp: {target}")
    old = d / "freqtrade-tunnel-start.bat"
    if old.exists():
        old.unlink()
        say(f"Fjarlægði gömlu ræsiskrána {old.name} (annars færðu tvær göngur)")
    say("Næst þegar þú skráir þig inn í Windows opnast gangan, staðan er sótt og vafrinn opnast.")


def uninstall_autostart():
    removed = False
    for name in ("freqtrade-opna.bat", "freqtrade-tunnel-start.bat"):
        f = startup_file() / name
        if f.exists():
            f.unlink()
            removed = True
            say(f"Fjarlægði {f}")
    if not removed:
        say("Engin sjálfvirk ræsing var uppsett.")


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Opna gönguna, sýna stöðu, opna mælaborðin")
    ap.add_argument("--auto", action="store_true", help="fyrir sjálfvirka ræsingu við innskráningu")
    ap.add_argument("--no-browser", action="store_true", help="ekki opna vafra")
    ap.add_argument("--fix-ssh-config", action="store_true", help="laga Host freqtrade-view í ~/.ssh/config")
    ap.add_argument("--install-autostart", action="store_true")
    ap.add_argument("--uninstall-autostart", action="store_true")
    a = ap.parse_args(argv)
    if a.fix_ssh_config:
        return fix_ssh_config() or 0
    if a.install_autostart:
        return install_autostart() or 0
    if a.uninstall_autostart:
        return uninstall_autostart() or 0

    say("1/3 Athuga gönguna til netþjónsins...")
    ensure_ssh_config()
    states = port_states()
    if any(states.values()):
        say("    gangan er þegar opin")
    else:
        say("    gangan er lokuð - opna hana")
        start_tunnel()
        states = wait_for_tunnel(90 if a.auto else 45)
        if not any(states.values()):
            explain_tunnel_failure(states)
            return 1
        say("\n    gangan er opin")
    labels = {15050: "Aðalbotn", 15051: "Paper-botn"}
    for port, ok in states.items():
        if not ok:
            say(f"    ATHUGA: mælaborð {labels[port]} (port {port}) svarar ekki, en hitt virkar. "
                f"Gámurinn gæti verið niðri - sjá stöðuna hér að neðan.")

    say("2/3 Sæki stöðu af netþjóninum...")
    try:
        status = fetch_status(3 if a.auto else 1)
    except RuntimeError as exc:
        say(f"    Gat ekki sótt stöðu: {exc}")
        say("    Gangan virkar, svo mælaborðin gætu samt opnast.")
        status = None
    if status:
        report(status)

    if a.no_browser:
        say("3/3 Vafri ekki opnaður (--no-browser)")
    else:
        say("3/3 Opna mælaborðin í vafranum:")
        for label, url in URLS.items():
            if not states[int(url.rsplit(":", 1)[1])]:
                say(f"    {label}: {url}  (sleppt - svarar ekki)")
                continue
            say(f"    {label}: {url}")
            webbrowser.open_new_tab(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
