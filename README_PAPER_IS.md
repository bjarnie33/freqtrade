# Paper-viðbót fyrir Freqtrade

Viðbótin er tengd Freqtrade með nýrri stefnu og sérstöku Docker Compose-skjali. Upprunalegu skjölunum hefur ekki verið breytt. Enginn bot hefur verið ræstur og engin API-pöntun verið send.

## Uppsetning

Afritaðu innihald þessa pakka inn í rót núverandi `freqtrade` verkefnis. Skrárnar eru nýjar; þær þurfa ekki að skrifa yfir `config.json`, `docker-compose.yml`, `SimpleRSIStrategy.py` eða gagnagrunninn.

Keyrðu síðan úr verkefnisrót:

```bash
docker compose -f docker-compose.paper.yml up -d
docker compose -f docker-compose.paper.yml logs -f
```

Docker er ekki tiltækt í prófunarumhverfinu hér; raunkeyrsla með Freqtrade og markaðstengingu er enn óstaðfest. `stable` myndin er ekki fest við tiltekna útgáfu. Fyrir endurtekningarhæfar prófanir þarf að festa mynd við staðfesta útgáfu/digest eftir fyrstu samþættingarprófun.

Nýi botinn notar opin Binance-markaðsgögn og sýndarfé. Hann slekkur á sandbox-stillingunni vegna þess að dry-run hermir viðskipti sjálfur; hann þarf engin viðskiptalykilgögn. Prófunarpörin eru BTC/USDT og ETH/USDT, ekki Coinbase/USD. Þetta er tæknilegt prófunarval. Gamli botinn getur enn keyrt sjálfstætt og nýi botinn tekur ekki við gömlum opnum stöðum.

Nýjar skrár við keyrslu:

- `user_data/paper_trades.sqlite`: Freqtrade sýndarviðskipti, staða og gjöld.
- `user_data/paper_audit.sqlite`: ákvörðunarskrá og síðustu lokuðu kerti með eiginleikum.
- `user_data/logs/paper.log`: keyrsluskrá.

Nýja API-þjónustan er óvirk. Fyrirliggjandi dashboard tengist því áfram gamla botnum ef hann er keyrandi; dashboard hefur ekki verið endurforritað til að sýna nýja audit-gagnagrunninn.

## Áhættumörk í prófunarkóðanum

Hámark 50 USDT á kaup, 3 opnar stöður og 10 samþykktar kaupabeiðnir á UTC-degi. Kaup stöðvast eftir 50 USDT í samanlögðu innleystu tapi dagsins. Þetta eru sýnidæmismörk, ekki sérsniðin fjárhagsráðgjöf. Hagkvæm viðskipti draga ekki frá tapsmörkunum; hafnaðar/misheppnaðar pantanir eftir samþykki geta talið með í kaupabeiðnafjölda.

Daglegt tapsmark nær ekki til óinnleysts taps eða heildarvirðisfalls eignasafns. Það lokar ekki öllum stöðum. Freqtrade stop-loss/ROI gilda áfram. Takmörkun að nýjum kaupum er því ekki sama og heildaráhættuþak.

Til að banna ný kaup, búðu til tóma skrá `user_data/EMERGENCY_STOP`. Söluheimildir og stop-loss eru áfram virk. Fjarlægðu skrána til að aflétta. Til að stöðva allan nýja botinn:

```bash
docker compose -f docker-compose.paper.yml stop
```

Gagnagrunnarnir eru á tengdri möppu og varðveitast. Ekki eyða möppunni eða gagnagrunnunum. Við stöðvun hættir botinn líka að fylgjast með opnum sýndarstöðum.

## Greining viðskiptasögu

```bash
python scripts/research.py history user_data/tradesv3.sqlite --output trade_history.json
```

Þetta les gamla gagnagrunninn, þar með talið viðhengdan WAL ef hann er til staðar, án þess að breyta honum. Þegar gagnagrunnur er afritaður úr keyrandi kerfi á að nota SQLite backup eða stöðva botinn og varðveita WAL; ekki afrita aðeins `.sqlite` skrána meðan skrif eru í gangi.

Úttakið inniheldur verð, tíma, pör, gjöld, stöðu og hagnað/tap. Það getur ekki endurskapað eldri features, order-book eða markaðsaðstæður sem voru aldrei vistaðar.

## Þjálfun og prófun

```bash
python -m pip install -r requirements-research.txt
python scripts/research.py train BTC_USDT_5m.csv --output user_data/research_models
```

CSV verður að innihalda `date,open,high,low,close,volume`, eitt par, samfelld lokuð 5 mínútna kerti í UTC og hækkandi tímaröð. Ekki nota 1.067 viðskiptafærslur sem staðgengil fyrir kertagögn; aðferðin þjálfar á markaðskertum. Ekkert kertagagnasafn var með til að framkvæma þessa þjálfun á raunverulegum sögugögnum.

