# Week 01 · LLM Gateway

> 实现状态（2026-08-29）：源码、配置示例、依赖锁、测试套件和验证脚本均已完成。29 项本地测试与 6 项真实双模型测试全部通过，完整验收报告见 `reports/verification.json`。

## 启动与使用

要求 Python 3.12 和 uv。服务按单实例、单 worker 运行；命令从本作业目录执行。

### 1. 填写本地配置

已创建并忽略 Git 的 `.env`，请填写：

| 变量 | 用途 |
| --- | --- |
| `GATEWAY_API_KEY` | 自定义的长随机字符串，保护网关调用和管理接口 |
| `DEEPSEEK_PRO_API_KEY` | Pro 上游密钥 |
| `DEEPSEEK_FLASH_API_KEY` | Flash 上游密钥，可以和 Pro 使用同一把 DeepSeek Key |
| `DEEPSEEK_PRO_BASE_URL` / `DEEPSEEK_FLASH_BASE_URL` | 默认 https://api.deepseek.com，通常不需要修改 |

服务自动读取作业目录的 `.env`，进程环境变量优先。不要把真实 Key 发到聊天或写入已跟踪的 JSON/YAML 文件。克隆后的新目录若没有 `.env`，从 `.env.example` 复制一次再填写，勿覆盖已有密钥。

默认直接加载 `gateway.example.yaml`。需要调整模型速率、并发、超时或其他资源限制时，复制为本地 `gateway.yaml` 后修改；也可以通过 `GATEWAY_CONFIG` 指定配置文件。

### 2. 安装依赖并启动

依赖安装与测试已按下列方式验证。日常启动可直接执行：

```powershell
cd homework/week01-llm-gateway
uv sync --extra dev --locked
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

访问 `http://127.0.0.1:8000/docs` 查看交互式接口文档。`/healthz` 检查存活，`/readyz` 检查本地数据库，不调用模型。未填写密钥或保留占位值时，启动失败是预期行为。

### 3. curl 示例

以下为 PowerShell 示例，使用 `curl.exe` 避免 Windows PowerShell 的 curl 别名差异。先在终端将 `$gatewayKey` 设置为 `.env` 中相同的 Gateway Key；不要把赋值后的秘密保存到项目文件。Bash 可使用 `curl` 和 `$GATEWAY_API_KEY`。

```powershell
curl.exe http://127.0.0.1:8000/healthz
curl.exe http://127.0.0.1:8000/v1/models -H "Authorization: Bearer $gatewayKey"
curl.exe http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer $gatewayKey" -H "Content-Type: application/json" --data-binary "@examples/chat.json"
curl.exe -N http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer $gatewayKey" -H "Content-Type: application/json" --data-binary "@examples/stream.json"
curl.exe http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer $gatewayKey" -H "Content-Type: application/json" --data-binary "@examples/structured.json"
curl.exe http://127.0.0.1:8000/v1/prompts -H "Authorization: Bearer $gatewayKey" -H "Content-Type: application/json" --data-binary "@prompts/meeting-summary.json"
curl.exe http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer $gatewayKey" -H "Content-Type: application/json" --data-binary "@examples/prompt-call.json"
curl.exe "http://127.0.0.1:8000/admin/usage?limit=10" -H "Authorization: Bearer $gatewayKey"
curl.exe http://127.0.0.1:8000/admin/usage/summary -H "Authorization: Bearer $gatewayKey"
```

`examples/prompt-call.json` 引用版本 1；首次发布演示模板即创建版本 1，再次发布会创建新版本而不覆盖旧版。将示例中的 `model` 改为另一个公开别名即可比较两条协议路径。

### 4. 验证命令与结果

```powershell
uv run pytest -q -m "not live"
uv run python scripts/verify_all.py
uv run python scripts/verify_all.py --live
```

前两条只使用 Mock 与本地假上游，不需要真实 Key。第三条显式启用真实模型调用，可能计费，缺配置会失败。若直接使用 pytest 运行真实测试，命令为 `uv run pytest -q -m live --live`。

本次实际结果：

- `pytest -m "not live"`：29 passed，6 deselected。
- `ruff check app tests scripts`：通过。
- `scripts/verify_all.py --live`：35 passed，其中真实模型 6/6；路由、流式、结构化、模板、可观测性、韧性、限流和安全分组全部 PASS。

验证脚本将脱敏结果写到可提交的 `reports/verification.json`，报告不包含密钥或提示正文。默认运行仍会把真实模型标为 `NOT_RUN`；只有本地套件与 live 测试均通过才标记 `acceptance_complete: true`。不要用不带 `--live` 的结果覆盖最终真实验收报告。

### 实现边界

Jinja2 模板支持变量、下标、条件和非嵌套循环，过滤器限于 default/length/tojson；不允许属性访问、函数调用、算术/拼接表达式、嵌套或递归循环。结构化输出统一使用对象作为根，数组可以作为对象字段，保证两种上游路径具有同一可验证子集。

密钥、运行数据库、日志、缓存和临时报告不应提交；依赖锁文件 `uv.lock`、示例 JSON、`.env.example`、源码和脱敏的 `reports/verification.json` 可以提交。

## 1. 目标与范围

本作业实现一个统一的 LLM Gateway。调用方只需要面向一种稳定的请求与响应协议，而网关根据请求中的 `model` 动态选择对应的上游协议和模型：

| 网关模型别名 | 上游协议 | 适配器 |
| --- | --- | --- |
| `deepseek-v4-pro` | Chat Completions | `ChatCompletionsAdapter` |
| `deepseek-v4-flash` | Responses API | `ResponsesAdapter` |

首版范围包括：模型路由、流式 SSE、结构化输出、提示词模板版本、调用观测、统一错误、重试和按模型独立限流。

不在首版范围内：多租户权限、分布式限流、跨区域部署、工具调用、多模态、响应缓存、异步任务队列和模型自动降级。部署采用单实例、单 worker。

技术栈确定为 Python、FastAPI、httpx、SQLite（aiosqlite）和 Jinja2 Sandbox；配合 Pydantic 做接口校验、jsonschema 做本地输出校验、pytest / pytest-asyncio 做测试。

这是 Chat Completions **文本子集兼容**的网关，不承诺完整兼容全部上游字段。首版只接收 `system`、`user`、`assistant` 角色和字符串内容，单次生成一个结果；工具、多模态、服务端会话状态及未声明字段明确拒绝，不静默忽略。

