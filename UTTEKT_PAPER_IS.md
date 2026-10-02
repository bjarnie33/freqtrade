# Tæknileg úttekt á meðfylgjandi Freqtrade-verkefni

Dagsetning: 1. október 2026. Þetta er úttekt á afhenta ZIP-afritinu og viðhengdum SQLite/WAL-gögnum. Hún staðfestir ekki hvaða útgáfa keyrir á tölvu notandans eða inni í núverandi Docker-gámi.

## Helstu staðreyndir

`docker-compose.yml` velur `SimpleRSIStrategy` og `user_data/config.json`. Sú stilling er `dry_run: true`, Binance og USDT. Hún er ekki Coinbase/USD-uppsetning. `exchange.sandbox` er ekki sjálfstætt öryggisbann við raunviðskiptum; dry-run er aðalgreiningin á hermun og raunviðskiptum.

`user_data/tradesv3.sqlite`, lesinn með WAL, inniheldur 1.067 viðskipti: 1.026 lokuð og 41 opin. Allar færslurnar bera heitið `SimpleRSIStrategy`. Opnunartímar eru frá 26. september 2026 kl. 12:51 UTC til 1. október 2026 kl. 17:00 UTC. Summa vistaðs `close_profit_abs` fyrir lokuð viðskipti er −302,5794233 USDT. Þetta er skráð niðurstaða gagnagrunnsins, ekki endurreiknuð staðfesting á öllum gjöldum eða heildarvirði opnu staðanna. Hann inniheldur 2.581 pöntunarfærslu og enga færslu í `trade_custom_data`.

`user_data copy/tradesv3.sqlite` er tómur. Afritsstefnan undir `user_data copy` notar 30/70 RSI-mörk; virka stefnan undir `user_data` notar 50/50. Að rugla þessum möppum saman myndi gefa ranga lýsingu á botnum.

## Niðurstöðutafla — upprunalega kerfið

| Atriði | Staða | Hvar í kóða | Vandamál | Tillaga |
|---|---|---|---|---|
| 1. Fyrri viðskipti | ⚠️ | `freqtrade/persistence/trade_model.py`: `LocalTrade`, `Trade`, `to_json`; `user_data/tradesv3.sqlite`: `trades`, `orders`; `Freqtrade_dashboard.py` | Verð, tímar, pör, stærð, gjöld og afkoma eru vistuð. Engin sönnun um vistuð ákvörðunarfeatures eða sögulegt order-book. | Útflutningur viðskiptasögu og sérstök audit-skrá fyrir hverja nýja ákvörðun. |
| 2. Features | ⚠️ | `user_data/strategies/SimpleRSIStrategy.py`: `populate_indicators`, entry/exit; `sample_strategy.py`: `populate_indicators` | Virka stefnan reiknar aðeins TA-Lib RSI; vísar í sample strategy eru ekki sjálfkrafa notaðir. Upphitun/tækniúttekt á stöðugleika vantar. | Sameiginleg, prófuð feature-vinnsla og lokuð-kerti regla, með vistun á þeim features sem botinn notar. |
| 3. Training/Test | ❌ | Virka stefnan kallar ekki `self.freqai.start`; `user_data/freqaimodels` hefur aðeins `.gitkeep` | Innbyggðir FreqAI-kóðar og dæmi staðfesta ekki að þetta forrit þjálfi eða noti líkan. Engar staðfestar model versions eða out-of-sample niðurstöður í þessu keyrsluflæði. | Sjálfstæð tímaröð með bilum fyrir labels, þjálfunarskölun, útgáfuvistun og aðskildri prófun. |
| 4. Paper trading | ⚠️ | `freqtrade/exchange/exchange.py`: `create_dry_run_order`, `get_dry_market_fill_price`, `add_dry_order_fee`; `freqtrade/wallets.py`; config/Compose | Freqtrade hefur sýndarpantanir, wallet, gjöld og order-book verðskrið. Hermun er samt ekki raunveruleg röð í kauphöll, pöntunarbið eða fullkomið líkan hlutafyllinga. | Halda dry-run, greina raunhæfni forsendna og bera saman við sérstaka kostnaðarhermun. |
| 5. Risk management | ⚠️ | config: `max_open_trades`, `stake_amount`, stop-loss/ROI; strategy; `freqtrade/freqtradebot.py`: pöntunarflæði | 116 opnar stöður og `stake_amount: unlimited` gefa ekki fast fjárhæðarþak. Engin sértæk dagleg tapstoppun eða full ákvörðunarskrá í virku stefnunni. Live-pöntunarkóði er til í Freqtrade. | Sér paper-stefna með fast þak, dagleg mörk, neyðarflaggi, audit og harðri höfnun live-stillingar. |

## A. Það sem botinn gerir rétt núna

