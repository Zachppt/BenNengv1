# ============================================================
# run.py — 启动入口 v2
# 并发运行：扫描主循环 + Telegram 指令监听
# ============================================================

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scanner.log", encoding="utf-8"),
    ]
)

from main import main
from agent.analyzer import poll_commands


async def run():
    await asyncio.gather(
        main(),
        poll_commands(),
    )


if __name__ == "__main__":
    asyncio.run(run())