首版默认显式关闭上游思考模式，只返回可见回答。启用思考模式必须通过受控配置；推理事件不作为回答正文、不触发 TTFT，仅在上游提供时统计 reasoning_tokens。

## 2. 总体架构

```text
Client
  │  POST /v1/chat/completions
  ▼
API Layer
  │  鉴权 · 请求校验 · Request ID · JSON/SSE 封装
  ▼
Gateway Orchestrator
  │  Prompt 渲染 · 模型路由 · 限流/并发准入
  │  共享调用预算 · 重试/修复 · 观测记录
  ├───────────────────────────────┐
  ▼                               ▼
ChatCompletionsAdapter       ResponsesAdapter
  │                               │
  ▼                               ▼
Chat Completions API          Responses API
  │                               │
  └─────────── Provider / Model ──┘
```

网关对外保持 Chat Completions 风格，`ResponsesAdapter` 负责完成协议转换。这样客户端不需要感知上游 API 的鉴权方式、请求体字段、SSE 事件名或返回对象结构。

## 3. 代码分层与目录结构

实现采用依赖方向单一的分层结构：API 层依赖服务层，服务层依赖领域模型、Repository 和 Adapter；Adapter 与 Repository 均不依赖 FastAPI 路由。

```text
week01-llm-gateway/
├── app/
│   ├── main.py                 # FastAPI 应用、生命周期、依赖组装
│   ├── config.py               # YAML / .env 读取、配置校验
│   ├── schemas.py              # 对外 Pydantic 请求与响应模型
│   ├── domain/
│   │   ├── models.py           # 规范化请求/响应、TextDelta/Usage/Finished 事件
│   │   ├── errors.py           # 框架无关的领域/上游错误
│   │   └── enums.py            # 调用状态、Adapter 类型等枚举
│   ├── api/
│   │   ├── deps.py             # 鉴权与服务依赖注入
│   │   ├── middleware.py       # 请求体边界、Request ID、ASGI 上下文
│   │   ├── errors.py           # 领域异常到 HTTP / SSE 错误的映射
│   │   ├── sse.py              # 对外 SSE 编码、响应预取与发送边界
│   │   └── routes.py           # HTTP 路由；仅参数转换与响应封装
│   ├── core/
│   │   ├── retry.py            # 退避策略、调用预算、可注入的等待函数
│   │   ├── security.py         # Bearer Key 校验、Key 指纹、Request ID
│   │   └── rate_limit.py       # 按模型的令牌桶与并发准入
│   ├── adapters/
│   │   ├── base.py             # Adapter 抽象协议
│   │   ├── chat_completions.py # Chat Completions 请求、响应与 SSE 映射
│   │   └── responses.py        # Responses API 请求、响应与 SSE 映射
│   ├── services/
│   │   ├── gateway.py          # 主编排：路由、重试、流式、调用记录
│   │   ├── router.py           # model alias 到 Adapter / 上游配置的解析
│   │   ├── prompts.py          # 版本解析、Sandbox Jinja2 渲染、消息插入
│   │   ├── structured.py       # JSON 解析、Schema 校验和修复消息构造；不直接调用模型
│   │   └── usage.py            # 延迟、TTFT、Token 和聚合查询
│   └── repositories/
│       ├── database.py         # aiosqlite 连接、初始化、事务管理
│       ├── prompts.py          # PromptTemplate / PromptVersion SQL 操作
│       └── invocations.py      # 调用记录的创建、回填与查询 SQL
├── tests/
│   ├── unit/                   # Adapter 映射、模板渲染、限流、Schema 校验
│   ├── integration/            # FastAPI + MockTransport 的应用集成测试
│   ├── streaming/              # 本地真实 HTTP 测试，验证分块到达与取消
│   ├── live/                   # 显式启用的真实模型冒烟测试
│   └── conftest.py             # 临时 SQLite、测试配置、Mock 上游夹具
├── scripts/
│   └── verify_all.py           # 复用测试套件，输出分类验收报告
├── prompts/                    # 脱敏演示模板；运行时以 SQLite 版本记录为准
├── examples/                   # 可直接传给 curl 的请求 JSON
├── gateway.example.yaml
├── .env.example
├── .gitignore
├── pyproject.toml
├── uv.lock
├── .python-version
└── README.md
```

各层的边界必须保持清晰：

| 层 | 可以做什么 | 不应做什么 |
| --- | --- | --- |
| `api` | 读取 HTTP、鉴权、调用服务、输出 JSON/SSE | 拼装上游 payload、编写 SQL、判断重试 |
| `services` | 编排业务规则、调用 Adapter/Repository | 解析 FastAPI Request、直接处理 HTTP 响应对象 |
| `adapters` | 协议字段转换、调用 httpx、解析上游事件 | 访问 SQLite、限流、决定公开模型路由 |
| `repositories` | SQLite CRUD 与事务 | 调用模型、模板渲染、HTTP 错误映射 |
| `core` | 可复用的横切规则 | 依赖具体上游协议 |

`main.py` 在应用启动时创建一个共享的 `httpx.AsyncClient`、初始化 SQLite 表结构和服务对象；关闭时释放 HTTP 连接池和数据库连接。路由通过 FastAPI 依赖注入取得这些服务，不在路由函数内临时创建客户端或数据库连接。

## 4. 对外 API 与调用契约

除健康检查和本地开发文档外，首版全部使用同一把 `GATEWAY_API_KEY` 鉴权：

```text
Authorization: Bearer <GATEWAY_API_KEY>
```

这降低了作业的权限实现复杂度。生产环境应将模型调用权限和模板/观测管理权限拆为不同的 Key 或角色。

### 调用面

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | 统一模型调用；支持非流式、SSE、结构化输出和模板引用 |
| `GET` | `/v1/models` | 返回网关公开的模型别名与能力 |

`/v1/models` 不暴露上游供应商、真实模型 ID、上游 URL 或密钥：

```json
{
  "data": [
    {
      "id": "deepseek-v4-pro",
      "object": "model",
      "capabilities": {
        "stream": true,
        "structured_output": true,
        "upstream_api": "chat_completions"
      }
    }
  ]
}
```

每次调用均由服务端生成唯一 `request_id`，通过 `X-Request-ID` 返回并关联日志、用量记录和流式错误。客户端的关联 ID 可校验后另存为 `client_request_id`，不能用它作为数据库主键，避免重复 ID 覆盖记录；首版不提供幂等请求语义。

