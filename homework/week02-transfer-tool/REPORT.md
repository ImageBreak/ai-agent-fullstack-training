# 第二周作业报告：受治理的转账工具

## 1. 作业目标

本作业在统一的 `ToolRuntime` 中注册高风险写工具 `transfer`，并贯通以下治理链路：

```text
参数校验
  → 固定优先级权限判断
  → 无副作用业务预检
  → 参数绑定的一次性人工审批
  → 受超时和重试策略保护的业务执行
  → 返回结果脱敏
  → 决策与执行审计
```

所有转账调用都通过 `ToolRuntime.invoke()`，测试不直接调用 `transfer_handler()`。

## 2. 可复现环境

项目使用 `uv` 管理 Python、依赖和锁文件：

- Python：3.14.3（最低支持版本为 3.11）
- Pydantic：2.13.5
- pytest：9.1.1
- 依赖声明：`pyproject.toml`
- 完整依赖锁定：`uv.lock`
- Python 版本提示：`.python-version`

在另一台已安装 `uv` 的计算机上执行：

```bash
cd homework/week02-transfer-tool
uv sync --locked
uv run --locked python -m pytest test_tool_governance.py -v -k "transfer"
```

`uv sync --locked` 只接受已经提交的锁文件；如果 `pyproject.toml` 与 `uv.lock` 不一致，命令会失败，而不会静默更新依赖。

当前仓库中的测试文件位于作业目录根部，因此测试路径是 `test_tool_governance.py`，不是 `tests/test_tool_governance.py`。

## 3. 核心设计

### 3.1 严格参数模型

```python
class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TransferArgs(StrictArgs):
    from_account: str = Field(pattern=r"^ACC-[A-Z]-[0-9]{6}$")
    to_account: str = Field(pattern=r"^ACC-[A-Z]-[0-9]{6}$")
    amount: float = Field(gt=0, le=100_000)
```

`extra="forbid"` 阻止模型通过 `approved=True`、`user_id="admin"` 等额外字段注入身份或审批信息。账号格式、正金额和 Schema 金额上限由 Pydantic 在进入权限引擎之前验证。

### 3.2 转账治理策略

```python
ToolPolicy(
    effect=Effect.WRITE,
    risk=Risk.HIGH,
    permission="transfer:execute",
    requires_approval=True,
    timeout_seconds=2.0,
    max_retries=0,
    idempotent=False,
)
```

该策略表达了以下约束：

- 转账是写操作，因此在 `PLAN` 模式下禁止执行；
- 转账属于高风险操作，必须人工审批；
- 调用者必须拥有 `transfer:execute` 权限；
- 单次执行最多等待两秒；
- 非幂等写操作不得自动重试；
- 非幂等写超时返回 `TIMEOUT_UNKNOWN`。

### 3.3 固定优先级权限状态机

`PermissionEngine.decide()` 保持框架原有顺序：

1. 显式 deny 规则；
2. `PLAN` 模式写保护；
3. 本轮执行白名单；
4. RBAC 业务权限；
5. 业务预检；
6. 高风险人工审批；
7. bypass 模式；
8. allow 规则；
9. Shell 风险判断；
10. 默认允许。

转账在第 6 步必须取得有效审批，因此 bypass 和 allow 规则不能越过前面的硬边界。

### 3.4 无副作用业务预检

```python
async def transfer_precheck(raw_arguments, context):
    arguments = raw_arguments
    assert isinstance(arguments, TransferArgs)

    if 50_000 < arguments.amount <= 80_000:
        raise PolicyDenied("EXCEED_LIMIT", "转账金额处于教学拦截区间 (50000, 80000]")

    balance = ACCOUNTS.get((context.tenant_id, arguments.from_account))
    if balance is None or balance < arguments.amount:
        raise PolicyDenied("INSUFFICIENT_BALANCE", "转出账户余额不足")
```

预检只读取状态，不修改余额。业务拒绝发生在审批消费和 handler 执行之前。

### 3.5 参数绑定的一次性审批

审批摘要由工具名和规范化后的完整参数计算：

```python
canonical = json.dumps(_stable_value(arguments), ensure_ascii=False, separators=(",", ":"))
digest = hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()
```

审批消费时同时验证：

