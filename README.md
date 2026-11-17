# 历史档案混合编码归一与索引保形

        面向一线复核人员的单服务数据工作台。样例数据随仓库提供，结论保留行号、原因码和原值。

        ## 业务模块

        - M1 文件采信：`/api/intake/run`
- M2 编码判定：`/api/encoding/detect`
- M3 字段保形：`/api/fields/normalize`
- M4 内容归一：`/api/content/normalize`
- M5 索引对账：`/api/reconcile`
- M6 批次导出：`/api/export/evidence.csv`
- M7 馆际共享：`/batch`（授权、交付、收回、下载凭据、审计复现）

        ## 核心规则

        候选解码必须完整且无替换字符；档号、盒号与超大流水号始终按字符串逐字保留。

        ## 馆际共享（M7）

        批次页接入实际档案（`ARCHIVE_PATH` 指向的 CSV，默认 `sample_data/archive_dirty.csv`），
        档号、盒号与行号原样保留。共享流程：

        1. **登录**：`POST /api/auth/login` 签发 HMAC 签名会话；身份与所属馆由服务端配置
           派生，伪造、过期或改馆的令牌一律 401/403，不会按请求体里的 `library_id` 换馆。
        2. **按馆授权**：授权员 `PUT /api/grants/{library_id}`，范围限定到具体档号；每次
           变更递增 `version` 并写入授权事件。
        3. **交付**：接收馆 `POST /api/deliveries`（必须带幂等键）。首次创建时把原授权
           版本、授权快照与档案清单冻结为 manifest 并计算摘要；重复幂等键复用同一交付。
        4. **收回**：`POST /api/grants/{library_id}/revoke`。未使用的下载凭据立即作废；
           已下载副本不追缴。
        5. **审计**：`GET /api/audit`、`GET /api/audit/verify`（哈希链）、
           `GET /api/deliveries/{id}/reproduce`（按原授权复现清单并校验摘要）。

        并发与恢复边界：

        - 授权读取、范围校验、清单冻结、交付记账、凭据核销都在同一个 SQLite 写事务里
          串行提交，审计链的提交顺序即生效边界。
        - 排队交付在开始执行与完成落账前各重验一次现行权限；中断恢复同样重验。权限已
          收回或范围已收缩时交付转 `failed` 并留否决审计。
        - 交付记账按 `delivery_id` 唯一，重试与重放不会重复记账。

        ## 启动

        ```bash
        cp .env.example .env   # 设置 AUTH_SECRET
        docker compose up --build
        ```

        浏览器访问 `http://localhost:18210`，先登录再进入 `/batch`。运行测试：

        ```bash
        docker compose exec -T app pytest -q
        ```

        演示账户（默认配置，生产请通过 `AUTH_USERS` 覆盖）：授权员 `curator`、审计员
        `auditor`、甲馆 `lib-alpha`、乙馆 `lib-beta`。

        ## 样例流程

        依次运行六个模块；最后在批次页复核输入、有效、隔离与输出合计。同一输入摘要重复执行时复用原 `run_id`。

        ## 已知不覆盖

        不处理加密文件与需要外部密钥的对象；不连接真实生产存储；馆际交付为同库内队列，
        未接入跨馆消息中间件。