### Prompt 管理面

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/prompts` | 创建模板或发布一个新版本 |
| `GET` | `/v1/prompts` | 查询模板列表和激活版本 |
| `GET` | `/v1/prompts/{id}` | 查询模板元数据和版本内容 |
| `POST` | `/v1/prompts/{id}/activate` | 激活指定的历史版本 |

发布示例：

```json
{
  "id": "meeting-summary",
  "name": "会议总结",
  "role": "user",
  "content": "请将以下内容总结为 {{ format }}：\n{{ transcript }}",
  "activate": true
}
```

### 观测与运维面

| 方法 | 路径 | 鉴权 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/admin/usage` | 是 | 查询调用明细，支持模型、状态、时间和流式筛选 |
| `GET` | `/admin/usage/summary` | 是 | 返回按模型、状态和流式类型聚合的统计 |
| `GET` | `/healthz` | 否 | 进程存活检查 |
| `GET` | `/readyz` | 否 | SQLite 和启用模型配置的就绪检查 |
| `GET` | `/docs` | 否 | FastAPI 自动生成的 OpenAPI 文档 |

`/readyz` 只检查本地数据库和配置完整性，不发起真实模型调用，也不会消耗 Token。


### 请求

首版公开接口为 `POST /v1/chat/completions`。

```json
{
  "model": "deepseek-v4-flash",
  "messages": [
    {"role": "system", "content": "你是一名严谨的项目助理。"}
  ],
  "stream": false,
  "temperature": 0.2,
  "max_tokens": 512,
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "meeting_summary",
      "schema": {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": false
      }
    }
  },
  "gateway_prompt": {
    "id": "meeting-summary",
    "version": 2,
    "variables": {"format": "一个 summary 字段的 JSON 对象", "transcript": "会议内容……"}
  }
}
```

- `model` 必填，且必须是已公开的网关模型别名。
- `messages` 是统一的会话输入；引用 `role=user` 模板时可为空，渲染后的最终会话必须有用户任务。
- `stream` 默认 `false`；`stream_options` 仅允许在流式请求中使用。
- `max_tokens` 缺省为 1024，允许 1–4096；超过网关配置上限返回 `422 invalid_request`。所有长度、超时和并发限制见“韧性与资源控制”。
- `response_format` 可选；首版支持 `json_object` 和 `json_schema`。
- `gateway_prompt` 是网关扩展字段，一个请求至多引用一个模板。`role=system` 模板插入开头，`role=user` 模板追加到末尾；调用方无需传递完整模板正文。示例假设模板已发布。
- 系统消息仅允许出现在会话开头。Responses Adapter 将开头的系统消息按顺序合并成 `instructions`，其余历史消息映射到 `input`，避免移动中途系统消息而改变会话语义。

### 完整响应

无论实际调用哪一种上游协议，均归一化为 Chat Completions 风格的响应。响应中的 `model` 返回网关模型别名；`usage` 统一为 `prompt_tokens`、`completion_tokens`、`total_tokens`。发生修复时包含本次逻辑请求全部上游尝试的已知用量；缺失字段为 `null`，以 `gateway_usage_complete` 标识是否完整。

完整响应包含 `id`、`object=chat.completion`、`created`、`model`、`choices` 和 `usage`；JSON 模式下，合法 JSON 仍放在 `choices[0].message.content` 字符串中。上游因 Token 上限截断映射到 `finish_reason=length`，内容过滤映射到 `finish_reason=content_filter`，并记录 `status=incomplete`，不计入完整成功次数；不能把任何 HTTP 200 都解释为生成成功。

### 流式响应

响应头为 `Content-Type: text/event-stream`。每个文本增量以统一格式发出：

```text
data: {"id":"req_xxx","object":"chat.completion.chunk","created":1787875200,"model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"SSE"},"finish_reason":null}]}

data: {"id":"req_xxx","object":"chat.completion.chunk","created":1787875200,"model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

Chat Completions 上游的 chunk 与 Responses 上游的文本增量事件都转换为此格式。客户端设置 `stream_options.include_usage=true` 时，网关在 `[DONE]` 前额外输出 `choices=[]` 的用量块；无论客户端是否请求此块，网关都采集上游用量。

HTTP 响应提交之前完成鉴权、校验、观测记录创建、准入和上游首个文本/终止事件预取；重试受全请求最多 3 次上游调用及总时限约束。HTTP 响应一旦开始，任何上游错误只发送 SSE 错误对象，不再重试；`[DONE]` 仅表示流结束，不代表成功。空文本的合法终止也应结束，不能无限等待首 Token。

Responses 上游用 `response.completed` / `response.incomplete` / `response.failed` 结束，不能等待其发送 `[DONE]`；这些事件由 Adapter 归一化。参见 [DeepSeek Responses 流式文档](https://api-docs.deepseek.com/guides/responses_api/)。

SSE 解析以空行分隔的完整事件为单位，处理 `event:`、多行 `data:`、注释心跳和跨网络块字符，不能把一行或一个 HTTP chunk 等同于一个事件。客户端断开时关闭上游流、取消重试并记为 `cancelled`；结构化流式校验的累积缓冲必须有大小上限。

### 错误响应

非流式请求始终返回统一的 OpenAI 风格错误对象：

```json
{
  "error": {
    "message": "Model deepseek-v4-pro is rate limited",
    "type": "rate_limit_error",
    "param": "model",
    "code": "rate_limit_exceeded"
  }
}
```

流已经开始后不能修改 HTTP 状态码；此时以 SSE 错误事件结束：

```text
data: {"error":{"message":"Upstream stream interrupted","type":"api_error","code":"upstream_stream_error"}}