- Hann er stilltur á dry-run í afhenta config og heldur viðskiptasögu í SQLite.
- Viðskiptafærslur hafa kaupverð (`open_rate`), söluverð (`close_rate`), opnunar/lokunartíma, par, magn, fjárhæð, gjaldahlutföll/gjaldakostnað og skráðan hagnað/tap. Opnar færslur geta eðlilega haft tóma sölureiti.
- TA-Lib RSI byggir á OHLCV-kertagögnum frá Freqtrade. Kaupreglan er RSI < 50 og volume > 0; sölureglan RSI > 50 og volume > 0.
- Stop-loss er −5% og ROI-reglan 10% frá upphafi, með fyrirvara um fyllingar og markaðshreyfingar.
- Freqtrade wallet-flæðið gerir greinarmun á lausu og bundnu fé og birtir stöður. Þetta eru innbyggðar takmarkanir, ekki sönnun um sérstakt sérsmíðað risk-lag.
- Dry-run markaðspantanir geta reiknað áætlað fyllingarverð úr order-book og bæta við taker-gjaldi. Kóðinn hefur 5% mörk fyrir verstu fyllingarverðshlið í þessu flæði; það þýðir ekki að öll viðskipti beri 5% verðskrið.

## B. Það sem er að hluta til rétt

- Viðskiptasaga er til, en ekki full endurgerð á upplýsingum við ákvörðun. `trade_custom_data` er tóm.
- Vísar í `sample_strategy.py` eru raunverulegir útreikningar, en Compose velur aðra stefnu. ADX, stochastic-fast, MACD, MFI, Bollinger, SAR, TEMA og Hilbert/sine í dæminu eru því ekki sönnun um features í virkum bot.
- FreqAI hefur innbyggð split/timerange/model-vistunarflæði. `freqtrade/freqai/data_kitchen.py` setur `shuffle=False` ef það er ekki tilgreint, en leyfir stillingar sem þarf að sannreyna fyrir hvert raunverulegt líkan. Þetta flæði er ekki virkt í skoðaðri RSI-stefnu.
- Paper trading er framkvæmanlegt í framework-kóðanum, en ytri markaðsþjónusta, núverandi Docker-image, tenging og raunveruleg keyrsla voru ekki prófuð hér.
- Fjöldaþak og stop-loss eru til. Dagleg heildaráhætta, fast fjárhæðarþak og skráning allra eigin ákvörðunargagna eru ekki sýnd í upprunalegri stefnu.

## C. Það sem vantar

Virk AI-þjálfun/spá, staðfest model-samanburður, endurþjálfunarstýring, skráð markaðsfeatures við eldri viðskipti, sögulegt order-book, óinnleyst daglegt drawdown-þak og sértæk stöðvun nýrra kaupa við daglegt tap. Ekki fannst sértækt Coinbase Advanced Trade-kaupaflæði í þessari uppsetningu. Almenn Freqtrade exchange-viðmót eru ekki staðfest Coinbase/USD-samþætting.

Nýja viðbótin útvegar þjálfunarskipun og líkanstengingu en ekki sjálfvirkt retraining, PostgreSQL-markaðssafn, sjálfvirkan flutning á Linux eða söfnun allra USD-para. Þau stærri verkefni þurfa áfram sérstaka útfærslu.

## D. Data leakage og look-ahead

Í virku RSI-stefnunni fannst ekki neikvætt shift, miðjaður gluggi eða skölun yfir framtíðargögn. TA-Lib RSI notar núverandi og eldri kertagildi. Það er þó ekki sama og keyrð `lookahead-analysis` úttekt; slík keyrsla krefst uppsetts Freqtrade og kertagagna.

`ta.RSI(dataframe)` notar sjálfgefið 14 tímabila RSI, eða um 70 mínútur á 5 mínútna kertum. RSI er endurkvæmur útreikningur með upphitun og eldri upplýsingum. Virka stefnan skilgreinir ekki sérstakt `startup_candle_count`; stöðugleiki upphafsvísa þarf prófun. Frame-tími er opnunartími kertis, svo ákvörðun má fyrst nota lokagildi eftir lok þess.

Fyrri viðskiptafærslur án vistaðra features duga ekki til að sanna hvað botinn vissi. Að sækja núverandi order-book eða nota seinni markaðsstöðu við sögulegt mat myndi skapa villandi endurgerð.

Nýja rannsóknarlagið notar framtíðargögn aðeins í markbreytu þjálfunar; þau eru ekki í features. 12-kerta bil skilur þjálfun/validation/test til að útiloka skörun á markbreytutímabilum. Eiginleikar og skölun eru prófuð sérstaklega. Endurkvæmt RSI/EMA getur samt verið háð upphitunarlengd, og endurtekið val út frá lokaprófi skapar matsleka.

### Eiginleikar nýju viðbótarinnar

Allir koma úr sömu 5 mínútna OHLCV-gögnum og eru reiknaðir við/eftir lok kertis; enginn notar order-book eða söguleg viðskipti sem input.

