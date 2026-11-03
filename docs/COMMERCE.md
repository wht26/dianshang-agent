# 统一业务接口（v0.9）

当前先完成统一接口，没有淘宝/抖店应用授权。默认无订单、无库存，不自动生成演示数据。所有金额为人民币整数分。

## 能力与数据来源

| 能力 | 现在可运行 | 外部接入后 |
| --- | --- | --- |
| 订单 | 商家导入已核验已支付订单，买家凭专属凭证查询 | 网关同步淘宝/抖店状态 |
| 库存 | 按颜色/尺码SKU登记、版本校验、事务扣减、防超卖 | 网关读取对应平台SKU库存 |
| 物流 | 自建订单录入承运商、运单、事件，保存最近50节点 | 平台网关返回真实物流；尚未直接连接快递公司 |
| 退款 | 买家申请、金额占用、商家审核、执行、结果核验和审计 | HTTPS网关调用实际支付/平台退款服务 |

`native`是本应用的自建商城业务台账，不是通用支付系统。导入时登记的是商家已经核验支付成功的订单，不会收款或创建支付交易。淘宝/抖店订单导入不扣减本地库存，以免与平台重复扣减。本地退款状态成功也不自动增加库存，只有实物退回验收后由商家调整库存。

## 本机启用商家权限

在PowerShell生成管理员令牌，仅放用户环境变量，不要提交或截图：

```powershell
$env:WANG_ADMIN_TOKEN = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
[Environment]::SetEnvironmentVariable('WANG_ADMIN_TOKEN', $env:WANG_ADMIN_TOKEN, 'User')
powershell -File start.ps1
```

已有令牌时直接启动，不要重复生成以免使旧客户端失效。`start.ps1`读取用户级环境变量；直接运行uvicorn时需在当前进程中设置。商家所有接口都要求 `Authorization: Bearer <令牌>`，写接口同时要求 `X-Wang-Client: web`。不配置至少32字符令牌时商家接口返回503。本机 `/docs` 提供不依赖外部CDN的调试页：填写令牌、选择接口、查看字段、填写JSON并确认后发送，页面自动设置客户端头。支持查看 `/openapi.json` 结构。

当前管理知识库和商品导入仍限本机使用，未统一纳入商家鉴权。本版不应直接暴露公网。订单访问凭证是持有即授权的秘密，不是账号身份；只交给对应买家。DB保存摘要，首次创建返回明文一次，重发会使旧凭证立即失效。Cookie有效期1天，但凭证本身在重发前仍有效；没有买家账号、短信核验或自动过期机制。

## 接入顺序

1. 导入真实商品目录。对每个颜色/尺码SKU建立`product_id`映射。
2. 商家调用库存调整接口，初始版本`expected_version=0`、`delta`为可用库存；之后先读版本再提交增减，版本冲突409后重读。
3. 将已支付订单导入，包含真实`paid_cents`和支付参考号。商品单价不是退款上限，退款以实付金额为限；所有商品明细都由商家校验。`status`必填；自建订单只支持初始待发货，平台订单必须填写同步到的真实状态。
4. 将首次返回的`access_token`通过原订单的可信渠道发给对应买家。不要放进网址、聊天内容或公共日志。首次响应丢失可用管理员重发接口，重复导入409不会重复扣库存。
5. 买家在“订单与售后服务”关联凭证，可查询、刷新或明确提交退款申请。Chat只能读裁剪后的状态，不能执行写操作。库存问答需选择映射对应的商品。
6. 商家查订单列表、核验诉求、审核退款，再调用执行接口。配置真实渠道前，执行会报错并保持待执行，不生成假退款成功。
7. 渠道处理中或结果不明时，只调用核验接口。退款成功后仍应通过原支付渠道核实到账情况。

商品导入与业务台账分开，替换店铺目录不会迁移业务映射；请勿把多家店铺混放在同一个实例。本版只有单店业务库，没有多商户租户隔离。

## 应用API

| 方法与路径 | 权限与用途 |
| --- | --- |
| GET `/api/admin/commerce/channels` | 商家查看渠道配置标记；不代表平台授权已验证 |
| GET `/api/admin/commerce/orders` | 商家最近100订单及退款状态，不返回明文凭证 |
| POST `/api/admin/commerce/inventory` | 商家调整库存，body含sku/product_id/delta/expected_version |
| POST `/api/admin/commerce/orders` | 商家导入已支付订单，事务扣减自建库存 |
| POST `/api/admin/commerce/orders/{id}/access` | 商家重发订单凭证，使旧凭证失效 |
| POST `/api/admin/commerce/orders/{id}/shipment` | 商家录入自建物流；禁止手工改写平台物流 |
| GET `/api/admin/commerce/audit` | 商家最近100操作记录 |
| POST `/api/admin/commerce/refunds/{id}/decision` | 商家审批，body为approved布尔值 |
| POST `/api/admin/commerce/refunds/{id}/execute` | 商家执行已审核退款；不是LLM工具 |
| POST `/api/admin/commerce/refunds/{id}/reconcile` | 商家查询结果不明或渠道处理中退款 |
| POST `/api/commerce/access` | 买家用access_token设置HttpOnly订单Cookie |
| POST `/api/commerce/access/clear` | 清除当前Cookie；不撤销已发出的凭证或旧聊天 |
| GET `/api/commerce/order` | 当前凭证对应订单、物流和退款 |
| POST `/api/commerce/order/refresh` | 自建读取本地记录，平台调用网关刷新 |
| GET `/api/commerce/inventory/{source}/{sku}` | 自建读台账；淘宝/抖店通过网关读取 |
| POST `/api/commerce/refunds` | 当前买家申请退款，含request_id/amount_cents/reason |