- 审批存在且未使用；
- 审批尚未过期；
- 用户一致；
- 租户一致；
- 工具名一致；
- 完整参数摘要一致。

金额从 1200 元改为 9000 元后，原审批不匹配，runtime 返回 `CONFIRM / APPROVAL_REQUIRED`。审批成功消费后标记为 `used=True`，不能重放。

### 3.6 副作用与超时

只有权限决策为 `ALLOW` 时，runtime 才会在 `asyncio.timeout()` 中调用 handler。转账 handler 执行扣款、入账并返回交易结果。

教学用超时分支在金额大于 80000 元时等待三秒，而策略超时是两秒。由于转账是非幂等写操作，runtime 不自动重试，并返回 `TIMEOUT_UNKNOWN`。

本教学实现把等待放在余额修改之前，因此测试可以验证余额没有变化。生产系统不能仅根据客户端超时推断转账失败，而应使用幂等交易号、状态查询和对账机制确认最终状态。

### 3.7 结果脱敏

handler 返回原始结果后，runtime 统一调用 `_redact()`：

```python
safe_content = _redact(dict(raw))
```

账号规则保留账户前缀和最后四位：

```text
ACC-A-123456 → ACC-A-****3456
ACC-A-654321 → ACC-A-****4321
```

同时，名称匹配 `token`、`secret`、`password`、`authorization` 的字段值会变为 `***`，邮箱会变为 `***@***`。

### 3.8 审计追踪

权限判断后写入 `decision` 审计；handler 成功、失败或超时后写入 `execution` 审计。

成功转账的审计顺序为：

```text
decision  / APPROVED
execution / OK
```

未经审批的转账只有：

```text
decision / APPROVAL_REQUIRED
```

当前 `AuditRecord` 只保存参数键名，不保存参数值和结果内容。因此完整账号不会进入审计记录；脱敏账号位于最终 `ToolResult.content` 中。如果验收要求审计记录本身展示脱敏账号，需要另行增加只接收脱敏结果的审计字段。

## 4. 测试覆盖

| 测试 | 验证内容 | 结果 |
|---|---|---|
| `test_transfer_rejects_injected_arguments_and_invalid_amount` | 额外参数注入、账号格式、零金额、Schema 上限 | PASSED |
| `test_transfer_precheck_blocks_over_limit_and_insufficient_balance` | 教学额度限制、余额不足、无副作用拒绝 | PASSED |
| `test_transfer_is_denied_without_permission_or_whitelist_and_in_plan_mode` | RBAC、执行白名单、PLAN 写保护 | PASSED |
| `test_transfer_requires_approval_bound_to_arguments_and_masks_accounts` | CONFIRM、参数绑定审批、一次性审批、账号脱敏、审计 | PASSED |
| `test_transfer_timeout_is_reported_as_unknown_and_leaves_balances_untouched` | 两秒超时、非幂等不重试、`TIMEOUT_UNKNOWN` | PASSED |

## 5. 验收结果

2026-09-18 在 Windows、CPython 3.14.3 上使用锁定环境执行：

```text
platform win32 -- Python 3.14.3, pytest-9.1.1, pluggy-1.6.0
collected 5 items
5 passed in 2.38s
```

测试命令：

```bash
uv run --locked python -m pytest test_tool_governance.py -v -k "transfer"
```

## 6. 硬性约束核对

- 5 个 transfer 测试全部通过；
- 未修改 `PermissionEngine.decide()`；
- 测试全部通过 `ToolRuntime.invoke()` 调用；
- `TransferArgs` 保留 `extra="forbid"`；
- 未审批调用返回 `CONFIRM / APPROVAL_REQUIRED`；
- 成功结果中的账号显示为 `ACC-A-****3456` 和 `ACC-A-****4321`；
- 决策和执行阶段均产生审计记录。

## 7. 教学实现与生产实现的边界

本作业用于理解治理框架，不是生产级资金系统。生产实现还需要：

- 使用 `Decimal` 或整数最小货币单位代替 `float`；
- 在数据库事务内重新检查账户状态和余额；
- 使用行锁、乐观锁或原子条件更新防止并发超扣；
- 为交易提供全局唯一幂等键；
- 将收款账户检查同时放入 precheck 和事务内执行检查；
- 建立交易状态查询、异步对账和人工处置流程；
- 使用持久化、不可篡改且访问受控的审计存储。