data: [DONE]
```

## 5. 请求处理流程

一个逻辑请求可包含原始生成、传输重试和一轮结构化修复，但整个过程共享一个 request_id、一份固定的 Prompt 版本、最多 3 次上游调用和同一个总时限。

1. 接收请求，创建服务端 request_id 并启动单调时钟；执行请求体大小限制、鉴权、基础参数校验和模型解析。
2. 创建 `running` 调用记录。写入失败立即返回 `503 observability_unavailable`，不发起上游请求。
3. 解析并固定 Prompt 版本，渲染变量和组装消息；校验 Schema、消息角色及所有资源上限。
4. 按模型执行并发和 RPM 准入。拒绝时记录 `rejected` 并返回 429；内部重试和修复不重复消费客户端 RPM 配额。
5. Gateway 通过对应 Adapter 调用上游。每次真正发出 HTTP 请求都消费一个共享调用预算；重试退避、修复和发送输出均受总时限限制。
6. 非流式路径校验完整输出，必要时启动一轮修复；流式路径在首个文本或终止事件预取成功后提交响应，之后不重试、不修复。
7. 正常结束、失败或取消后，关闭上游连接并释放并发名额；保存最终状态、已知用量和延迟。结束记录写入失败只告警，不重复请求模型、不改写已经发出的内容。

鉴权、基础请求校验和模型解析之前的拒绝只写脱敏访问日志；建立调用记录后的所有结束路径都回填状态。进程崩溃导致的记录缺失与未完成状态按“可观测性与持久化”处理。

## 6. 模型路由与协议适配

模型注册表以配置文件维护，记录模型别名、适配器类型、真实模型标识、上游基地址、密钥环境变量和限流参数。请求只根据 `model` 选择一条确定的路由：

```text
deepseek-v4-pro   → ChatCompletionsAdapter → deepseek-v4-pro
deepseek-v4-flash → ResponsesAdapter       → deepseek-v4-flash
```

未知模型返回 `404 model_not_found`。首版不设置跨模型 fallback：Pro 与 Flash 的能力、价格与输出特征不同，失败时静默切换会破坏调用方预期。

### Adapter 接口

适配器接收同一份内部规范化请求，并返回规范化完整响应或类型化事件。事件必须能承载用量和结束原因，不能只返回字符串。

```text
Adapter
  complete(normalized_request) -> normalized_response
  stream(normalized_request)   -> async iterator[TextDelta | Usage | Finished]
```

#### ChatCompletionsAdapter

- 构造上游 `messages`、`model`、采样参数与供应商支持的 `response_format`。
- 使用对应供应商的鉴权头和 `/chat/completions` 请求体。
- 从 `choices[0].message.content` 解析完整文本，从 `choices[].delta.content` 解析流式文本。

#### ResponsesAdapter

- 将开头的系统消息按顺序合并为 `instructions`，其余 `messages` 保序映射为 `input`。
- 将 `max_tokens` 映射为 `max_output_tokens`。
- 将 `response_format` 映射为 Responses API 的 JSON Schema 格式字段。
- 只提取完整 `output` 中 message 的 output_text，不把 reasoning 当正文；将文本 delta 映射为统一事件，终止事件携带的完整文本不重复输出。

Adapter 将供应商错误转换为框架无关的 `UpstreamError`（状态、可重试性、脱敏错误类别）；它不负责路由、数据库写入、重试决策或对外 HTTP 错误。Gateway 编排调用，API 层统一封装 JSON/SSE，避免每个 Adapter 重复实现业务规则。

### 默认思考模式与参数映射

| 统一配置/请求 | Chat Completions 路径 | Responses 路径 |
| --- | --- | --- |
| 默认非思考模式 | `thinking.type=disabled` | `reasoning.effort=none` |
| 显式启用思考模式 | `thinking.type=enabled`，按配置设置 reasoning_effort | 按配置设置 reasoning.effort |
| 输出 Token 上限 | `max_tokens` | `max_output_tokens` |
| JSON Schema | JSON 模式与 Schema 提示，网关本地校验 | `text.format` 与网关本地校验 |

每种模式的可用参数均由 Adapter 明确列出；不接收任意上游参数透传。非思考模式下采样参数正常映射；启用思考模式时若采样参数不受支持，返回 `422 unsupported_feature`，不静默忽略。

## 7. Prompt 模板与版本

模板版本不可变，数据模型为：

```text
PromptTemplate(id, name, active_version)
PromptVersion(prompt_id, version, role, content, created_at)
```

- 创建相同 `id` 的新内容时递增版本号，历史版本不覆盖。
- 首版不提供历史版本删除；允许 `role=system` / `user`，不允许 `assistant` 模板。
- `gateway_prompt.version` 缺省时引用 `active_version`；指定时精确引用该版本。
- 未设置激活版本时，缺省引用返回 `422 prompt_version_required`；调用开始时解析并固定版本，后续重试与修复不重新读取激活指针。
- `system` 模板放最前，`user` 模板放最后。用户模板的变量包含本次任务正文，`messages` 可以仅承载历史上下文；追加模板不覆盖现有消息。
- 模板使用 `SandboxedEnvironment` 和 `StrictUndefined`；变量只接收 JSON 数据，不传入 Python 对象或函数，也不二次渲染变量值。限制模板长度、变量体积和渲染结果长度；Sandbox 不等于 Prompt Injection 防护，也不自动消除 CPU/内存消耗风险。
- 渲染后的内容不写入调用观测表，避免记录用户输入和敏感 Prompt 正文。

发布新版本与可选的激活操作必须放在一个短事务中。通过联合主键 `(prompt_id, version)` 和写事务保证并发发布不产生相同版本；读取版本、HTTP 调用期间不持有数据库写事务。

## 8. 结构化输出

### 上游能力差异

根据评审时核对的 [DeepSeek Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/)，其 `response_format` 列出 `text` 和 `json_object`；JSON 模式要求消息中明确指示输出 JSON。Pro 路径不直接发送 `json_schema`，由网关负责 Schema 校验。

| 路径 | 网关收到 `json_schema` 后的处理 |
| --- | --- |
| Pro / Chat Completions | 上游使用 `json_object`，将 Schema 和 JSON 指令加入消息，返回后本地严格校验 |
| Flash / Responses | 按其能力映射 `text.format`，返回后同样本地严格校验 |

Responses 的 `text.format` 支持情况参见 [DeepSeek 协议兼容说明](https://api-docs.deepseek.com/guides/responses_api/)。模型能力以配置声明、官方文档和真实冒烟测试共同确认；不根据协议名称推断全部功能都可用。

`strict=true` 在本项目中表示网关只把通过本地校验的非流式结果作为成功返回，不代表两个上游都执行原生受约束解码。首版 Schema 顶层固定为 object，嵌套字段支持明确的 Schema 子集（object / array / string / number / integer / boolean / null、properties、required、items、enum、additionalProperties），不支持的关键字返回 `422 unsupported_schema`。禁止远程 `$ref` 解析，防止校验时意外访问外部资源。

处理顺序如下：

```text
请求 Schema
  → 按 Adapter 映射为支持的格式约束与提示
  → 获取模型文本
  → 本地 JSON 解析
  → 本地 JSON Schema 校验
  → 合法 JSON 响应 / 422 structured_output_invalid
