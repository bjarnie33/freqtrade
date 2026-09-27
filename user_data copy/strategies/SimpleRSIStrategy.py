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

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Reiknar RSI (Relative Strength Index) - staðall vísir sem mælir
        # hvort eign sé "yfirkeypt" (hátt RSI) eða "yfirseld" (lágt RSI)
        dataframe["rsi"] = ta.RSI(dataframe)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Einföld regla: kaupa þegar RSI fer undir 30 (talið "yfirselt")
        dataframe.loc[
            (dataframe["rsi"] < 30) & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Einföld regla: selja þegar RSI fer yfir 70 (talið "yfirkeypt")
        # - stop-loss/take-profit úr config.json gilda líka samhliða þessu
        dataframe.loc[
            (dataframe["rsi"] > 70) & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe
