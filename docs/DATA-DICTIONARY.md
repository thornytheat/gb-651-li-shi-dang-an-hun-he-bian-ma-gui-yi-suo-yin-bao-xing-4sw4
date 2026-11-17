# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |
| `library_id` | string | 否 | 接收馆标识，由登录身份确定，不取自请求体 |
| `grant_version` | integer | 否 | 授权版本，每次授权变更递增 |
| `delivery_id` | string | 否 | 由馆别与幂等键派生的稳定交付编号 |
| `manifest` | object | 否 | 交付时冻结的档案清单与授权快照 |
| `manifest_digest` | string | 否 | 冻结清单的规范化摘要 |
| `ticket_id` | string | 否 | 单次下载凭据编号 |
| `ticket_status` | string | 否 | `active` / `used` / `revoked` / `expired` |
| `downloads` | array | 是 | 已完成的下载记录，不随授权收回删除 |
| `audit_hash` | string | 否 | 审计记录哈希，链式覆盖前一条 |

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。

馆际共享相关时间（授权生效、交付、凭据签发、下载、审计）一律存 UTC ISO-8601（`Z` 结尾）；
授权变更与下载的先后以审计链中的提交顺序为准。