```

调用前先校验 Schema 自身，错误返回 `422 invalid_json_schema`。`json_object` 要求顶层是对象，不仅仅是任意可解析的 JSON。解析拒绝 NaN / Infinity 等非标准 JSON；只兼容完整包裹输出的一层 Markdown JSON 代码块，不从自然语言中猜测截取对象。去掉代码块后重新序列化，确保返回的 content 本身是合法 JSON。

非流式调用首次校验失败且调用预算与剩余时间允许时，最多启动一轮结构化修复：保留原任务，加入上一轮 assistant 输出与校验错误，要求只返回正确 JSON。取得修复输出后仍校验失败，或没有剩余预算可启动修复时，返回 `422 structured_output_invalid`；修复调用本身遇到网络错误则按上游错误与超时规则处理。截断与上游拒绝不能伪装为成功；明确的拒绝或内容过滤不触发结构化修复。修复的所有已知 Token 都计入调用总量，不保存修复正文。

流式调用在输出完成后对累计文本做本地校验，通过才发送正常完成块；失败发送 SSE `structured_output_invalid` 再结束。流式不修复，已经发出的字符无法撤回，也不能透明移除已发出的 Markdown 代码围栏；需要严格 JSON 的业务使用非流式模式。

## 9. 韧性与资源控制

### 共享调用预算与重试

- `max_upstream_calls=3`：首次生成、所有传输重试、结构化修复及修复的传输重试共享最多 3 次上游 HTTP 调用，禁止嵌套循环各自获得 3 次。
- `max_structured_repairs=1`：最多启动一轮修复；这一轮因网络故障再次发送仍属于同一轮修复，但每次发送都会消耗调用预算。
- 仅对连接错误、上游超时、429、500、502、503、504 重试。普通 4xx、鉴权错误、请求错误、客户端取消、整个请求已超时均不重试；JSON 校验失败只进入受限修复流程。
- 第一次重试等待 250ms，第二次等待 500ms，各增加 0–100ms 的随机抖动。等待函数和抖动源可注入，测试不执行真实等待。
- 上游带有效 `Retry-After` 时，等待时间取其与退避时间的较大值；若超出剩余请求时限，立即结束并返回对应的上游限流/失败，不提前重试。
- 流式只允许在下游 HTTP 响应提交前重试；提交后失败发出 SSE 错误并结束。所有 httpx 隐含重试关闭，调用预算由 Gateway 统一控制。
- 连接失败以外的重试可能产生重复计费；本项目不提供上游 exactly-once 保证，也不自动切换模型。

预算示例：

| 过程 | 总上游调用数 | 结果 |
| --- | ---: | --- |
| 503 → 503 → 成功 | 3 | 返回成功 |
| 非法 JSON → 修复超时 → 重试修复成功 | 3 | 返回合法 JSON |
| 503 → 503 → 非法 JSON | 3 | 无剩余修复预算，返回 structured_output_invalid |
| 非法 JSON → 修复结果仍非法 | 2 | 修复轮数已用完，返回 structured_output_invalid |
| 流提交后上游断开 | 不增加 | SSE 错误结束，不重试 |

`upstream_call_count` 统计实际发送次数，`transport_retry_count` 统计因可重试故障再次发送的次数，`structured_repair_count` 统计启动的修复轮数。三个字段分别记录，不能相互替代。

### 按模型限流与并发准入

令牌桶键为 `调用方 Key 指纹 + 模型别名`。初版只有一把 Gateway Key，因此所有调用方共享该模型的桶；Pro 与 Flash 彼此独立，不声称提供用户隔离。

并发上限限制的是活跃逻辑请求数，覆盖其上游调用、退避和修复阶段。准入时先检查并发名额和 RPM 令牌，成功后原子地占用名额、扣除一个令牌；任何一项不足都立即拒绝，不排队、不部分扣费。传输重试和修复不再扣客户端 RPM 令牌，但仍计入上游调用预算。

拒绝返回 `429 rate_limit_exceeded`，用 `param=rate_limit` 或 `param=concurrency` 区分原因。RPM 拒绝按令牌恢复时间计算 `Retry-After`；并发拒绝返回 1 秒重试提示，该提示不保证届时一定有空位。已准入请求失败不返还 RPM 令牌，但并发名额始终在 finally 路径释放。检查、扣减、释放必须并发安全。

首版使用进程内状态，固定单实例、单 worker；多 worker、多副本和跨 Key 的供应商总配额控制不在本版范围内。

### 资源与超时默认值

以下值是网关自身的初版安全默认值，不代表供应商上限；统一写入配置，测试覆盖每个边界。容量单位均按 UTF-8 字节计算，KiB=1024 bytes，MiB=1024 KiB。

| 配置项 | 默认值 | 超限行为 |
| --- | --- | --- |
| `max_request_bytes` | 1 MiB | 413 request_too_large，不调用上游 |
| `max_template_bytes` | 64 KiB | 422 prompt_render_error |
| `max_variables_bytes` | 256 KiB | 422 prompt_render_error |
| `max_rendered_prompt_bytes` | 512 KiB | 422 prompt_render_error |
| `max_schema_bytes` / `max_schema_depth` | 32 KiB / 16 层 | 422 invalid_json_schema |
| `max_json_depth` | 32 层 | 请求数据超限为 422；上游 JSON 超限为 structured_output_invalid |
| `max_structured_output_bytes` | 1 MiB | 502 upstream_response_too_large；流提交后使用同码 SSE 错误 |
| `max_upstream_response_bytes` | 2 MiB，解压后的完整非流式响应 | 502 upstream_response_too_large |
| `max_sse_event_bytes` | 256 KiB，每个解码后的 SSE 事件 | 502 upstream_response_too_large；提交后使用 SSE 错误 |
| `default_max_tokens` / `max_output_tokens` | 1024 / 4096 | 请求值超出 1–4096 返回 422 invalid_request |
| `connect_timeout_seconds` | 10 秒 | 上游连接超时，可在预算内重试 |
| `read_idle_timeout_seconds` | 60 秒 | 上游读取空闲超时；心跳不能延长总时限 |
| `request_timeout_seconds` | 120 秒 | 504 request_timeout；提交后使用同码 SSE 错误 |
| `cleanup_timeout_seconds` | 2 秒 | 清理/终态记录失败只告警，确保释放资源 |
| Pro RPM / burst / max_concurrency | 10 / 2 / 2 | 429 rate_limit_exceeded |
| Flash RPM / burst / max_concurrency | 30 / 5 / 5 | 429 rate_limit_exceeded |

总请求时限从接收请求开始，覆盖正常处理、重试退避、修复和输出发送；终止后的资源清理有单独的 2 秒上限，不得借清理继续调用模型。上游超时由每次调用剩余时限收紧。HTTP 连接池大小必须容纳两个模型的总并发，并避免无界连接等待。

请求体限制按实际收到的字节累计，不能只信任 Content-Length；非流式上游响应也需边读边限长，不能完整读入内存后才检查。模板采用受限语法/过滤器和 JSON 变量，渲染增量累计到限额即停止，不允许任意函数调用。

### 统一错误码

对外错误对象及 SSE 形式见接口章节；上游异常消息必须脱敏，不透传密钥、完整响应正文或供应商内部信息。

| HTTP | code | 场景 |
| --- | --- | --- |
| 401 | invalid_api_key | Gateway Key 缺失或无效 |
| 404 | model_not_found / prompt_not_found | 模型、模板或版本不存在 |
| 413 | request_too_large | HTTP 请求体超过限制 |
| 422 | invalid_request / unsupported_feature | 请求不合法或首版未支持的能力 |
| 422 | prompt_render_error / prompt_version_required | 渲染失败或没有可解析的模板版本 |
| 422 | invalid_json_schema / unsupported_schema | Schema 不合法或使用不支持的约束 |
| 422 | structured_output_invalid | 最终 JSON/Schema 校验失败或无法修复 |
| 429 | rate_limit_exceeded | 网关 RPM 或并发准入拒绝 |
| 429 | upstream_rate_limited | 上游限流且预算或时限内无法再尝试 |
| 502 | upstream_error / upstream_stream_error | 上游不可恢复错误或流异常结束 |
| 502 | upstream_response_too_large | 完整响应、事件或校验缓冲超限 |
| 503 | observability_unavailable | 初始调用记录无法写入 |
| 504 | upstream_timeout / request_timeout | 上游调用超时或总请求时限耗尽 |

流式提交后 HTTP 状态保持不变，使用对应 code 的 SSE 错误，记录 status=error，随后结束。客户端已断开时不再尝试写 SSE。

## 10. 可观测性与持久化

每次调用生成 `request_id`，记录以下字段：

| 分类 | 字段 |
| --- | --- |
| 路由 | 网关模型、真实模型、Adapter、协议 |
| 用量 | 输入/输出/总 Token、缓存输入 Token、推理 Token（上游提供时）、用量完整性 |
| 延迟 | 请求总延迟、首 Token 延迟（TTFT） |
| 结果 | HTTP 状态、统一错误码、是否流式 |
| 韧性 | 总上游调用次数、传输重试次数、结构化修复次数、最后失败类别 |
| 模板 | Prompt ID、Prompt 版本 |

首版以 SQLite 保存明细，并提供只读管理接口返回最近调用和按模型、状态、流式/非流式分类的汇总。

- `input_tokens`、`output_tokens`、`total_tokens` 来自上游，不自行估算；缓存输入是输入子集，推理 Token 是输出子集，不能重复相加。
- 用量缺失记 `null`，并设置 `usage_complete=false`，不能填 0。多次尝试累计已知用量，同时保留各尝试的阶段、状态和已知计数；不得把重复出现在流中的累计 usage 再累加。
- 聚合同时返回有用量/缺用量的请求数；缺失数据不作为零参与平均值。计数按 `model`、`status`、`stream` 分类。
- `total_latency_ms` 用单调时钟衡量收到请求到最终响应交付给 ASGI 发送层或取消的耗时，包括等待、重试与修复，不代表客户端实际收齐的网络耗时。
- `ttft_ms` 从同一起点计到首个非空可见文本增量准备发送；心跳、role、reasoning 和 usage 不触发 TTFT。即便随后失败，也保留已观察到的 TTFT；没有文本则为 `null`。
- 请求状态至少区分 `running`、`success`、`incomplete`、`error`、`cancelled`、`rejected`；流式 HTTP 200 后失败仍记录 `status=error`，不能仅凭 HTTP 状态汇总成功率。

### SQLite 数据模型与事务

持久化使用三张主表：

| 表 | 关键字段与约束 |
| --- | --- |
| prompt_templates | id 主键、name、active_version、created_at、updated_at |
| prompt_versions | (prompt_id, version) 联合主键、role、content、created_at；关联模板且版本不可变 |
| invocations | request_id 主键、client_request_id、模型/协议、Prompt 引用、状态/错误、Token、延迟、三个调用计数、attempt_details、时间戳 |

`attempt_details` 使用 JSON 列保存最多 3 次尝试的阶段、状态和已知用量，不保存请求/响应正文。运行时以 SQLite 版本记录为准，演示模板源文件只用于初始化演示数据。

SQLite 启用 WAL、外键约束和 1 秒 busy timeout；所有写事务保持短小，不能跨 HTTP 调用、SSE 发送或退避等待。模板发布及可选激活在同一个写事务内完成，联合主键防止并发版本重号。每次调用先建立 running 记录，结束后回填。

### 观测写入失败策略

- 初始 running 记录创建失败：返回 `503 observability_unavailable`，不请求上游、不消耗模型调用预算，输出脱敏错误日志。
- 正常响应完成、上游失败或客户端取消后的回填失败：在有界清理时间内记录脱敏告警，不再次请求模型，不改写已发送响应，不阻塞并发名额释放。数据库仍不可用时 `/readyz` 返回 503。
- 单实例重启后，将遗留 running 记录收敛为 `status=error, error_code=process_interrupted`；未知终止时间和 Token 保持为空，不填造最终延迟或用量。
- 进程崩溃、断流或数据库故障可能使末次用量无法获取/落库，这是首版明确的持久化边界。保留已知信息与 usage_complete=false，不宣称精确账单审计。

模板正文是模板存储的必要数据；用户消息、变量值、渲染结果、完整模型输出、修复正文和密钥不进入调用表或应用日志。用量写入失败不改变上述隐私规则。

## 11. 配置与密钥管理

配置与敏感信息分层保存：

```text
gateway.example.yaml   可提交：模型别名、路由、超时、重试、限流
.env.example           可提交：环境变量名称，无真实值
gateway.yaml           本地覆盖配置，不提交
.env                   真实密钥，不提交
data/gateway.db        本地 SQLite 数据库，不提交
```

`.env.example` 的字段如下；本地复制为 `.env` 后填入真实密钥：

```dotenv
GATEWAY_API_KEY=replace-me
DEEPSEEK_PRO_API_KEY=replace-me
DEEPSEEK_FLASH_API_KEY=replace-me
DEEPSEEK_PRO_BASE_URL=https://api.deepseek.com
DEEPSEEK_FLASH_BASE_URL=https://api.deepseek.com
```

模型配置只引用环境变量，绝不保存真实 Key：

```yaml
retry:
  max_upstream_calls: 3
  max_structured_repairs: 1