| Feature | Útreikningur | Tímabil | Framtíðargögn |
|---|---|---|---|
| `return_1` | Lokaverð / fyrra lokaverð − 1 | 1 kertis bil | Nei |
| `return_12` | Lokaverð / lokaverð 12 kertum fyrr − 1 | 60 mínútur | Nei |
| `volume_ratio` | Núverandi volume / meðaltal síðustu 12 volume-gilda | 12 kerti, með núverandi lokuðu kerti | Nei |
| `volatility_12` | Staðalfrávik síðustu 12 eins-kertis breytinga | 12 breytingar | Nei |
| `sma_ratio` | Lokaverð / 24-kerta meðaltal − 1 | 120 mínútur | Nei |
| `rsi_14` | EWM jákvæðra/neikvæðra breytinga, alpha 1/14, RSI-formúla | Endurkvæmt, min. 14 breytingar | Nei |
| `macd_ratio` | (EMA12 − EMA26) / lokaverð | Endurkvæmt EMA | Nei |

Þetta MACD-feature er línan sjálf normaliseruð með verði, ekki signal-line eða histogram. RSI notar aðra upphafsstillingu en TA-Lib; merki þurfa ekki að vera nákvæmlega þau sömu og hjá gamla botnum.

## E. Öryggisatriði áður en live kemur til greina

Config-afritið inniheldur innskráð aðgangsorð og JWT-leyndarmál fyrir API-þjónustuna. Hér eru þau ekki endurbirt. Þessi gildi ætti að endurnýja áður en þjónustan er opnuð eða afritinu deilt víðar. Compose bindur port á localhost sem dregur úr ytri aðgangi; það eyðir ekki þessum viðkvæmu gildum úr skránni.

Freqtrade hefur raunverulegt pöntunarflæði: í `exchange.py:create_order` velur dry-run sýndarpöntun, en annars fer kall til exchange API. Núverandi config velur hermun, en breyting á stillingu gæti virkjað annað flæði. Nýja stefnan hafnar `dry_run != true` í constructor og samþykkir bara `runmode == dry_run` í pöntunarköllum. Hún getur þó ekki gert aðrar stefnur, aðrar keyrslur eða handvirka config-breytingu utan hennar öruggar.

Ekki treysta á strategy callback sem kastar villu til að hafna kaupum: í `freqtradebot.py` er `confirm_trade_entry` vafið með `default_retval=True`. Nýja stefnan grípur skráningar/risk-villur sjálf og skilar False. Slökkt er á force-entry og API í viðbótarconfig.

Daglegt tap í viðbótinni er samanlagt innleyst tap, ekki óinnleyst heildartap. Neyðarflaggið bannar ný kaup en neyðir ekki tafarlausa lokun. Þetta eru efnisleg takmörk sem þarf að skilja áður en kerfið er metið.

## F. Forgangsraðað áframhald

1. Varðveita gamla gagnagrunn/WAL og endurnýja API-leyndarmál. Halda gamla botnum aðskildum frá nýju prófuninni.
2. Samþættingarprófa nýju Compose-uppsetninguna og paper-stefnuna með lykillausum markaðsgögnum; staðfesta að engin raunpöntun fari út og að endurræsing varðveiti audit/stöðu.
3. Safna lokuðum kertum og ákvörðunargögnum. Bæta sérstaklega við tímastimplaðri order-book-söfnun ef full endurgerð pantanafyllinga er markmiðið.
4. Keyra þjálfun á raunverulegum, staðfestum kertagögnum og halda lokaprófinu óbreyttu. Greina gömlu viðskiptin sérstaklega, án þess að búa til ósönnuð söguleg features.
5. Keyra Freqtrade lookahead/recursive analysis og backtesting, með raunhæfum gjaldaforsendum og prófun mismunandi tímabila.
6. Bera RSI og líkan saman í áframhaldandi paper-keyrslu með sömu pörum/fé/kostnaði. Bæta við daglegu equity/drawdown-þaki, betri framkvæmdarhermun og sjálfvirkri retraining-stýringu ef gögn og niðurstöður réttlæta það.

## Afhent útfærsla og sannprófun

Viðbótarkóðinn er í `user_data/strategies/AuditedPaperStrategy.py` og `paper_research.py`; tengingin er í `docker-compose.paper.yml` og `config.paper.json`. CLI er í `scripts/research.py`. Uppsetning og takmarkanir eru í `README_IS.md`.

9 ný próf stóðust og Python-skrárnar voru þýddar. Þjálfun/prófun var keyrð á tilbúnum prófgögnum, ekki raunverulegri markaðssögu. Freqtrade-viðmót voru hermd í callback-prófunum þar sem TA-Lib, ccxt og Docker vantar í þessu umhverfi. Ekki hefur því verið staðfest að heildarkerfið ræsist í Docker eða skili hagnaði. Upprunalega uppsetningin og gömlu gagnagrunnarnir hafa ekki verið skrifaðir yfir.

Opinber viðmiðun fyrir callbacks: https://www.freqtrade.io/en/stable/strategy-callbacks/ . Afhenta frumkóðanum var fylgt fyrir nákvæma hegðun þessarar útgáfu.