导入订单body字段：`request_id`、`source`、`status`、`external_id`、`payment_reference`、`paid_cents`、`items`；每个item含`sku`、`name`、`quantity`、`unit_cents`。详见 [字段Schema](commerce.schema.json)。只有已核验付款订单可导入，不支持未支付订单生命周期、取消还库或支付回调验签。

物流body字段：`status`、`carrier`、`tracking_number`、`event`。状态为`awaiting_shipment/shipped/delivered/closed`；发货/签收需要承运商及运单号，状态不允许倒退。这里登记物流事件，不会自动叫快递或购买面单。物流事件不要填写买家地址、联系方式。

## 自建/淘宝/抖店HTTPS网关协议

网关是商家自己部署的适配服务，本项目提供客户端和协议，不包含官方签名SDK、OAuth回调/令牌刷新或快递公司适配服务。平台官方URL不能直接配置到这里。分别设置用户级环境变量：

- `WANG_NATIVE_GATEWAY_URL`、`WANG_NATIVE_GATEWAY_TOKEN`：自建支付退款网关。
- `WANG_TAOBAO_GATEWAY_URL`、`WANG_TAOBAO_GATEWAY_TOKEN`：淘宝商家适配服务。
- `WANG_DOUYIN_GATEWAY_URL`、`WANG_DOUYIN_GATEWAY_TOKEN`：抖店商家适配服务。

URL必须HTTPS，不含用户名、查询参数或片段。请求携带Bearer令牌，20秒超时，禁止重定向。TOKEN是适配服务令牌，不是直接把平台AppSecret混作Bearer。适配服务应校验订单归属、支付金额和业务资格，再调用对应平台获授权的官方能力。

| 网关方法与相对路径 | 请求/响应契约 |
| --- | --- |
| GET `/orders/{external_id}` | 返回external_id、status、carrier、tracking_number、event，状态枚举同上 |
| GET `/inventory/{sku}` | 返回sku、available非负整数；sku应映射平台颜色/尺码SKU |
| POST `/refunds` | 请求refund_id、external_order_id、payment_reference、amount_cents、currency=CNY；头Idempotency-Key=refund_id |
| GET `/refunds/{refund_id}` | 查询同一退款；响应契约与POST相同 |

退款响应必须为JSON：`refund_id`、`amount_cents`、`currency`、`status`、`receipt`。状态仅`submitted/succeeded/failed`，receipt为非空渠道回执。必须匹配请求金额和refund_id；HTTP200本身不代表退款成功。receipt不要含支付账号或其他隐私。

网关必须持久化幂等键到平台退款/售后单号的映射，即使进程重启也不重复出款；POST重复应返回同一操作。GET必须查询实际渠道结果；操作尚在执行返回submitted，不得把暂时未查到直接当failed。淘宝/抖店中“审核售后申请”不等于已经退款成功：需要关联实际售后单，核验当前售后阶段，最终确认渠道退款状态后才返回succeeded。没有实际支付接入时不得实现一个总返回succeeded的替身。

应用退款状态：

`requested → approved → executing → submitted/succeeded/failed`

未通过审核为rejected；网关超时、非法回执或异常结果为uncertain。executing（例如进程中断）、submitted、uncertain可调用reconcile，只查不重复出款。缺少网关配置时执行失败会恢复approved。金额占用包含待审核、待执行、执行中、渠道处理中、待核验和成功退款；失败或拒绝释放额度。最大退款金额受已登记实付减去占用约束。系统不自动认定买家退款资格或退货验收结果。

## 官方平台后续联调参考

实际接入需要应用权限与商家授权，还要核对官方当前字段和可操作阶段：

- [淘宝订单与物流场景说明](https://developer.alibaba.com/docs/doc.htm?articleId=121170&docType=1&treeId=780)
- [淘宝退款API文档](https://developer.alibaba.com/docs/api.htm?apiId=22465&scopeId=11527)
- [抖店调用与签名规范](https://op.jinritemai.com/docs/guide-docs/10/23)
- [抖店售后统一操作说明](https://op.jinritemai.com/docs/g/197/2752?from=list)

这些资料用于确定适配方向，不表示本项目已经实现或验证对应官方接口。

## 验证范围

使用临时数据库验证权限、库存事务并发、幂等、退款并发金额占用、审批、渠道执行契约、异常回执、超时核验、持久化和模型只读上下文。HTTP传输使用MockTransport验证；没有创建真实平台订单、调用真实支付退款或真实快递服务。正式外部联调需先使用平台允许的测试环境和测试订单。
