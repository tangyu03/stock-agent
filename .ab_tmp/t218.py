import sys
sys.path.insert(0, '.')
from src.push.pushplus import PushPlus

# 1. chunk 原子化：卡片不跨块
cards = []
for i in range(6):
    cards.append(f"<b>卡{i} 名称{i}(00{i})</b><br/>{'x'*3000}<br/>")
content = "<hr/>".join(cards)
chunks = PushPlus._chunk_content(content, max_len=10000)
print("chunk count:", len(chunks))
ok = True
for ci, c in enumerate(chunks):
    for i in range(6):
        if f"卡{i}" in c:
            # 卡必须是完整块
            if f"名称{i}" not in c:
                ok = False
                print(f"chunk {ci}: 卡{i} 被切断!")
print("all cards atomic:", ok)

# 2. 数据时点
print("sample_time:", PushPlus._latest_sample_time([
    {"tech_signals": {"volume_snapshot": {"volume_ratio_sample_time": "20260917090500"}}},
    {"tech_signals": {"volume_snapshot": {"same_period_sample_time": "20260917140200"}}},
    {"tech_signals": {"volume_snapshot": {}}},
]))
print("progress 14:07:", PushPlus._trading_progress("20260917140700"))
print("progress 09:30:", PushPlus._trading_progress("20260917093000"))
print("progress 11:30:", PushPlus._trading_progress("20260917113000"))
print("progress 13:00:", PushPlus._trading_progress("20260917130000"))
print("progress 15:00:", PushPlus._trading_progress("20260917150000"))