timeouts:
  connect_timeout_seconds: 10
  read_idle_timeout_seconds: 60
  request_timeout_seconds: 120
  cleanup_timeout_seconds: 2
limits:
  max_request_bytes: 1048576
  max_template_bytes: 65536
  max_variables_bytes: 262144
  max_rendered_prompt_bytes: 524288
  max_schema_bytes: 32768
  max_schema_depth: 16
  max_json_depth: 32
  max_structured_output_bytes: 1048576
  max_upstream_response_bytes: 2097152
  max_sse_event_bytes: 262144
  default_max_tokens: 1024
  max_output_tokens: 4096
models:
  deepseek-v4-pro:
    adapter: chat_completions
    base_url: ${DEEPSEEK_PRO_BASE_URL}
    api_key: ${DEEPSEEK_PRO_API_KEY}
    upstream_model: deepseek-v4-pro
    thinking_enabled: false
    requests_per_minute: 10
    burst: 2
    max_concurrency: 2
  deepseek-v4-flash:
    adapter: responses
    base_url: ${DEEPSEEK_FLASH_BASE_URL}
    api_key: ${DEEPSEEK_FLASH_API_KEY}
    upstream_model: deepseek-v4-flash
    thinking_enabled: false
    requests_per_minute: 30
    burst: 5
    max_concurrency: 5
