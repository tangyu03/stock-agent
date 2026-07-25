"""
A股短线交易 Agent 主入口

用法:
  python -m src.main run --phase pre_market      盘前预案 (8:50)
  python -m src.main run --phase intraday        盘中统一检查 (实时信号)
  python -m src.main run --phase post_market     盘后复盘 (15:30)
  python -m src.main run --phase weekly          周报

  python -m src.main init                        初始化
  python -m src.main article --text "..."        处理文章
"""
import os
import sys
import argparse
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime

os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    import time as _time
    _time.tzset()
except Exception:
    pass

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db import init_db
from src.config_models import load_all_configs
from src.orchestrator.engine import get_orchestrator

LOG_DIR = PROJECT_ROOT / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.TimedRotatingFileHandler(
            LOG_DIR / "agent.log",
            when="midnight",
            backupCount=30,
            encoding="utf-8",
        ),
    ],
)

logger = logging.getLogger(__name__)

PHASES = [
    "pre_market",
    "intraday",
    "post_market",
    "weekly",
]


def main():
    parser = argparse.ArgumentParser(description="A股短线交易 Agent")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="初始化数据库+校验配置")

    ap = sub.add_parser("article", help="处理文章/观点")
    ap.add_argument("--text", type=str, required=True, help="文章内容")
    ap.add_argument("--source", type=str, default="", help="文章来源")

    run_p = sub.add_parser("run", help="执行调度阶段")
    run_p.add_argument("--phase", type=str, required=True, choices=PHASES,
                       help="调度阶段: " + " | ".join(PHASES))

    args = parser.parse_args()

    if args.command == "init":
        logger.info("=== 初始化 ===")
        init_db()
        try:
            for name in load_all_configs():
                logger.info("OK %s", name)
        except Exception as e:
            logger.error("配置校验失败: %s", e)
            sys.exit(1)
        logger.info("=== 完成 ===")

    elif args.command == "article":
        result = get_orchestrator().process_user_article(args.text, args.source)
        if result:
            logger.info("观点处理完成: %s", result.get("insight_id"))
            print(f"提取到 {len(result.get('judgments', []))} 条判断")
        else:
            logger.warning("未提取到有效判断")

    elif args.command == "run":
        try:
            get_orchestrator().run(args.phase)
        except Exception as e:
            logger.error("调度异常: %s", e, exc_info=True)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
