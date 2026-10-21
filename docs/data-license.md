# 哲思参照系数据许可台账

> change: `zhesi-worldview-workshop` task 1.1。结论必须给出明确判定与出处；本表是摄取前置门的事实记录。

## 结论一览（2026-10-20 核验）

| 源 | 用途 | 许可 | 判定 | 署名/约束 |
|---|---|---|---|---|
| Philosophy Atlas | 种子图谱（376 人×六轴、804 关系边含极性、topics） | **CC BY 4.0**（whitepaper v2.1 明示 "Dados abertos sob licença CC BY 4.0"） | **允许（需署名）** | 结果 attribution 带 "Philosophy Atlas (MBGuedes), CC BY 4.0" 与源链接 |
| Stanford Encyclopedia of Philosophy | 概念引用层 | 站点条款限制批量复制 | **允许（只链不抄）** | 仅存标题+摘要+URL，不复制正文 |
| Wikidata | 全量层（人物/关系/时间线） | CC0 | **允许（无约束）** | 惯例仍带来源标注 |
| PhilPapers Survey | 立场分布 | 站点内容，Cloudflare 后 | **允许（低频快照引用，注明轮次年份）** | 2009/2020 轮数据带年份标注；不做全文镜像 |

## 核验出处

- Philosophy Atlas whitepaper v2.1（2026-06）：`https://philosophyatlas.com.br/web/whitepaper.html`
  - 关键句（页脚）："Dados abertos sob licença CC BY 4.0. Desenvolvido por MBGuedes"
  - 语料口径：376 filósofos / 804 conexões / 117 escolas；边类型三种（positiva/crítica/sintética）
  - 数据文件直取路径：`data/raw/philosophers.csv`、`data/raw/influences.csv`、`data/catalogue/topics/topics.json`
- SEP 许可立场：设计决策 D2（引用层只链不抄），源自信约通行的 no-bulk-copy 条款
- Wikidata：CC0（https://www.wikidata.org/wiki/Wikidata:Licensing）

## 影响与后续

- design.md D2 的「许可不允许可降级 Wikidata-only」分支**不触发**——Atlas 许可通过，按主路径摄取
- Atlas 白皮书还提到两份 working paper 已上 Zenodo 带 DOI（§9），如需引用数据集版本，摄取时在 provenance 里带 whitepaper 版本号（v2.1 / corpus v8.0）

## 双门摄取（change `zhesi-atlas-resilience` 1.3）

许可确认是**静态事实**，可得性是**动态事实**，两者分开判定、都过才摄取：

```
许可检查（license.require_license） → 可得性探测（probe.probe） → 快照落盘（snapshot.store） → 解析入库
```

- 许可未确认：该层不摄取，`source_status` 里 `reason=license_unconfirmed`
- 不可得：该层不摄取，`source_status` 里 `reason=unreachable|timeout|http_error|empty_content`
- 探测结果带 HTTP 状态、耗时、内容 sha256 与探测时间，落 `source_status` 表——
  这样「源挂了」与「解析器坏了」在台账上可分（前者探不通，后者探得通但解析失败）
- 快照不可变、内容寻址（`<root>/<dataset>/<sha256>/<文件名>`），清单登记 `snapshots` 表；
  每行数据的 `snapshot_version` 指向具体快照 id，参照系查询结果随 provenance 下发

### Atlas 可得性实测（2026-10-21）

| 项 | 结果 |
|---|---|
| DNS | `philosophyatlas.com.br` → 179.188.54.241 |
| TCP 443 / 80 | 均超时 |
| 经代理 | 返回 522（源站不可达） |
| 公开仓库 / Zenodo 记录 | 未找到 |
| 许可 | CC BY 4.0（whitepaper v2.1 页脚明示） |

结论：**许可通过、可得性无保障**。因此本 change 增加可得性门与降级路径，
不以「许可已确认」推定「数据一定在」。

## 许可红线（change `zhesi-atlas-resilience` 6.1）

代码守卫：`fd_zhesi_reference/license.py` 的 `FORBIDDEN_HOSTS` +
`assert_allowed_url()`。任何出站请求目标命中即抛 `ForbiddenSource`。
红线主机只允许作为**引用文本**出现在 `provenance.CITATION_URLS` / `ATTRIBUTIONS`。

| 源 | 许可 | 判定 | 理由 |
|---|---|---|---|
| InPhO（Indiana Philosophy Ontology） | CC BY-NC-SA | **禁止引入** | 非商用许可与本产品商业分发冲突 |
| DBpedia | CC BY-SA | **禁止引入** | 传染性许可会污染整体分发 |
| Kialo | 站点条款 | **禁止抓取** | 条款禁止抓取；论证结构自建（Argdown） |
| PhilPapers 直连 | 站点条款 + Cloudflare | **禁止直连** | 直连被拦且条款不明；论文元数据改走 OpenAlex 源 `S4306402130` |

替代路径（已接线）：PhilPapers **论文元数据**经 OpenAlex 的 PhilPapers 源获取
（source id `S4306402130`，OpenAlex 数据 CC0），工具 `philpapers_paper_search`。
**PhilPapers Survey 的立场分布**是另一层，单独确认来源、单独 provenance
（`scripts/ingest_survey.py` → `stances` 表 → `stance_distribution`），
不以论文元数据替代立场分布，两者互不兜底。

## SEP 存储边界（change `zhesi-atlas-resilience` 6.2）

`concepts` 表只存 **标题、URL 与短引用**：标题 ≤ 200 字符、片段 ≤ 200 字符，
写入时即截断（`tools_sep._short_quote`），因此存储面不会因解析器改动而膨胀成
条目正文/摘要的镜像。自检：`tools_sep.storage_policy_violations(conn)` 应为空。
