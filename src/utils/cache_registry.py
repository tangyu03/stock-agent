"""
P2-3: 模块级全局缓存统一管理说明
=================================
项目有6+个模块级缓存，无统一TTL策略。本文件记录各缓存位置和策略。

缓存清单：
1. stock_data.py: _spot_cache（实时行情），_quote_prefetch_cache（预取行情）
2. market_env.py: _market_env_cache（市场环境）
3. institutional_scorer.py: _margin_market_cache（融资余额），_institutional_session_cache（机构打分）
4. lhb_scorer.py: _lhb_detail_cache / _lhb_jgmmtj_cache（龙虎榜）
5. event_calendar.py: _unlock_summary_cache（解禁日历）
6. data_cache.py: SQLite 持久化缓存（唯一统一管理的）

TTL策略：
- 实时行情：session级（当日失效）
- 龙虎榜/融资余额：session级（1小时TTL）
- 解禁日历：session级（当日失效）
- SQLite缓存：统一TTL（默认8小时，可配置）

后续改进方向：
- 所有session级缓存统一改为 DataCache.get_or_fetch(cache_key, fetch_func, ttl_hours)
- 减少6个模块各自的缓存管理代码
"""
