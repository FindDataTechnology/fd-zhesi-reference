"""PhilPapers Survey 立场分布工具（task 2.5 接线）。"""

from __future__ import annotations

from fastmcp import FastMCP

from .config import Settings
from .errors import HonestError


async def stance_distribution(question: str) -> dict:
    """立场分布：经典立场问题上职业哲学家的立场分布（带轮次年份）。"""
    raise HonestError("stance_distribution", "2.5")


def register(mcp: FastMCP, settings: Settings) -> None:
    mcp.tool(stance_distribution)