Logistic regression spáir líkum á meira en 0,5% lokaverðshækkun yfir næstu 12 kerti. Þetta er rannsóknarmarkmið, ekki spá um nettóhagnað. Eldri 80% gagnanna mynda þróunarsafn, með 12-kerta bil fyrir síðari 20% prófunarsafn. Þrjú `TimeSeriesSplit` próf hafa sama bil innan þróunarsafns. Skölun lærist aðeins af þjálfunargögnum. Lokasafnið er ekki notað til þjálfunar; endurtekið val út frá lokasafninu getur samt mengað matið.

Hermunin notar merki eftir lok kertis og fyllingu á opnun næsta kertis. Sjálfgefin forsendugildi: 0,1% gjald á hvora hlið, 10 bps heildarspread, 10 bps slippage á hvora hlið, 50 USDT staða, 1.700 USDT upphafsfé og ein löng staða. Lokavirði sýnir áætlað söluvirði opinnar stöðu eftir kostnað. Hún hermir ekki order-book dýpt, hlutafyllingar, bið eftir limit-pöntun eða Freqtrade stop-loss/ROI. Hún er því sérstakt rannsóknarpróf, ekki nákvæm eftirlíking af paper-botnum.

Hver þjálfun vistar nýja útgáfu með líkani, gagnahashi, tímamörkum og niðurstöðum. Nákvæmlega sama keyrsla fær sama auðkenni og er ekki skrifuð yfir. Líkön eru ekki endurþjálfuð sjálfkrafa; keyrðu skipunina aftur með nýju safni. Samanburður krefst sömu gagna, próftímabils og kostnaðarforsendna:

```bash
python scripts/research.py compare path/to/report1.json path/to/report2.json
```

## Tengja rannsóknarlíkan við paper-botinn

Sjálfgefið notar nýja stefnan RSI < 50 fyrir kaup og RSI > 50 fyrir sölu. RSI hér notar pandas EWM með Wilder-stuðli; upphafsstilling er ekki nákvæmlega sú sama og TA-Lib í gömlu stefnunni. Ekki gera ráð fyrir nákvæmlega sömu merkjum.

Til að nota nýþjálfað líkan skaltu bæta `paper_model_path` við `user_data/config.paper.json`, með gámaslóðinni að nýju skránni:

```json
"paper_model_path": "/freqtrade/user_data/research_models/UTGAFA/model.joblib"
```

Gámurinn þarf þá líka `scikit-learn` og `joblib`. Notaðu Dockerfile að neðan, byggðu sér mynd og settu `image: local/freqtrade-paper-research` í nýja Compose-skjalið:

```dockerfile
FROM freqtradeorg/freqtrade:stable
COPY requirements-research.txt /tmp/requirements-research.txt
RUN pip install --no-cache-dir -r /tmp/requirements-research.txt
```

```bash
docker build -f Dockerfile.paper -t local/freqtrade-paper-research .
```

Líkanið tekur við kaupamerkjunum (>0,6) og sölumerkjunum (<0,4), en stop-loss og ROI eru áfram virk. Það spáir ekki á eigin þjálfunar/próftímabili. Notaðu aðeins `joblib` skrár sem þú býrð sjálfur til; sniðið getur keyrt kóða við innlestur. Engin sjálfvirk skipting yfir í nýtt líkan er útfærð.

Audit skráir síðasta lokaða kertið, eiginleika, útgáfu líkans, líkindaspá, merki og kaup/sölubeiðnir. Kertiskrá er ekki sögulegur order-book-straumur. Núverandi gögn duga því ekki til að endurgera alla verðmyndun Freqtrade-pantana; boðverð og magn eru vistuð í ákvörðunarskránni.

## Prófanir

```bash
python -m unittest discover -s tests -p test_paper.py -v
```

9 próf stóðust: orsakasamhengi features, næsta-kertis fylling og kostnaður, tímabil/bil þjálfunar, bann við live, stærðarmörk, daglegur samþykkisfjöldi eftir endurræsingu, tap/balance/staðafjöldi, höfnun við skráningarvillu, útilokun ólokinna kerta og paper-sala meðan neyðarstöðvun er virk. Þessi nýju próf nota lítil staðgengilsviðmót fyrir Freqtrade; þau staðfesta ekki keyrslu með raunverulegu exchange-viðmóti.

Áður en niðurstöður eru metnar þarf samþættingarprófun í Docker, söguleg gögn, Freqtrade `lookahead-analysis` og `recursive-analysis`. Engin niðurstaða um arðsemi hefur verið staðfest.
