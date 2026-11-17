# 深度登记 历史档案混合编码归一与索引保形

        - 规模：中型
        - 核心规则：候选解码必须完整且无替换字符；档号、盒号与超大流水号始终按字符串逐字保留。
        - 使用者与工作流：导入随仓样例，逐模块复核，最后导出单一证据清单。

        ## 功能模块

        | 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
        |---|---|---|---|---|
        | M1 文件采信 | 运行样例并复核证据 | `/api/intake/run` / `/intake` | `app/modules/m1_intake/core.py` `app/modules/m1_intake/service.py` | `tests/test_m1_intake.py` |
| M2 编码判定 | 运行样例并复核证据 | `/api/encoding/detect` / `/encoding` | `app/modules/m2_encoding/core.py` `app/modules/m2_encoding/service.py` | `tests/test_m2_encoding.py` |
| M3 字段保形 | 运行样例并复核证据 | `/api/fields/normalize` / `/fields` | `app/modules/m3_fields/core.py` `app/modules/m3_fields/service.py` | `tests/test_m3_fields.py` |
| M4 内容归一 | 运行样例并复核证据 | `/api/content/normalize` / `/content` | `app/modules/m4_content/core.py` `app/modules/m4_content/service.py` | `tests/test_m4_content.py` |
| M5 索引对账 | 运行样例并复核证据 | `/api/reconcile` / `/reconcile` | `app/modules/m5_reconcile/core.py` `app/modules/m5_reconcile/service.py` | `tests/test_m5_reconcile.py` |
| M6 批次导出 | 运行样例并复核证据 | `/api/export/evidence.csv` / `/runs` | `app/modules/m6_runs/core.py` `app/modules/m6_runs/service.py` | `tests/test_m6_runs.py` |
| M7 馆际共享 | 登录后按馆授权、交付、收回、下载与审计复现 | `/api/grants/*` `/api/deliveries/*` `/api/audit` / `/batch` | `app/modules/m7_sharing/service.py` `app/modules/m7_sharing/store.py` `app/auth.py` `app/archive.py` | `tests/test_m7_sharing.py` |

        ## 陷阱登记

        | TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
        |---|---|---|---|---|---|
        | TRAP-01 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_intake/core.py` | `tests/test_m1_intake.py::test_trap_1` |
| TRAP-02 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_encoding/core.py` | `tests/test_m2_encoding.py::test_trap_1` |
| TRAP-03 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_fields/core.py` | `tests/test_m3_fields.py::test_trap_1` |
| TRAP-04 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_content/core.py` | `tests/test_m4_content.py::test_trap_1` |
| TRAP-05 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m5_reconcile/core.py` | `tests/test_m5_reconcile.py::test_trap_1` |
| TRAP-07 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m6_runs/core.py` | `tests/test_m6_runs.py::test_trap_1` |
| TRAP-08 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_intake/core.py` | `tests/test_m1_intake.py::test_trap_2` |
| TRAP-09 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_encoding/core.py` | `tests/test_m2_encoding.py::test_trap_2` |
| TRAP-16 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_fields/core.py` | `tests/test_m3_fields.py::test_trap_2` |
| TRAP-18 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_content/core.py` | `tests/test_m4_content.py::test_trap_2` |
| TRAP-19 | 审计员需要按原授权复现，但收回后旧凭据也应失效 | 固定权限越权，按现权限重算抹掉原交付依据 | 交付时冻结 manifest 与授权快照；下载/恢复一律按现行权限判定 | `app/modules/m7_sharing/service.py` | `tests/test_m7_sharing.py::test_reproduction_uses_original_authorization_not_current` `tests/test_m7_sharing.py::test_revoke_voids_old_ticket_but_keeps_downloaded_copy` |
| TRAP-20 | 伪造身份或换馆请求 | 采信请求体里的 `library_id` | 身份由签名会话确认，馆员固定本馆，跨馆即 403 | `app/auth.py` `app/modules/m7_sharing/handler.py` | `tests/test_m7_sharing.py::test_library_identity_cannot_be_switched_by_request` `tests/test_m7_sharing.py::test_login_required_and_forged_session_rejected` |
| TRAP-21 | 权限变更与下载并发 | 读到半新半旧状态 | 写事务串行提交，审计链顺序即生效边界 | `app/modules/m7_sharing/store.py` | `tests/test_m7_sharing.py::test_concurrent_revoke_and_download_have_one_boundary` |
| TRAP-22 | 排队交付中断后恢复 | 恢复时沿用旧权限、重试重复记账 | 恢复与落账前重验权限；按 `delivery_id` 唯一记账 | `app/modules/m7_sharing/service.py` | `tests/test_m7_sharing.py::test_interrupt_then_resume_revalidates_and_records_once` |
| TRAP-23 | 收回影响其他获授权馆与原档案 | 收回时连带清理共享档案 | 授权按馆隔离，原档案、档号与行号保持原样 | `app/archive.py` `app/modules/m7_sharing/service.py` | `tests/test_m7_sharing.py::test_other_library_keeps_its_access_after_revoke` `tests/test_m7_sharing.py::test_actual_archive_str_ids_and_source_lines` |

        ## 对账规则

        - 输入记录数 = 有效记录数 + 隔离记录数 + 空记录数。
        - 核心数值或事件数在转换、判定和导出之间保持守恒。
        - 同一输入摘要重跑复用原运行编号，不增加结果行。

        ## 已知不覆盖

        - 生产系统连接与外部密钥不在演示范围内；身份认证为单服务签名会话，未接外部 IdP。
