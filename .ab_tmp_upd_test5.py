# -*- coding: utf-8 -*-
import io

P = r"C:\Users\15831\Downloads\stock-agent2\tests\test_data_source_fixes.py"
s = io.open(P, encoding="utf-8").read()

old_method = '''    def test_fresh_report_period_votes_with_period_label(self, monkeypatch):
        """新鲜报告期（30 天前）→ 正常投票，明细带「报告期」口径前缀。"""
        as_of = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        _install_fake_akshare(monkeypatch, gdhs_frame=self._gdhs_frame(
            as_of, latest=39000, prev=41000,  # -4.9% → 筹码集中看多
        ))
        result = _inst._fetch_shareholder_count("688008")
        assert result["vote"] == 1
        assert result["detail"].startswith("报告期")
        assert result["raw"].get("as_of")
        assert "stale" not in result["raw"]
'''

new_method = '''    def _recent_standard_period(self, max_days=60) -> str:
        """返回 90 天内最近的 A 股标准报告期（0331/0630/0930/1231）日期串 YYYY-MM-DD。"""
        today = datetime.now()
        for mmdd in ("0930", "0630", "0331", "1231"):
            for year in (today.year, today.year - 1):
                try:
                    dt = datetime.strptime(f"{year}{mmdd}", "%Y%m%d")
                except ValueError:
                    continue
                if 0 <= (today - dt).days <= max_days:
                    return dt.strftime("%Y-%m-%d")
        return (today - timedelta(days=30)).strftime("%Y-%m-%d")

    def test_fresh_report_period_votes_with_period_label(self, monkeypatch):
        """新鲜标准报告期（≤60 天，如三季报 0930）→ 正常投票，明细带「报告期」口径前缀。"""
        as_of = self._recent_standard_period()
        _install_fake_akshare(monkeypatch, gdhs_frame=self._gdhs_frame(
            as_of, latest=39000, prev=41000,  # -4.9% → 筹码集中看多
        ))
        result = _inst._fetch_shareholder_count("688008")
        assert result["vote"] == 1
        assert result["detail"].startswith("报告期")
        assert "非标准报告期" not in result["detail"]
        assert result["raw"].get("as_of")
        assert result["raw"].get("report_period_standard") is True
        assert "stale" not in result["raw"]

    def test_non_standard_period_annotated(self, monkeypatch):
        """统计截止日 20260820（非 A 股标准报告期）→ 标注「非标准报告期」，
        不虚构报告期语义（60731/60820/60910 是披露/抓取日，不是一季/半年/三季/年报）。"""
        as_of = "2026-08-20"
        _install_fake_akshare(monkeypatch, gdhs_frame=self._gdhs_frame(
            as_of, latest=39000, prev=41000,  # -4.9% → 筹码集中看多
        ))
        result = _inst._fetch_shareholder_count("688195")
        assert result["vote"] == 1
        assert result["detail"].startswith("统计截止日")
        assert "非标准报告期" in result["detail"]
        assert "报告期" not in result["detail"]
        assert result["raw"].get("report_period_standard") is False
        assert result["raw"].get("as_of") == "20260820"
        assert "stale" not in result["raw"]
'''

assert old_method in s, "old method not found"
s = s.replace(old_method, new_method, 1)
io.open(P, "w", encoding="utf-8").write(s)
print("test patched OK")
