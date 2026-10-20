class _Adapter:
    def __init__(self):
        self.symbols = []

    def get_index_data(self, symbol):
        self.symbols.append(symbol)

        class Result:
            success = True
            data = [
                {"date": "2026-10-18", "close": 3700.0, "volume": 100},
                {"date": "2026-10-19", "close": 3720.0, "volume": 110},
            ]

        return Result()


def test_index_mode_fetches_shanghai_composite(monkeypatch):
    from src.loop.market_mode_adaptive import MarketModeAdaptive

    adapter = _Adapter()
    monkeypatch.setattr(
        "src.data_layer.akshare_adapter.get_akshare_adapter",
        lambda: adapter,
    )
    engine = MarketModeAdaptive()
    engine._fetch_index_kline()

    assert adapter.symbols == ["sh000001"]
