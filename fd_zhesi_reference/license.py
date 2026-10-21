"""许可门与许可红线守卫（zhesi-atlas-resilience 1.3 / 6.1）。

两道门分开判定（design 决策 1）：
- **许可门**（本模块）：静态事实——站方条款是否允许入库。未确认即不摄取。
- **可得性门**（probe.py）：动态事实——此刻能不能取到。

红线（docs/data-license.md「许可红线」）在代码里落成 `FORBIDDEN_HOSTS` +
`assert_allowed_url()`：任何出站请求目标命中即拒。允许出现的只有引用文本
（`provenance.CITATION_URLS`），即「可以链接，不可以抓取」。

红线的理由：
- InPhO：CC BY-NC-SA，禁止商用 → 不引入（本产品是商业服务）
- DBpedia：CC BY-SA，传染性许可会污染整体分发 → 不引入
- Kialo：站点条款禁止抓取 → 不引入
- PhilPapers 直连：Cloudflare 拦截 + 站点条款 → 论文元数据改走 OpenAlex 源
"""

from __future__ import annotations

from urllib.parse import urlparse

# 许可门的事实记录（出处见 docs/data-license.md）
LICENSE_REGISTRY: dict[str, dict] = {
    "philosophy-atlas": {
        "status": "confirmed",
        "license": "CC BY 4.0",
        "commercial_use": True,
        "constraints": ["结果 attribution 必须署名 MBGuedes 与源链接"],
        "evidence": "whitepaper v2.1 页脚：Dados abertos sob licença CC BY 4.0",
    },
    "sep": {
        "status": "confirmed",
        "license": "站点条款（no-bulk-copy）",
        "commercial_use": True,
        "constraints": ["只存标题、URL 与短引用；不复制条目正文或摘要"],
        "evidence": "design D2 引用层只链不抄",
    },
    "wikidata": {
        "status": "confirmed",
        "license": "CC0",
        "commercial_use": True,
        "constraints": [],
        "evidence": "https://www.wikidata.org/wiki/Wikidata:Licensing",
    },
    "philpapers-survey": {
        "status": "confirmed",
        "license": "站点内容（低频快照引用）",
        "commercial_use": True,
        "constraints": ["带轮次年份标注；不做全文镜像"],
        "evidence": "docs/survey-probe.md 口径记录",
    },
    "philpapers-openalex": {
        "status": "confirmed",
        "license": "CC0（OpenAlex）",
        "commercial_use": True,
        "constraints": ["署名 OpenAlex 与 PhilPapers 源 S4306402130"],
        "evidence": "OpenAlex 数据全部 CC0；PhilPapers 源 ID S4306402130",
    },
}

# 红线：禁止作为依赖或请求目标的源（只能作为引用文本出现）
REDLINES: dict[str, dict] = {
    "inpho": {
        "hosts": ("inpho.indiana.edu", "www.inpho.indiana.edu", "inphoproject.org"),
        "license": "CC BY-NC-SA",
        "reason": "非商用许可，与本产品商业分发冲突",
    },
    "dbpedia": {
        "hosts": ("dbpedia.org", "www.dbpedia.org", "live.dbpedia.org"),
        "license": "CC BY-SA",
        "reason": "传染性许可会污染整体分发",
    },
    "kialo": {
        "hosts": ("kialo.com", "www.kialo.com", "kialo-edu.com"),
        "license": "站点条款",
        "reason": "站点条款禁止抓取；论证结构自建（Argdown）",
    },
    "philpapers-direct": {
        "hosts": ("philpapers.org", "www.philpapers.org", "survey2020.philpeople.org"),
        "license": "站点条款 + Cloudflare 拦截",
        "reason": "直连被拦且条款不明；论文元数据改走 OpenAlex 源 S4306402130",
    },
}

FORBIDDEN_HOSTS: frozenset[str] = frozenset(
    host for entry in REDLINES.values() for host in entry["hosts"]
)


class LicenseNotConfirmed(RuntimeError):
    """许可门未通过：该源不得入库。"""

    def __init__(self, source: str, reason: str = ""):
        entry = LICENSE_REGISTRY.get(source)
        detail = reason or (
            "许可未确认（docs/data-license.md 无允许判定）"
            if entry is None
            else f"状态={entry['status']}"
        )
        super().__init__(f"许可门未通过：{source} 不摄取（{detail}）")
        self.source = source
        self.reason = detail


class ForbiddenSource(RuntimeError):
    """出站目标命中许可红线。"""

    def __init__(self, url: str, redline: str, reason: str):
        super().__init__(f"许可红线：{url} 属于 {redline}（{reason}），禁止请求")
        self.url = url
        self.redline = redline


def license_entry(source: str) -> dict | None:
    return LICENSE_REGISTRY.get(source)


def license_confirmed(source: str) -> bool:
    entry = LICENSE_REGISTRY.get(source)
    return bool(entry and entry["status"] == "confirmed")


def require_license(source: str) -> dict:
    """许可门：确认允许才返回条目，否则抛 LicenseNotConfirmed。"""
    entry = LICENSE_REGISTRY.get(source)
    if not entry or entry["status"] != "confirmed":
        raise LicenseNotConfirmed(source)
    return entry


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:  # 畸形 URL 也按不可信处理
        return ""


def redline_for(host: str) -> tuple[str, dict] | None:
    """命中红线的源名与条目；未命中 None。子域按后缀匹配。"""
    h = host.lower()
    for name, entry in REDLINES.items():
        for forbidden in entry["hosts"]:
            if h == forbidden or h.endswith("." + forbidden):
                return name, entry
    return None


def assert_allowed_url(url: str) -> str:
    """出站请求前的守卫：命中红线直接抛错（不静默降级）。"""
    hit = redline_for(host_of(url))
    if hit:
        name, entry = hit
        raise ForbiddenSource(url, name, entry["reason"])
    return url


def redline_report() -> list[dict]:
    """红线清单（供文档/自检与测试读取，避免文档与代码漂移）。"""
    return [
        {
            "source": name,
            "hosts": list(entry["hosts"]),
            "license": entry["license"],
            "reason": entry["reason"],
            "allowed_as": "引用文本（attribution / 链接），不得作为请求目标",
        }
        for name, entry in REDLINES.items()
    ]
