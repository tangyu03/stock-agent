"""
AKShare 数据适配器
封装 AKShare 接口，提供统一的异常处理和降级机制

反反爬三大策略：
1. 降速 + 随机延时 —— 避免规律性请求触发 WAF
2. 动态请求头模拟 —— 每次请求轮换 UA/Referer/Client-Hints
3. 多数据源切换 —— 东财不可用时自动降级到新浪/腾讯源
"""
import logging
import random
import time
from typing import Optional, Dict, List, Any, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 动态请求头池
# ---------------------------------------------------------------------------
USER_AGENT_POOL = [
    # Chrome (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome (Mac)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    # Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
]

REFERER_POOL = [
    "https://quote.eastmoney.com/",
    "https://data.eastmoney.com/",
    "https://www.eastmoney.com/",
    "https://finance.sina.com.cn/",
    "https://stockpage.10jqka.com.cn/",
]

ACCEPT_LANGUAGES = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9",
    "zh-CN,zh;q=0.8,en-US;q=0.6,en;q=0.4",
]


def _build_random_headers() -> Dict[str, str]:
    """每次调用生成一组随机请求头，模拟不同浏览器环境"""
    ua = random.choice(USER_AGENT_POOL)
    # 从 UA 推断 sec-ch-ua 平台提示（Chrome only）
    if "Chrome/126" in ua:
        sec_ch_ua = '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"'
    elif "Chrome/125" in ua:
        sec_ch_ua = '"Chromium";v="125", "Google Chrome";v="125", "Not-A.Brand";v="99"'
    elif "Chrome/124" in ua:
        sec_ch_ua = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
    elif "Edg/126" in ua:
        sec_ch_ua = '"Chromium";v="126", "Microsoft Edge";v="126", "Not-A.Brand";v="99"'
    else:
        sec_ch_ua = ""

    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": random.choice(REFERER_POOL),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if sec_ch_ua:
        headers.update({
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"' if "Macintosh" in ua else '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        })
    return headers


# ---------------------------------------------------------------------------
# 数据源定义
# ---------------------------------------------------------------------------
class DataSource:
    """数据源描述"""
    EASTMONEY = "eastmoney"   # 东方财富（默认，数据最全）
    SINA = "sina"             # 新浪（备选，稳定性好）
    TENCENT = "tencent"       # 腾讯（备选）


# 接口→多源映射表：每个业务接口可用的数据源及对应 akshare 函数
# 格式: { 业务方法内部source_key: [(优先级, 数据源, akshare函数名, 参数适配器), ...] }
SOURCE_FALLBACK_MAP = {
    "stock_zh_a_hist": [
        (1, DataSource.EASTMONEY, "stock_zh_a_hist", None),              # 东财（默认，数据最全）
        (2, DataSource.SINA, "stock_zh_a_daily", "sina_code_adapter"),    # 新浪（备选）
    ],
    "stock_zh_bj_hist": [
        (1, DataSource.SINA, "stock_zh_bj_daily", "sina_bj_code_adapter"),  # 新浪北交所接口
    ],
    "stock_zh_index_daily": [
        # 新浪稳定优先；东财备选。
        # 腾讯 stock_zh_index_daily_tx 已移除：其内部 get_tx_start_year + 按年循环走
        # proxy.finance.qq.com 无 timeout 裸请求，在本环境运行时会挂死 30s，得不偿失。
        (1, DataSource.SINA, "stock_zh_index_daily", "sina_code_adapter"),
        (2, DataSource.EASTMONEY, "stock_zh_index_daily_em", None),
    ],
    # 注：stock_zh_a_spot（全市场行情）已移除——涨跌家数改走乐咕接口、
    # 成交额改走腾讯指数、个股行情改走腾讯 qt.gtimg.cn，均不再拉全市场。
}


def _adapt_sina_code(code: str) -> str:
    """
    将纯数字股票代码转换为新浪格式：sh600519 / sz000001

    东财接口 symbol="000001" → 新浪接口 symbol="sh000001"
    """
    if not code:
        return code
    code = code.strip()
    # 已经有前缀就直接返回
    if code.startswith(("sh", "sz", "SH", "SZ")):
        return code.lower()
    # 6开头=上海，0/3开头=深圳
    if code.startswith("6"):
        return f"sh{code}"
    elif code.startswith(("0", "3")):
        return f"sz{code}"
    return code


def _is_bj_stock(code: str) -> bool:
    """判断是否为北交所股票（92/83/87/89 开头）"""
    if not code:
        return False
    code = code.strip()
    return code[:2] in ("92", "83", "87", "89") and len(code) == 6


def _adapt_bj_code(code: str) -> str:
    """
    北交所代码适配：东财接口对北交所需要带 BJ 前缀
    920438 → BJ920438
    """
    if not code:
        return code
    code = code.strip()
    if _is_bj_stock(code) and not code.startswith("BJ"):
        return f"BJ{code}"
    return code


def _adapt_sina_bj_code(code: str) -> str:
    """
    北交所新浪代码适配：新浪接口需要 bj 前缀
    920438 → bj920438
    """
    if not code:
        return code
    code = code.strip()
    if _is_bj_stock(code) and not code.startswith("bj"):
        return f"bj{code}"
    return code


# ---------------------------------------------------------------------------
# AKShareResult
# ---------------------------------------------------------------------------
@dataclass
class AKShareResult:
    """AKShare 接口返回的统一结构"""
    success: bool
    data: Any = None
    error: str = ""
    source: str = ""         # 接口名称
    data_source: str = ""    # 实际使用的数据源 (eastmoney / sina)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ---------------------------------------------------------------------------
# AKShareAdapter
# ---------------------------------------------------------------------------
class AKShareAdapter:
    """
    AKShare 数据适配器，封装常用接口

    反反爬策略：
    - 策略1：降速 + 随机延时，避免规律性请求
    - 策略2：动态请求头模拟，每次调用轮换 UA
    - 策略3：多数据源切换，东财不可用自动降级新浪
    """

    # 重试配置
    MAX_RETRIES = 3
    RETRY_DELAY = 3              # 基础延迟(秒)
    CIRCUIT_BREAKER_THRESHOLD = 3  # 连续失败3次后熔断
    CIRCUIT_BREAKER_COOLDOWN = 120  # 熔断冷却(秒)

    # ---------- 短时源熔断（针对 RemoteDisconnected 等反爬信号）----------
    # 某数据源在某接口上出现反爬类错误后，短时间内直接跳过该源，避免每次都浪费时间重试
    SOFT_BLOCK_COOLDOWN = 60     # 软熔断冷却(秒)，远小于硬熔断的 120s

    # ---------- 策略1：降速 + 随机延时参数 ----------
    MIN_INTERVAL = 1.5           # 最小请求间隔(秒)
    RANDOM_DELAY_MIN = 0.5      # 随机附加延迟下限(秒)
    RANDOM_DELAY_MAX = 2.0      # 随机附加延迟上限(秒)
    BATCH_EXTRA_DELAY = 1.0     # 批量请求额外等待(秒)

    def __init__(self):
        self._health: Dict[str, bool] = {}
        self._failure_counts: Dict[str, int] = {}
        self._circuit_open_until: Dict[str, float] = {}
        self._soft_block_until: Dict[str, float] = {}  # 短时源熔断（反爬信号触发）
        self._last_call_time: float = 0
        self._call_count: int = 0           # 累计调用计数（用于动态调速）
        self._source_status: Dict[str, str] = {}  # 数据源状态缓存
        self._ak = None
        self._session_patched = False       # 是否已成功 patch session
        self._ping()
        self._setup_anti_crawl()

    # ================================================================
    # 策略2：动态请求头模拟
    # ================================================================
    def _setup_anti_crawl(self):
        """
        反反爬初始化：设置全局 requests 默认 headers

        方案：在 requests.Session.__init__ 后自动注入随机 headers
        比劫持 request 方法更安全，不会导致无限递归
        """
        if self._ak is None:
            return

        try:
            import requests
            # 保存原始 __init__
            _original_init = requests.Session.__init__

            def _patched_init(self_session, *args, **kwargs):
                _original_init(self_session, *args, **kwargs)
                # 每次创建新 Session 时注入随机 headers
                self_session.headers.update(_build_random_headers())

            requests.Session.__init__ = _patched_init

            # 同时给现有全局 Session 刷新 headers
            # akshare 内部可能复用某个 session 实例
            try:
                import akshare
                for attr_name in dir(akshare):
                    obj = getattr(akshare, attr_name, None)
                    if obj is None:
                        continue
                    sess = getattr(obj, 'session', None)
                    if sess is not None and isinstance(sess, requests.Session):
                        sess.headers.update(_build_random_headers())
            except Exception:
                pass

            self._session_patched = True
            logger.info("AKShare 反爬 Session patch 成功（动态 headers 注入）")

        except Exception as e:
            logger.debug("Session patch 失败，将使用方案B: %s", e)
            self._session_patched = False



    # ================================================================
    # 策略1：降速 + 随机延时
    # ================================================================
    def _rate_limit(self):
        """
        请求频率控制：基础间隔 + 随机抖动

        规则：
        - 两次请求之间至少间隔 MIN_INTERVAL 秒
        - 额外添加 [RANDOM_DELAY_MIN, RANDOM_DELAY_MAX] 的随机延时
        - 连续调用次数越多，基础间隔自动微增（动态调速）
        """
        now = time.time()
        elapsed = now - self._last_call_time

        # 动态调整基础间隔：调用越多越慢，最多 2x
        dynamic_min = self.MIN_INTERVAL * (1 + min(self._call_count * 0.01, 1.0))

        # 基础等待
        if elapsed < dynamic_min:
            base_wait = dynamic_min - elapsed
        else:
            base_wait = 0

        # 随机抖动
        jitter = random.uniform(self.RANDOM_DELAY_MIN, self.RANDOM_DELAY_MAX)

        total_wait = base_wait + jitter
        if total_wait > 0:
            logger.debug("Rate limiting: waiting %.2fs (base=%.2fs, jitter=%.2fs, call_count=%d)",
                         total_wait, base_wait, jitter, self._call_count)
            time.sleep(total_wait)

    def _batch_delay(self):
        """批量请求之间的额外延迟"""
        delay = self.BATCH_EXTRA_DELAY + random.uniform(0.3, 1.0)
        logger.debug("Batch delay: %.2fs", delay)
        time.sleep(delay)

    # ================================================================
    # 熔断器
    # ================================================================
    def _is_circuit_open(self, source: str) -> bool:
        """检查某接口是否被熔断"""
        open_until = self._circuit_open_until.get(source, 0)
        return time.time() < open_until

    def _record_failure(self, source: str):
        """记录接口失败，达到阈值后熔断"""
        count = self._failure_counts.get(source, 0) + 1
        self._failure_counts[source] = count
        if count >= self.CIRCUIT_BREAKER_THRESHOLD:
            self._circuit_open_until[source] = time.time() + self.CIRCUIT_BREAKER_COOLDOWN
            logger.warning("Circuit breaker OPEN for %s (failures=%d, cooldown=%ds)",
                           source, count, self.CIRCUIT_BREAKER_COOLDOWN)
        # 全局不可用判定
        failed_sources = sum(1 for c in self._failure_counts.values()
                            if c >= self.CIRCUIT_BREAKER_THRESHOLD)
        if failed_sources >= 5:
            self._health["akshare"] = False
            logger.warning("AKShare marked globally unavailable (%d sources failed)", failed_sources)

    def _record_soft_block(self, source: str):
        """
        短时源熔断：反爬信号（RemoteDisconnected 等）触发后，短时间内跳过该源

        与硬熔断的区别：
        - 硬熔断：连续失败 3 次后触发，冷却 120s，影响整个 source_key
        - 软熔断：反爬信号 1 次即触发，冷却 60s，只影响特定 source_key+数据源 组合
        软熔断的目的是避免在已知被反爬的源上重复浪费时间（每次 RemoteDisconnected
        都要等 1.5s 限速 + 请求超时），让 fallback 更快地切换到可用源。
        """
        self._soft_block_until[source] = time.time() + self.SOFT_BLOCK_COOLDOWN

    def _is_soft_blocked(self, source: str) -> bool:
        """检查某源是否被短时熔断"""
        return time.time() < self._soft_block_until.get(source, 0)

    def _record_success(self, source: str):
        """记录接口成功，重置失败计数"""
        self._failure_counts.pop(source, None)
        self._circuit_open_until.pop(source, None)

    # ================================================================
    # 策略3：多数据源切换
    # ================================================================

    # 各数据源函数签名缓存（用于自动过滤不兼容的参数）
    _FUNC_PARAM_CACHE: Dict[str, List[str]] = {}

    def _filter_kwargs_for_func(self, func, kwargs: Dict) -> Dict:
        """
        根据函数签名过滤掉不支持的参数，避免 TypeError

        新浪 stock_zh_a_daily(symbol) 不接受 period/start_date/end_date/adjust
        东财 stock_zh_a_hist(symbol, period, start_date, ...) 全部支持
        """
        func_name = getattr(func, '__name__', str(func))

        # 缓存函数参数列表
        if func_name not in self._FUNC_PARAM_CACHE:
            try:
                import inspect
                sig = inspect.signature(func)
                params = list(sig.parameters.keys())
                self._FUNC_PARAM_CACHE[func_name] = params
            except (ValueError, TypeError):
                # 无法获取签名，不过滤
                return kwargs

        allowed = self._FUNC_PARAM_CACHE[func_name]
        filtered = {k: v for k, v in kwargs.items() if k in allowed}

        removed = set(kwargs.keys()) - set(filtered.keys())
        if removed:
            logger.debug("函数 %s 过滤掉不兼容参数: %s", func_name, removed)

        return filtered

    def _call_with_fallback(self, source_key: str, fallback_list: List[tuple], **kwargs) -> Any:
        """
        带多源降级的 AKShare 调用

        Args:
            source_key: 业务接口标识，用于熔断追踪
            fallback_list: [(优先级, 数据源名, akshare函数名, 参数适配器名), ...]
            **kwargs: 传给 akshare 函数的参数

        Returns:
            DataFrame 或 None
        """
        # 按优先级排序
        fallback_list = sorted(fallback_list, key=lambda x: x[0])

        for priority, ds_name, func_name, param_adapter in fallback_list:
            # 检查该数据源该接口是否被熔断
            ds_source_key = f"{source_key}__{ds_name}"
            if self._is_circuit_open(ds_source_key):
                logger.debug("数据源 %s 的 %s 被硬熔断，跳过", ds_name, source_key)
                continue
            # 检查短时软熔断（反爬信号触发，60s 冷却）
            if self._is_soft_blocked(ds_source_key):
                logger.debug("数据源 %s 的 %s 被软熔断(反爬)，跳过", ds_name, source_key)
                continue

            # 参数适配
            call_kwargs = dict(kwargs)
            if param_adapter == "sina_code_adapter" and "symbol" in call_kwargs:
                call_kwargs["symbol"] = _adapt_sina_code(call_kwargs["symbol"])
            elif param_adapter == "sina_bj_code_adapter" and "symbol" in call_kwargs:
                call_kwargs["symbol"] = _adapt_sina_bj_code(call_kwargs["symbol"])

            # 获取 akshare 函数
            func = getattr(self._ak, func_name, None)
            if func is None:
                logger.debug("akshare 函数 %s 不存在，跳过数据源 %s", func_name, ds_name)
                continue

            # 策略3核心：自动过滤不兼容的参数
            call_kwargs = self._filter_kwargs_for_func(func, call_kwargs)

            # 调用
            result = self._call_with_retry(func, ds_source_key, **call_kwargs)
            if result is not None:
                self._source_status[source_key] = ds_name
                logger.info("数据源 %s 返回成功 (%s)", ds_name, source_key)
                return result

            logger.warning("数据源 %s 失败 (%s)，尝试下一个", ds_name, source_key)

        # 所有数据源都失败
        self._record_failure(source_key)
        logger.error("所有数据源均失败 (%s)", source_key)
        return None

    # ================================================================
    # 核心：带重试的调用
    # ================================================================
    def _call_with_retry(self, func, source: str, *args, **kwargs):
        """带重试、熔断和频率控制的 AKShare 调用"""
        # 检查熔断
        if self._is_circuit_open(source):
            logger.debug("Circuit breaker open for %s, skipping", source)
            return None

        last_error = None
        for attempt in range(self.MAX_RETRIES + 1):
            # 策略1：降速 + 随机延时
            self._rate_limit()
            self._call_count += 1
            self._last_call_time = time.time()

            try:
                import concurrent.futures
                # P2-11: 超时后线程不会真正停止，但akshare最终会因socket超时返回
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(func, *args, **kwargs)
                    result = future.result(timeout=30)  # 每次调用最多30秒
                self._record_success(source)
                return result
            except concurrent.futures.TimeoutError:
                # 超时通常意味着该源在当前网络环境不通，重试无意义 → 直接放弃，走 fallback
                logger.warning("AKShare %s 调用超时(30s)，跳过该源（不重试）", source)
                self._record_failure(source)
                return None
            except Exception as e:
                last_error = e
                error_str = str(e)
                error_type = type(e).__name__

                # 结构性数据错误（KeyError/AttributeError）→ 不重试
                # 典型：新股 K 线不足，sina 返回数据缺少 'date' 列 → KeyError
                # 这表示数据源对该股票无数据，重试不会改变结果
                if error_type in ("KeyError", "AttributeError"):
                    self._record_failure(source)
                    logger.warning(
                        "AKShare %s 结构性数据错误(%s: %s)，跳过重试（可能为新股/数据不足）",
                        source, error_type, error_str[:100],
                    )
                    return None

                # 反爬类错误 → 不重试，直接熔断
                is_anti_crawl = any(kw in error_str for kw in [
                    "403", "Forbidden", "blocked", "黑名单", "限制访问",
                    "验证码", "captcha", "请输入验证码",
                ])
                if is_anti_crawl:
                    self._record_failure(source)
                    logger.error("AKShare %s 被反爬拦截: %s", source, error_str[:200])
                    break

                # RemoteDisconnected → 东财特有，尝试切换源
                is_remote_disconnect = "RemoteDisconnected" in error_str or "Connection aborted" in error_str
                if is_remote_disconnect:
                    # RemoteDisconnected 不浪费重试次数，直接标记失败并跳到下一个源
                    self._record_failure(source)
                    # 软熔断：60s 内该 source_key+数据源 组合不再尝试
                    self._record_soft_block(source)
                    logger.warning("AKShare %s RemoteDisconnected（反爬，软熔断 %ds），跳到下一个数据源",
                                   source, self.SOFT_BLOCK_COOLDOWN)
                    return None

                # 其他错误 → 指数退避重试
                if attempt < self.MAX_RETRIES:
                    delay = self.RETRY_DELAY * (2 ** attempt) + random.uniform(1, 3)
                    logger.warning("AKShare %s attempt %d/%d failed: %s, retry in %.1fs",
                                   source, attempt + 1, self.MAX_RETRIES + 1,
                                   error_str[:100], delay)
                    time.sleep(delay)
                else:
                    self._record_failure(source)
                    logger.error("AKShare %s failed after %d attempts: %s",
                                 source, self.MAX_RETRIES + 1, error_str[:200])

        return None

    # ================================================================
    # 启动检测
    # ================================================================
    def _ping(self):
        """启动时检测可用性 —— 先试东财，失败再试新浪（带超时保护）"""
        try:
            import akshare as ak
            self._ak = ak
        except ImportError:
            self._ak = None
            self._health["akshare"] = False
            logger.error("AKShare not installed. Run: pip install akshare")
            return

        # 依次尝试东财 / 新浪小批量接口（用单只查询探活，不拉全市场）
        # P2 审计（2026-08-18）：每源最多重试 2 次，避免单次瞬时失败误报"均不可达"
        ping_sources = [
            ("eastmoney", lambda: ak.stock_bid_ask_em(symbol="000001")),
            ("sina", lambda: ak.stock_zh_a_hist(symbol="000001", period="daily", adjust="")),
        ]

        for name, ping_fn in ping_sources:
            for attempt in range(2):
                try:
                    time.sleep(random.uniform(1.0, 2.0))  # 启动时随机等待（同时充当重试间隔）
                    # 使用线程+超时保护，避免东方财富反爬导致无限挂起
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(ping_fn)
                        df = future.result(timeout=15)  # 最多等15秒
                    if df is not None and not df.empty:
                        self._health["akshare"] = True
                        self._source_status["global"] = name
                        logger.info("AKShare 初始化成功 (数据源=%s, spot_data=%d行)", name, len(df))
                        return
                except concurrent.futures.TimeoutError:
                    logger.warning("AKShare 数据源 %s ping 超时(15s)（第%d/2次），%s",
                                   name, attempt + 1, "重试" if attempt == 0 else "放弃该源")
                except Exception as e:
                    logger.debug("AKShare 数据源 %s ping 失败: %s", name, str(e)[:150])
                # 失败后继续：同源重试或进入下一源

        # 两个源都尝试 2 次仍不通，才标记不可达（避免单次瞬时失败误报）
        self._health["akshare"] = True
        self._source_status["global"] = "unreachable"
        logger.warning("AKShare 模块已加载但东财/新浪均不可达（各重试2次），将按需降级")

    def is_available(self) -> bool:
        """检查 AKShare 是否可用"""
        return self._health.get("akshare", False)

    def get_active_source(self, source_key: str = "global") -> str:
        """获取当前使用的数据源"""
        return self._source_status.get(source_key, DataSource.EASTMONEY)

    # ================================================================
    # 业务接口
    # ================================================================

    # ============ 涨跌停数据 ============

    def get_zt_pool(self, date: Optional[str] = None) -> AKShareResult:
        """
        获取涨停池数据
        包含涨停家数、连板高度等
        """
        if not self.is_available():
            return AKShareResult(success=False, error="AKShare not available", source="get_zt_pool")

        # ping检测不一定准确，仍按需尝试实际调用（熔断机制兜底）

        try:
            if date is None:
                date = datetime.now().strftime("%Y%m%d")
            df = self._call_with_retry(self._ak.stock_zt_pool_em, "stock_zt_pool_em", date=date)
            if df is None:
                return AKShareResult(success=False, error="Retry exhausted", source="stock_zt_pool_em")
            return AKShareResult(
                success=True,
                data=df.to_dict("records") if not df.empty else [],
                source="stock_zt_pool_em",
                data_source=DataSource.EASTMONEY,
            )
        except Exception as e:
            self._record_failure("stock_zt_pool_em")
            logger.error("get_zt_pool failed: %s", e)
            return AKShareResult(success=False, error=str(e), source="get_zt_pool")

    def get_dt_pool(self, date: Optional[str] = None) -> AKShareResult:
        """获取跌停池数据"""
        if not self.is_available():
            return AKShareResult(success=False, error="AKShare not available", source="get_dt_pool")

        # ping检测不一定准确，仍按需尝试实际调用（熔断机制兜底）

        try:
            if date is None:
                date = datetime.now().strftime("%Y%m%d")
            df = self._call_with_retry(self._ak.stock_zt_pool_dtgc_em, "stock_zt_pool_dtgc_em", date=date)
            if df is None:
                return AKShareResult(success=False, error="Retry exhausted", source="stock_zt_pool_dtgc_em")
            return AKShareResult(
                success=True,
                data=df.to_dict("records") if not df.empty else [],
                source="stock_zt_pool_dtgc_em",
                data_source=DataSource.EASTMONEY,
            )
        except Exception as e:
            self._record_failure("stock_zt_pool_dtgc_em")
            logger.error("get_dt_pool failed: %s", e)
            return AKShareResult(success=False, error=str(e), source="get_dt_pool")

    # ============ 北向资金 ============




    # P2-1: K线键名规范说明
    # 本模块返回的K线字典统一用中文键（"日期/开盘/收盘/最高/最低/成交量"）
    # 调用方用 k.get("收盘", k.get("close", 0)) 兼容写法读取
    # 后续迭代应统一为英文键，减少兼容代码
    def get_stock_hist(
        self,
        code: str,
        period: str = "daily",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        adjust: str = "qfq",
    ) -> AKShareResult:
        """
        获取个股历史K线数据（多源切换）

        Args:
            code: 股票代码，如"000001"（东财格式，新浪格式会自动转换）
            period: 周期 daily/weekly/monthly
            start_date: 起始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD
            adjust: 复权类型 qfq/hfq/"" (前复权/后复权/不复权)
        """
        if not self.is_available():
            return AKShareResult(success=False, error="AKShare not available", source="get_stock_hist")

        try:
            if end_date is None:
                end_date = datetime.now().strftime("%Y%m%d")
            if start_date is None:
                start_date = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")

            # ---- 北交所特殊处理 ----
            if _is_bj_stock(code):
                logger.info("检测到北交所股票 %s，使用新浪北交所接口", code)
                adapted_code = _adapt_sina_bj_code(code)
                records = self._get_bj_hist_sina(adapted_code)
                if records:
                    return AKShareResult(
                        success=True,
                        data=records,
                        source="stock_zh_bj_hist",
                        data_source=DataSource.SINA,
                    )
                else:
                    return AKShareResult(
                        success=False,
                        error="北交所股票新浪接口无数据",
                        source="stock_zh_bj_hist"
                    )

            # ---- 普通股票处理：东财为主，新浪为备选 ----
            adapted_code = code
            if _is_bj_stock(code):
                adapted_code = _adapt_bj_code(code)
                logger.debug("北交所代码适配: %s → %s", code, adapted_code)

            # ---- 策略3：多源切换 ----
            df = self._call_with_fallback(
                source_key="stock_zh_a_hist",
                fallback_list=SOURCE_FALLBACK_MAP["stock_zh_a_hist"],
                symbol=adapted_code,
                period=period,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
            if df is None:
                # 所有源都失败，判断是否因K线不足（次新股/北交所常见）
                err_msg = (
                    "All sources exhausted (possible reasons: BJ stock, "
                    "new IPO with insufficient K-line, or data unavailable)"
                )
                return AKShareResult(success=False, error=err_msg, source="stock_zh_a_hist")

            active_ds = self.get_active_source("stock_zh_a_hist")
            records = df.to_dict("records") if not df.empty else []

            # P2-1: 统一所有源的K线为英文键
            records = self._normalize_to_english(records)

            return AKShareResult(
                success=True,
                data=records,
                source="stock_zh_a_hist",
                data_source=active_ds,
            )
        except Exception as e:
            logger.error("get_stock_hist failed for %s: %s", code, e)
            return AKShareResult(success=False, error=str(e), source="get_stock_hist")

    # ============ 指数数据 —— 多源切换 ============

    def get_index_data(self, symbol: str = "000001") -> AKShareResult:
        """
        获取指数行情数据（多源切换）

        Args:
            symbol: 指数代码 000001=上证 399001=深证 399006=创业板
        """
        if not self.is_available():
            return AKShareResult(success=False, error="AKShare not available", source="get_index_data")

        # ping检测不一定准确，仍按需尝试实际调用（熔断机制兜底）


        try:
            # 新浪指数代码格式：sh000001 / sz399001
            sina_symbol = _adapt_sina_code(symbol)

            df = self._call_with_fallback(
                source_key="stock_zh_index_daily",
                fallback_list=SOURCE_FALLBACK_MAP["stock_zh_index_daily"],
                symbol=sina_symbol,  # 新浪接口需要带前缀
            )
            if df is None:
                return AKShareResult(success=False, error="All sources exhausted", source="stock_zh_index_daily")

            active_ds = self.get_active_source("stock_zh_index_daily")
            return AKShareResult(
                success=True,
                data=df.to_dict("records") if not df.empty else [],
                source="stock_zh_index_daily",
                data_source=active_ds,
            )
        except Exception as e:
            logger.error("get_index_data failed for %s: %s", symbol, e)
            return AKShareResult(success=False, error=str(e), source="get_index_data")

    # ============ 涨跌家数 —— 多源切换 ============

    def get_advance_decline(self) -> AKShareResult:
        """
        获取市场涨跌家数统计

        用乐咕市场活跃度接口 stock_market_activity_legu（轻量，直接返回汇总，
        不拉全市场 spot），规避全市场接口被封的问题。
        """
        if not self.is_available():
            return AKShareResult(success=False, error="AKShare not available", source="get_advance_decline")

        try:
            df = self._call_with_retry(
                self._ak.stock_market_activity_legu, "stock_market_activity_legu"
            )
            if df is not None and not df.empty:
                # df 是 item/value 两列的汇总表
                stats = {}
                for _, row in df.iterrows():
                    item = str(row.get("item", ""))
                    val = row.get("value", 0)
                    stats[item] = val

                def _num(key):
                    try:
                        return int(float(stats.get(key, 0)))
                    except (ValueError, TypeError):
                        return 0

                advance = _num("上涨")
                decline = _num("下跌")
                flat = _num("平盘")
                total = advance + decline + flat
                ratio = advance / decline if decline > 0 else float("inf")

                return AKShareResult(
                    success=True,
                    data={
                        "total": total,
                        "advance": advance,
                        "decline": decline,
                        "flat": flat,
                        "advance_decline_ratio": round(ratio, 2),
                    },
                    source="stock_market_activity_legu",
                    data_source="legu",
                )

            return AKShareResult(success=False, error="乐咕接口无数据", source="stock_market_activity_legu")
        except Exception as e:
            logger.error("get_advance_decline failed: %s", e)
            return AKShareResult(success=False, error=str(e), source="get_advance_decline")

    # ============ 北交所历史K线 —— 新浪源 ============

    def _get_bj_hist_sina(self, symbol: str) -> Optional[List[Dict]]:
        """从新浪获取北交所历史K线数据"""
        try:
            import requests

            url = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
            params = {
                "symbol": symbol,
                "scale": "240",  # 日线
                "ma": "no",
                "datalen": "200"  # 获取足够多的数据
            }

            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code != 200:
                return None

            raw_data = resp.text
            if not raw_data or raw_data == "null":
                return None

            import json
            data = json.loads(raw_data)
            if not data:
                return None

            # 转换为和东财相同的格式
            normalized = []
            for item in data:
                normalized.append({
                    "日期": item["day"],
                    "开盘": float(item["open"]),
                    "最高": float(item["high"]),
                    "最低": float(item["low"]),
                    "收盘": float(item["close"]),
                    "成交量": float(item["volume"]),
                })

            return normalized

        except Exception as e:
            logger.error("_get_bj_hist_sina failed for %s: %s", symbol, e)
            return None

    # ============ 市场成交额 —— 多源切换 ============

    def get_market_volume(self) -> AKShareResult:
        """
        获取沪深两市总成交额

        用腾讯指数行情（上证+深证成交额相加）直接获取，不拉全市场 spot。
        腾讯 qt.gtimg.cn 稳定，规避全市场接口被封的问题。
        """
        if not self.is_available():
            return AKShareResult(success=False, error="AKShare not available", source="get_market_volume")

        try:
            import requests
            import random
            _UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            r = requests.get("http://qt.gtimg.cn/q=sh000001,sz399001",
                             timeout=8, headers={"User-Agent": _UA})
            r.encoding = "gbk"

            total_amount = 0.0  # 单位：元
            for line in r.text.strip().split(";"):
                if "=" not in line:
                    continue
                parts = line.split("~")
                if len(parts) > 37:
                    try:
                        # 指数成交额在 [37]，单位万元
                        total_amount += float(parts[37]) * 10000
                    except (ValueError, TypeError):
                        continue

            if total_amount > 0:
                return AKShareResult(
                    success=True,
                    data={
                        "total_volume": total_amount,
                        "total_volume_yi": round(total_amount / 1e8, 2),
                    },
                    source="qt_index_turnover",
                    data_source="tencent",
                )

            return AKShareResult(success=False, error="腾讯指数成交额解析失败", source="get_market_volume")
        except Exception as e:
            logger.error("get_market_volume failed: %s", e)
            return AKShareResult(success=False, error=str(e), source="get_market_volume")

    # ============ 美股/VIX 外围数据 ============

    def get_us_market_snapshot(self) -> AKShareResult:
        """
        获取美股隔夜行情快照：S&P 500 / Nasdaq 涨跌幅 + VIX

        Returns:
            AKShareResult.data = {
                "sp500_change_pct": float | None,   # 标普500涨跌幅(%); None=无数据
                "nasdaq_change_pct": float | None,  # 纳斯达克涨跌幅(%)
                "dow_change_pct": float | None,     # 道琼斯涨跌幅(%)
                "vix": float | None,                # VIX 恐慌指数
                "vix_change_pct": float | None,     # VIX 涨跌幅(%)
            }
        """
        if not self.is_available():
            return AKShareResult(success=False, error="AKShare not available", source="get_us_market_snapshot")

        # P0-1 审计（2026-08-18）：无数据一律置 None，禁止用 0.0 当脏值兜底。
        # 美股闭市或接口不可达时，外围评估按"无数据"处理，而不是误读为"平盘/VIX=0"。
        data = {
            "sp500_change_pct": None,
            "nasdaq_change_pct": None,
            "dow_change_pct": None,
            "vix": None,
            "vix_change_pct": None,
        }

        try:
            import akshare as ak

            # 美股三大指数（新浪源）
            try:
                df_us = ak.index_us_stock_sina()
                if df_us is not None and not df_us.empty:
                    # 字段: 日期, 开盘, 最高, 最低, 收盘, 成交量, 名称
                    for _, row in df_us.iterrows():
                        name = str(row.get("名称", ""))
                        close = float(row.get("收盘", row.get("close", 0)))
                        prev_close = float(row.get("开盘", row.get("open", 0)))
                        if prev_close > 0:
                            chg = (close - prev_close) / prev_close * 100
                        else:
                            chg = 0.0

                        if "标普" in name or "SPX" in name.upper() or "S&P" in name.upper():
                            data["sp500_change_pct"] = round(chg, 2)
                        elif "纳斯达克" in name or "NASDAQ" in name.upper() or "IXIC" in name.upper():
                            data["nasdaq_change_pct"] = round(chg, 2)
                        elif "道琼斯" in name or "DJI" in name.upper():
                            data["dow_change_pct"] = round(chg, 2)
            except Exception as e:
                logger.debug("美股指数获取失败（新浪源）: %s，尝试东财源", e)
                # 降级：东财美股现货
                try:
                    df_sp = ak.stock_us_spot_em()
                    if df_sp is not None and not df_sp.empty:
                        for _, row in df_sp.iterrows():
                            code = str(row.get("代码", row.get("code", "")))
                            chg = float(row.get("涨跌幅", row.get("change_pct", 0)))
                            if "NDX" in code or "IXIC" in code:
                                data["nasdaq_change_pct"] = round(chg, 2)
                            elif "SPX" in code or "INX" in code:
                                data["sp500_change_pct"] = round(chg, 2)
                            elif "DJI" in code:
                                data["dow_change_pct"] = round(chg, 2)
                except Exception as e2:
                    logger.debug("美股指数获取失败（东财源）: %s", e2)

            # VIX 恐慌指数（全球指数现货）
            try:
                df_global = ak.index_global_spot_em()
                if df_global is not None and not df_global.empty:
                    for _, row in df_global.iterrows():
                        name = str(row.get("名称", row.get("name", "")))
                        if "VIX" in name.upper() or "恐慌" in name or "波动率" in name:
                            data["vix"] = round(float(row.get("最新价", row.get("close", 0))), 2)
                            data["vix_change_pct"] = round(float(row.get("涨跌幅", row.get("change_pct", 0))), 2)
                            break
            except Exception as e:
                logger.debug("VIX 获取失败: %s", e)

            def _fmt(v):
                return f"{v:.2f}" if v is not None else "None"

            populated = [k for k, v in data.items() if v is not None]
            if not populated:
                logger.warning(
                    "美股快照无任何有效数据（美股闭市/接口不可达）：SP500/Nasdaq/VIX 置 None，外围评估按无数据处理"
                )
            else:
                logger.info(
                    "美股快照: SP500=%s%% Nasdaq=%s%% VIX=%s (%s%%)",
                    _fmt(data["sp500_change_pct"]),
                    _fmt(data["nasdaq_change_pct"]),
                    _fmt(data["vix"]),
                    _fmt(data["vix_change_pct"]),
                )
            return AKShareResult(success=True, data=data, source="get_us_market_snapshot")

        except ImportError:
            return AKShareResult(success=False, error="AKShare not available", source="get_us_market_snapshot")
        except Exception as e:
            logger.error("get_us_market_snapshot failed: %s", e)
            return AKShareResult(success=False, error=str(e), source="get_us_market_snapshot")

    def get_us_futures_snapshot(self) -> AKShareResult:
        """
        获取美股期货实时行情（盘前参考）

        Returns:
            AKShareResult.data = {
                "sp500_futures_change_pct": float | None,   # 标普500期货涨跌幅(%); None=无数据
                "nasdaq_futures_change_pct": float | None,  # 纳斯达克期货涨跌幅(%)
                "dow_futures_change_pct": float | None,     # 道琼斯期货涨跌幅(%)
            }
        """
        if not self.is_available():
            return AKShareResult(success=False, error="AKShare not available", source="get_us_futures_snapshot")

        # P0-1 审计（2026-08-18）：与 get_us_market_snapshot 一致，无数据置 None 而非 0.0。
        data = {
            "sp500_futures_change_pct": None,
            "nasdaq_futures_change_pct": None,
            "dow_futures_change_pct": None,
        }

        try:
            import akshare as ak

            # 美股期货（新浪源：ES=标普500, NQ=纳斯达克100, YM=道琼斯）
            futures_map = {
                "ES": "sp500_futures_change_pct",
                "NQ": "nasdaq_futures_change_pct",
                "YM": "dow_futures_change_pct",
            }

            for symbol, key in futures_map.items():
                try:
                    df = ak.futures_foreign_hist(symbol=symbol)
                    if df is not None and len(df) >= 2:
                        last = float(df.iloc[-1].get("收盘", df.iloc[-1].get("close", 0)))
                        prev = float(df.iloc[-2].get("收盘", df.iloc[-2].get("close", last)))
                        if prev > 0:
                            data[key] = round((last - prev) / prev * 100, 2)
                except Exception as e:
                    logger.debug("美股期货 %s 获取失败: %s", symbol, e)

            def _fmt(v):
                return f"{v:.2f}" if v is not None else "None"

            populated = [k for k, v in data.items() if v is not None]
            if not populated:
                logger.warning("美股期货无任何有效数据（接口不可达）：SP500/Nasdaq/Dow 期货涨跌幅置 None")
            else:
                logger.info(
                    "美股期货: SP500=%s%% Nasdaq=%s%% Dow=%s%%",
                    _fmt(data["sp500_futures_change_pct"]),
                    _fmt(data["nasdaq_futures_change_pct"]),
                    _fmt(data["dow_futures_change_pct"]),
                )
            return AKShareResult(success=True, data=data, source="get_us_futures_snapshot")

        except ImportError:
            return AKShareResult(success=False, error="AKShare not available", source="get_us_futures_snapshot")
        except Exception as e:
            logger.error("get_us_futures_snapshot failed: %s", e)
            return AKShareResult(success=False, error=str(e), source="get_us_futures_snapshot")

    # ============ 数据标准化 ============

    @staticmethod
    @staticmethod
    def _normalize_to_english(records: List[Dict]) -> List[Dict]:
        """
        P2-1: 统一K线为英文键（date/open/high/low/close/volume）
        同时保留中文键作为别名（双向兼容，调用方可任选）
        """
        field_map = {
            "日期": "date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume",
        }
        # 中文→英文别名映射（供调用方兼容写法）
        cn_aliases = {
            "date": "日期", "open": "开盘", "high": "最高",
            "low": "最低", "close": "收盘", "volume": "成交量",
        }
        normalized = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            new_rec = {}
            # 先映射英文键
            for old_key, val in rec.items():
                new_key = field_map.get(old_key, old_key)
                new_rec[new_key] = val
            # 再加中文别名（双向兼容）
            for en_key, cn_key in cn_aliases.items():
                if en_key in new_rec and cn_key not in new_rec:
                    new_rec[cn_key] = new_rec[en_key]
            normalized.append(new_rec)
        return normalized

    @staticmethod
    def _normalize_sina_hist(records: List[Dict]) -> List[Dict]:
        """P2-1: 保留兼容（委托给 _normalize_to_english）"""
        return AKShareAdapter._normalize_to_english(records)


# 单例
_instance: Optional[AKShareAdapter] = None


def get_akshare_adapter() -> AKShareAdapter:
    global _instance
    if _instance is None:
        _instance = AKShareAdapter()
    return _instance
