# Destination rules

Resolve destinations only from `.kb/mappings/feishu_nodes.json`.

| Enterprise category | Feishu parent |
|---|---|
| 使命愿景价值观、产品知识 | 01｜业务与产品 |
| 成交案例、客户知识、获客、营销、销售方法 | 02｜客户与增长 |
| 业务流程、SOP、培训流程 | 03｜流程与交付 |
| 财务、法务、税务、经营决策、管理复盘 | 04｜经营与合规 |

Never infer or synthesize a token. A parent mapping must be present and `verified: true`.

If the category cannot be resolved with high confidence, do not publish or add the note to a remote review node. Keep it local, request the smallest necessary clarification, and require a new preview after the destination is confirmed.

`00｜知识库首页`, `91｜数据索引`, and `98｜同步记录` are maintenance destinations, not ordinary publication targets.

Do not route ordinary publications into a permanent `99` directory. Create a controlled historical area only when real deprecated canonical knowledge must remain accessible and the user explicitly approves that lifecycle.
