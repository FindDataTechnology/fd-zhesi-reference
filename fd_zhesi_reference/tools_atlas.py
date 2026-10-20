"""Atlas 种子图谱工具（task 2.2 接线）。数据 CC BY 4.0，attribution 随行。"""

from __future__ import annotations

from fastmcp import FastMCP

from .config import Settings
from .errors import HonestError


async def philosopher_query(name: str) -> dict:
    """人物查询：生卒/学派/时代/六轴坐标（Atlas 种子图谱，CC BY 4.0 署名随行）。"""
    raise HonestError("philosopher_query", "2.2")


async def relation_query(source: str, target: str) -> dict:
    """关系查询：两人关系边（极性 positive/critical/synthetic 与权重）。"""
    raise HonestError("relation_query", "2.2")


def register(mcp: FastMCP, settings: Settings) -> None:
    mcp.tool(philosopher_query)
    mcp.tool(relation_query)
