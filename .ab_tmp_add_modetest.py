# -*- coding: utf-8 -*-
p = r"tests/test_interface_gaps.py"
s = open(p, encoding="utf-8").read()
anchor = """    def test_stale_distribution_forces_defend(self, monkeypatch):"""
new_test = """    def test_stale_distribution_does_not_unretreat_structural_break(self, monkeypatch):
        \"\"\"跌破MA5×0.99 是硬结构信号：派发日过期也不得把撤退降级成更不保守的防守。\"\"\"
        mma = MarketModeAdaptive.__new__(MarketModeAdaptive)
        monkeypatch.setattr(mma, "_apply_external_shock", lambda mode: mode)
        from src.analyzers.expectation_divergence import DivergenceCounter
        monkeypatch.setattr(
            DivergenceCounter, "load",
            staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("test: skip S3"))),
        )
        # 先升后急跌：末5根跌破 MA5×0.99（结构撤退），且日期全在 2026-04（派发日过期）
        closes = [100.0 + i * 0.2 for i in range(35)]
        closes += [closes[-1] - (k + 1) * 1.8 for k in range(5)]
        kline = _kline_to(
            [f"2026-04-{(d % 28) + 1:02d}" for d in range(40)],
            closes=closes, volumes=[1000] * 40,
        )
        result = mma._assess_market("2026-09-08", kline)
        assert result["mode"] == "retreat"
        assert "已跌破MA5撤退为结构信号" in result["mode_reason"]

    def test_stale_distribution_forces_defend(self, monkeypatch):"""
assert anchor in s
s = s.replace(anchor, new_test, 1)
open(p, "w", encoding="utf-8").write(s)
print("OK")