```

启动时，任何已启用模型缺少上游地址或 Key 都应令服务启动失败。日志和调用记录只保存调用方 Key 的不可逆短指纹，绝不记录鉴权头、环境变量值、用户正文或渲染后的 Prompt。

环境变量优先于 `.env`，后者仅提供本地缺省值。`GATEWAY_API_KEY` 缺失或仍为示例占位值也应拒绝启动；测试显式注入测试 Key，不存在“空 Key 自动免鉴权”。上游地址由配置限定，调用请求不能覆盖 URL 或凭据。

`.gitignore` 必须忽略 `.env` / `.env.*`（保留 `.env.example`）、`gateway.yaml`、数据库及 `-wal` / `-shm` 伴生文件、本地日志和验收私有输出。忽略规则不保护已经被 Git 跟踪的秘密；提交前还需检查暂存内容。模板源文件可能包含业务敏感信息，只有脱敏演示模板才提交。

## 12. 测试设计

### 测试分层与工具

| 层 | 工具与隔离方式 | 验证重点 |
| --- | --- | --- |
| 单元测试 | pytest / pytest-asyncio、假时钟、Repository stub、MockTransport | 字段与事件映射、模板渲染、Schema、限流、退避规则 |
| 应用集成测试 | FastAPI + ASGITransport + 显式 lifespan + 临时 SQLite + MockTransport | 请求通过真实路由、服务、Adapter、Repository 后的行为 |
| 本地流式传输测试 | 本地 Uvicorn 网关 + 本地可控假上游 + 真实 httpx TCP 连接 | 第一块能否提前到达、不等待全流完成才输出、断开取消、终止顺序 |
| 真实模型冒烟测试 | 显式 live 开关、外部 `.env` 密钥、低输出上限 | 两个真实协议/模型可用，结果与用量字段符合实际接口 |

默认测试不访问任何外部模型服务。单元测试没有真实网络与磁盘依赖；涉及版本发布/事务的测试使用真实临时 SQLite，不能只 mock SQL 后声称验证了持久化。

`ASGITransport` 本身不触发应用启动和关闭，测试夹具必须显式进入 lifespan（例如 `LifespanManager`），否则共享 HTTP Client 与数据库未按生产路径初始化。参见 [HTTPX ASGI lifespan 说明](https://www.python-httpx.org/advanced/transports/)。

另一个边界是：目前 HTTPX 的 ASGITransport 会汇集响应 body，所以“内存测试拿到了多条 SSE 文本”不能证明网络上是实时流式。流式到达时间和断开传播必须由本地真实 HTTP 测试验证。该判断依据 [HTTPX ASGITransport 实现](https://github.com/encode/httpx/blob/master/httpx/_transports/asgi.py)。依赖使用锁文件固定版本，本地传输测试始终保留。

### 测试夹具与可测试性接口

`create_app(settings, dependencies)` 作为应用组装入口。配置、上游 Client/Transport、时钟、等待函数、随机抖动源和数据库路径可注入；避免在业务方法内临时创建客户端或直接读取全局环境变量。

| 夹具 | 职责 |
| --- | --- |
| `test_settings` | 两种路由、固定测试 Key、短超时与可调限流阈值；不读取真实 `.env` |
| `temp_database` | 每个测试使用独立 `tmp_path`，真实建表并在结束时关闭连接 |
| `mock_upstream` | 按路径、请求体与调用次数提供不同响应，记录被调用次数和请求字段 |
| `fake_clock` / `fake_sleep` | 控制 TTFT、配额恢复和退避；不执行真实数秒等待 |
| `test_app` / `api_client` | 进入/退出 lifespan；通过 HTTP 入口访问完整应用 |
| `local_servers` | 为传输测试启动本地网关与假上游，绑定回环地址和动态端口，确认就绪并可靠回收 |

MockTransport 应接在真正的 Adapter 后面，不能把 `gateway.complete()` 整体替换为预设结果，否则路由、协议转换和重试都没有被验证。测试凭据是固定假值；禁止默认访问外网。对相同验收行为使用模型参数化用例复用测试逻辑，避免复制两套相似断言。

### 验收与边界用例矩阵

| 功能 | 正常路径证据 | 必须覆盖的边界 |
| --- | --- | --- |
| 双协议路由 | Pro 命中 `/chat/completions`；Flash 命中 `/responses`；对外结构一致 | 未知模型、错误 URL 拼接、未支持字段、上游鉴权头转换 |
| 消息转换 | 用户模板追加、系统模板前置、历史会话顺序不变 | 模板单独生成任务、缺变量、中途 system 消息拒绝 |
| SSE 解析 | 两种协议事件均产生正确 delta、结束原因和用量 | 网络块截断、多行 data、心跳、UTF-8 分片、空文本完成 |
| SSE 终止 | Responses completed 映射完成，incomplete 按原因映射 length/content_filter | failed、意外 EOF、先 delta 后异常、不得把失败记为成功 |
| 真流式/取消 | 收到第一块时上游尚未结束；客户端关闭后上游被关闭 | 下游缓冲、后续仍生成、取消后仍重试、资源泄漏 |
| JSON 输出 | json_object 顶层对象；Schema 返回合法 content 字符串 | 无效 Schema、未知关键字、远程引用、NaN、数组顶层、代码块 |
| 结构化修复 | 首次不合法、一次修复成功；所有已知 Token 累计 | 修复失败 422、预算不足、流式不修复、超限缓冲 |
| Prompt 版本 | 新发布不覆盖历史；指定版本和激活版本可独立读取 | 并发发布不重号、激活事务回滚、无激活版本、调用中激活变化 |
| 观测 | 明细/分类汇总、输入/输出/缓存/推理 Token、TTFT | usage 缺失为 null、重复累计 usage 不重复计数、部分流失败保留 TTFT |
| 重试与共享预算 | 前两次 503、第三次成功；生成与修复共享 3 次调用 | 修复网络重试也计预算、第三次非法输出不产生第四次、总时限、Retry-After、响应提交后不重试 |
| 模型限流与并发 | 同模型桶/并发满返回 429，另一模型仍可调用 | 原子准入、拒绝不扣配额、内部重试不扣 RPM、结束/取消释放名额 |
| 资源上限 | 请求、模板、Schema、输出缓冲和 SSE 事件在限额内正常工作 | 每个上限的边界值与超一字节、慢流超过总时限、清理有界 |
| 观测故障 | 初始写失败返回 503；结束回填失败只告警 | 初始失败不请求上游、结束失败不重调模型、重启恢复遗留 running、脱敏与资源释放 |
| 默认思考模式 | 两个 Adapter 显式关闭思考，回答仅含可见文本 | 推理事件不触发 TTFT、不泄漏到 content；启用推理后的计数与参数限制 |
| 鉴权与泄密 | 同一 Key 保护调用和管理端点；缺少/错误 Key 返回 401 | 日志/错误/用量不含假密钥或测试正文，缺配置启动失败 |
| 请求标识 | 每次生成独立 request_id，可关联全部尝试 | 两个请求携带相同客户端关联 ID，不发生主键覆盖 |

上游响应 fixture 以官方协议示例为起点，完整响应、usage 和失败事件分别建模。测试不只断言 HTTP 200，还要检查最终输出、实际命中的上游协议、调用次数和落库结果。

计时单元测试使用假时钟断言明确差值。本地流式测试采用同步事件：假上游先发送第一块，等待测试客户端确认收到后才发送剩余内容；设置有界超时，不用容易受机器性能影响的毫秒阈值判断“是否流式”。

### 验证脚本与报告

`scripts/verify_all.py` 复用 pytest 的验收标记和测试结果，不另写一份业务逻辑，也不直接打印预设 PASS。报告列出每项功能的用例数、通过/失败/跳过、模型/协议和失败摘要；任一必需项失败时退出码非零。

已提供并实际执行过的验证入口：

```bash
uv run pytest -q -m "not live"
uv run python scripts/verify_all.py
uv run python scripts/verify_all.py --live
```

默认报告要明确标注“Mock / 本地传输验证”，真实模型验收标为 `NOT_RUN`，不能显示全量验收完成。`--live` 是显式的可能计费操作；Key 缺失时报告缺配置并失败，而不是跳过后显示成功。真实请求使用短任务与有限输出，断言格式/协议/用量而不是固定文本；重试和限流故障注入仍只在假上游进行。

最终验收需要两类证据：默认自动化套件通过，以及 Pro/Flash 真实调用的脱敏报告（时间、协议、模型、网关请求 ID、状态、用量）。只靠 Mock 无法证明供应商端点、密钥权限或上游能力实际可用。

## 13. 验收与验证计划

验证脚本应覆盖以下可观察证据：

1. 分别使用 `deepseek-v4-pro` 与 `deepseek-v4-flash` 成功完成一次请求，并断言实际选用了不同 Adapter。
2. `stream=true` 接收多个 SSE 文本块及最终 `[DONE]`，并断言记录了 TTFT。
3. 提交 JSON Schema 并验证结果可解析且满足 Schema；另验证非法输出返回 `422`。
4. 创建两个模板版本，验证默认激活版本、显式历史版本及变量替换。
5. 查询调用明细和聚合统计，验证 Token 分类、总延迟与 TTFT。
6. Mock 上游前两次可重试失败、第三次成功，断言尝试次数和退避逻辑。
7. 连续调用同一模型直至超限，断言收到 `429`，且另一模型的桶不受影响。

Mock 测试与真实模型验证分层。自动化测试不使用真实 Key；真实网络测试必须显式开启并提示可能产生费用。最终作业验收仍必须保留两个真实模型成功调用的脱敏证据，不能用 Mock 通过代替“真实模型可用”。

## 14. 已确定的设计边界

- 对外仅提供 Chat Completions 文本子集；上游差异由两个 Adapter 显式转换，未支持功能返回错误。
- 每个请求最多 3 次上游调用、最多一轮结构化修复，默认非思考模式；所有资源和等待均受配置上限约束。
- 流式已发送文本不可撤回，严格 JSON 业务使用非流式模式；流式错误不能改变已经发出的 HTTP 状态码。
- 单 Key 的模型调用和管理权限相同；单进程限流不提供用户隔离或跨副本配额一致性。
- Prompt 版本能固定本地模板内容，但供应商可能更新模型别名背后的实现，因此不保证历史结果逐字可复现。
- JSON/Schema 合法只保证格式，不保证回答事实正确，也不能代替业务规则校验或 Prompt Injection 防护。
- 当前源码已通过 Mock、本地真实 HTTP 传输和真实双模型调用验证；脱敏报告保留模型别名、适配器类型、请求 ID、状态、Token、延迟和 TTFT 作为证据。

## 15. 实施顺序与交付物

1. 定义 Pydantic 请求、响应和领域模型，以及模型配置。
2. 建立两个 Adapter 与 Mock 上游测试夹具。
3. 实现路由、统一错误和重试。
4. 接入 SSE、用量/延迟账本和限流。
5. 实现 Prompt 版本与结构化校验。
6. 补齐验证脚本、启动指南与 curl 示例。

交付物包括完整源码、配置示例、依赖锁文件、可执行验证脚本、README 启动指南和 curl 示例，以及 `reports/verification.json` 中的脱敏自动化/真实模型验收报告；本节所列交付物均已完成。
