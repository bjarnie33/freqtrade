"""
Einföld dæmastefna fyrir Freqtrade - EINGÖNGU til samanburðar við okkar
eigin stop-loss/take-profit botn.

ATHUGIÐ: Þetta er byrjendadæmi (RSI-vísir), EKKI ráðlegging um að þessi
tiltekna stefna sé "góð" eða líkleg til að skila hagnaði. Sama regla
gildir og alltaf: engin stefna er tryggð til að virka, og fortíðar-
frammistaða (jafnvel í backtesting) segir ekki fyrir um framtíð.

Settu þetta í: user_data/strategies/SimpleRSIStrategy.py
"""

import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


class SimpleRSIStrategy(IStrategy):
    # Þessi tvö gildi eru líka sett í config.json - Freqtrade notar hærra
    # forgangsstig frá strategy-skjalinu sjálfu ef bæði eru til staðar
    minimal_roi = {"0": 0.10}   # sama og TAKE_PROFIT_PCT í okkar botni
    stoploss = -0.05            # sama og STOP_LOSS_PCT í okkar botni

    timeframe = "5m"

    # Sjálfvirkar neyðarhemlar - þurfa að vera hér (ekki í config.json,
    # sem er orðið úrelt (deprecated) frá og með þessari Freqtrade-útgáfu).
    # StoplossGuard: 3+ stop-loss á 24 kertum (2 klst) stöðvar ný kaup í 12 kerti.
    # MaxDrawdown: 10%+ fall á 48 kertum (4 klst) stöðvar ný kaup í 24 kerti.
    protections = [
        {
            "method": "StoplossGuard",
            "lookback_period_candles": 24,
            "trade_limit": 3,
            "stop_duration_candles": 12,
            "only_per_pair": False,
        },
        {
            "method": "MaxDrawdown",
            "lookback_period_candles": 48,
            "trade_limit": 10,
            "stop_duration_candles": 24,
            "max_allowed_drawdown": 0.1,
        },
    ]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Reiknar RSI (Relative Strength Index) - staðall vísir sem mælir
        # hvort eign sé "yfirkeypt" (hátt RSI) eða "yfirseld" (lágt RSI)
        dataframe["rsi"] = ta.RSI(dataframe)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Til baka í strangari mörk (30) eftir að gögn sýndu að 50 veldur
        # sífelldu "whipsaw" tapi - sjá úttekt: 0 af 1.026 viðskiptum náðu
        # take-profit með 50/50 mörkunum.
        cond = (dataframe["rsi"] < 30) & (dataframe["volume"] > 0)
        dataframe.loc[cond, "enter_long"] = 1
        # Skráir RSI-gildið sjálft á hverja færslu (sýnilegt í /api/v1/trades
        # og tradesv3.sqlite sem enter_tag) - svarar "hvaða merki hafði
        # kerfið aðgang að þegar ákvörðunin var tekin".
        dataframe.loc[cond, "enter_tag"] = "rsi_" + dataframe.loc[cond, "rsi"].round(1).astype(str)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Sama hér - til baka í 70
        dataframe.loc[
            (dataframe["rsi"] > 70) & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe