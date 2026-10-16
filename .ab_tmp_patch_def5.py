# -*- coding: utf-8 -*-
import io, re

TARGET = r"C:\Users\15831\Downloads\stock-agent2\src\analyzers\institutional_scorer.py"
s = io.open(TARGET, encoding="utf-8").read()

# 1) add helper after _SHAREHOLDER_STALE_DAYS
anchor = "_SHAREHOLDER_STALE_DAYS = 90\n"
assert anchor in s, "stale days anchor missing"
helper = anchor + '''
# 【C-股东户数】A股标准报告期：一季报 0331 / 半年报 0630 / 三季报 0930 / 年报 1231。
# 东方财富"股东户数统计截止日"常为非标准日期（如 0715/0820/0910），那是披露截止日，
# 不是财务报告期——标注区分，避免把披露/抓取日误写成报告期（口径杂音）。
_STANDARD_REPORT_MMDD = frozenset({"0331", "0630", "0930", "1231"})


def _is_standard_report_date(as_of_str: str) -> bool:
    """YYYYMMDD 是否为 A 股标准财务报告期（0331/0630/0930/1231）。"""
    return len(as_of_str) == 8 and as_of_str[4:] in _STANDARD_REPORT_MMDD

'''
s = s.replace(anchor, helper, 1)

# 2) annotate standard/non-standard in the parse block
old_parse = '''        as_of_str = ""
        as_of = None
        try:
            raw_date = str(df.iloc[0][date_col])[:10].replace("-", "").replace("/", "")
            if raw_date.isdigit() and len(raw_date) == 8:
                as_of = datetime.strptime(raw_date, "%Y%m%d")
                as_of_str = raw_date
        except Exception:
            as_of = None
        age_days = (datetime.now() - as_of).days if as_of else None
'''
new_parse = '''        as_of_str = ""
        as_of = None
        try:
            raw_date = str(df.iloc[0][date_col])[:10].replace("-", "").replace("/", "")
            if raw_date.isdigit() and len(raw_date) == 8:
                as_of = datetime.strptime(raw_date, "%Y%m%d")
                as_of_str = raw_date
        except Exception:
            as_of = None
        # 【C-股东户数】口径标注：仅标准财务报告期（0331/0630/0930/1231）称"报告期"，
        # 其余统计截止日（披露/抓取日）标注"非标准报告期"，不虚构报告期语义。
        period_standard = _is_standard_report_date(as_of_str) if as_of_str else False
        period_label = (
            f"报告期{as_of_str}"
            if period_standard
            else f"统计截止日{as_of_str or 'N/A'}（非标准报告期）"
        )
        age_days = (datetime.now() - as_of).days if as_of else None
'''
assert old_parse in s, "parse block not found"
s = s.replace(old_parse, new_parse, 1)

# 3) _raw_common add report_period_standard
old_rc = '''        _raw_common = {
            "latest": latest_count, "prev": prev_count, "change_pct": change_pct,
            "as_of": as_of_str, "age_days": age_days,
        }
'''
new_rc = '''        _raw_common = {
            "latest": latest_count, "prev": prev_count, "change_pct": change_pct,
            "as_of": as_of_str, "age_days": age_days,
            "report_period_standard": period_standard,
        }
'''
assert old_rc in s, "raw_common not found"
s = s.replace(old_rc, new_rc, 1)

# 4) stale detail
old_stale = '''                "detail": (
                    f"股东户数{as_of_str}（报告期级，滞后{age_days}天>"
                    f"{_SHAREHOLDER_STALE_DAYS}天）不参与投票"
                ),
'''
new_stale = '''                "detail": (
                    f"股东户数{period_label}（报告期级滞后{age_days}天>"
                    f"{_SHAREHOLDER_STALE_DAYS}天）不参与投票"
                ),
'''
assert old_stale in s, "stale detail not found"
s = s.replace(old_stale, new_stale, 1)

# 5) vote detail strings
for old, new in [
    (f'f"报告期{{as_of_str or \'N/A\'}}: 股东户数减少 {{change_pct*100:.1f}}%（{{prev_count:.0f}}→{{latest_count:.0f}}，筹码集中）"',
     f'f"{{period_label}}: 股东户数减少 {{change_pct*100:.1f}}%（{{prev_count:.0f}}→{{latest_count:.0f}}，筹码集中）"'),
    (f'f"报告期{{as_of_str or \'N/A\'}}: 股东户数增加 {{change_pct*100:.1f}}%（{{prev_count:.0f}}→{{latest_count:.0f}}，筹码分散）"',
     f'f"{{period_label}}: 股东户数增加 {{change_pct*100:.1f}}%（{{prev_count:.0f}}→{{latest_count:.0f}}，筹码分散）"'),
    (f'f"报告期{{as_of_str or \'N/A\'}}: 股东户数持平（变化 {{change_pct*100:.1f}}%）"',
     f'f"{{period_label}}: 股东户数持平（变化 {{change_pct*100:.1f}}%）"'),
]:
    assert old in s, f"vote detail not found: {old[:60]}"
    s = s.replace(old, new, 1)

io.open(TARGET, "w", encoding="utf-8").write(s)
print("patched institutional_scorer.py OK")

# 6) theme_map.yaml: add 腾景科技 override
TP = r"C:\Users\15831\Downloads\stock-agent2\config\theme_map.yaml"
tm = io.open(TP, encoding="utf-8").read()
marker = '  # 光模块/CPO\n'
assert marker in tm, "cpomarker not found"
addition = '  # 光模块/CPO\n  "688195": 光模块CPO   # 腾景科技（精密光学元件，被误归电子化学品）\n'
tm = tm.replace(marker, addition, 1)
io.open(TP, "w", encoding="utf-8").write(tm)
print("patched theme_map.yaml OK")
