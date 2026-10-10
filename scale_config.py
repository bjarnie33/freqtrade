# scale_config.py - stillir paper-botninn: fjoldi mynta, opnar stodur, stake og siur.
# Notkun (i freqtrade-moppunni):
#   python scale_config.py [myntir] [stodur] [stake] [spread] [verdsia] [aldur]
#   python scale_config.py 100 50 30 0.005 0 30
#     myntir  = hversu margar staerstu myntir (eftir veltu) eru skodadar      (sjalfgefid 60)
#     stodur  = mest opnar stodur                                              (50)
#     stake   = USDT a hverja stodu                                            (30)
#     spread  = mesti munur a kaup- og soluverdi, 0.005 = 0.5%                 (0.005)
#     verdsia = low_price_ratio fyrir PriceFilter, 0 = engin verdsia           (0.0001)
#     aldur   = minnsti aldur mynta i dogum                                    (30)
# Breytir bara user_data/config.paper.json og tekur afrit (config.paper.json.bak). Oruggt ad keyra tvisvar.
import json, pathlib, shutil, sys
a = sys.argv[1:]
coins = int(a[0]) if len(a) > 0 else 60            # hversu margar staerstu myntir (eftir veltu) eru skodadar
slots = int(a[1]) if len(a) > 1 else 50            # mest opnar stodur
stake = float(a[2]) if len(a) > 2 else 30.0        # USDT a hverja stodu
spread = float(a[3]) if len(a) > 3 else 0.005      # mesti munur a kaup- og soluverdi (0.01 = 1%)
price_ratio = float(a[4]) if len(a) > 4 else 0.0001  # 0 = engin verdsia (PriceFilter sleppt)
age = int(a[5]) if len(a) > 5 else 30              # minnsti aldur mynta i dogum
p = pathlib.Path("user_data/config.paper.json")
d = json.loads(p.read_text(encoding="utf-8-sig"))
tradable = float(d.get("dry_run_wallet", 1000)) * float(d.get("tradable_balance_ratio", 0.99))
if slots * stake > tradable:
    sys.exit("Stoppa: %d stodur x %.0f USDT = %.0f USDT, en veskid rumar adeins %.0f USDT (hamark: stake <= %.1f fyrir %d stodur)."
             % (slots, stake, slots * stake, tradable, tradable / slots, slots))
old_text = p.read_text(encoding="utf-8-sig")
d["max_open_trades"] = slots
d["stake_amount"] = int(stake) if stake == int(stake) else stake
pl = [{"method": "VolumePairList", "number_assets": coins, "sort_key": "quoteVolume", "refresh_period": 1800},
      {"method": "AgeFilter", "min_days_listed": age},
      {"method": "SpreadFilter", "max_spread_ratio": spread}]
if price_ratio > 0:
    pl.append({"method": "PriceFilter", "low_price_ratio": price_ratio})
d["pairlists"] = pl
d.setdefault("exchange", {})
d["exchange"]["pair_whitelist"] = [".*/USDT"]
d["exchange"]["pair_blacklist"] = [
    "BNB/.*",
    ".*(_PREMIUM|BEAR|BULL|DOWN|HEDGE|LONG|SHORT|UP|[0-9]+L|[0-9]+S)/.*",
    "(AUD|BRZ|CAD|CHF|EUR|GBP|HKD|IDRT|JPY|NGN|RUB|SGD|TRY|UAH|USD|ZAR|AEUR|EURI)/.*",
    "(.*USD|USD[A-Z0-9]*|BUSD|CUSDT|DAI|PAX|PAXG|SUSD|UST|VAI)/.*",
]
new_text = json.dumps(d, indent=2)
if new_text.strip() == old_text.strip():
    print("Ekkert breytt - stillingin er thegar svona.")
    sys.exit(0)
shutil.copy2(p, p.with_name("config.paper.json.bak"))
p.write_text(new_text, encoding="utf-8")
print("OK: %d staerstu myntir, aldur >= %d d, spread <= %.2f%%, verdsia %s; %d opnar stodur x %s USDT = %.0f USDT (veski %.0f)."
      % (coins, age, spread * 100, ("%.4f" % price_ratio) if price_ratio > 0 else "slokkt", slots, d["stake_amount"], slots * stake, tradable))
