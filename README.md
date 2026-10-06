# log-ai-compressor · 日志AI压缩器

> **日志的取证层，不是日志的分析层。**
> 我们不把日志交给大模型 —— 用确定性算法把日志变成**可引用的证据包**，
> 大模型只读证据。拿不出因果链就说证据不足，并精确列出还缺什么才能定论。
> **所有计算在本机完成，日志不出网。**

[![CI](https://github.com/hu-chenyu/log-ai-compressor/actions/workflows/ci.yml/badge.svg)](https://github.com/hu-chenyu/log-ai-compressor/actions)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](./pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](./pyproject.toml)

---

## 0. 这个工具和别的日志工具有什么不一样

绝大多数日志工具（包括带 AI 的）都在优化**命中率** —— 尽可能多地给出根因。
但这有个没被解决的问题：**一个自信的错误根因，比没有根因更糟**，因为值班的人会照着它去查。

举个实测例子。同一份 20 万行日志里：

| | 出现次数 | 真实角色 |
| --- | --- | --- |
| `connection pool exhausted` | **370** | 症状 |
| `Connection is not available` | **10** | **真正的因** |

按频率排，工具一定会说「根因：连接池耗尽」。**这是错的。** 真正的因只出现 10 次，被当成了噪音。

本工具的做法：

```
判定：可能原因 —— 统计推断，非因果证明
不可作为根因输出

最可能的原因：connection pool exhausted —— 但这是统计推断，不是因果证明

补齐下列证据才能定论：
  · 缺少确定性因果链   —— 带完整异常堆栈的日志（尤其是 Caused by: 链）
  · 存在难以区分的并列候选 —— 能区分二者的额外信号（调用链先后、指标对比）
  · 无异常堆栈        —— 未做异常栈裁剪的完整堆栈
```

**这就是本项目与全品类工具的核心差异**，详见第 3.4 节。

---

## 1. 它到底做什么

一句话：**把一个日志文件，变成一份带行号引用、压缩到几百 token 的证据包。**

| 场景 | 痛点 | 解法 |
| --- | --- | --- |
| **日志压缩投喂大模型** | 几十 MB 的日志远超 LLM 上下文窗口，直接粘贴要么截断要么爆 token | 聚类去重 + Top N + 典型样例。**实测 10 万行 / 7MB → 150 tokens** |
| **快速故障排查** | 上百万行人工翻找，同类错误刷屏干扰判断 | 根因排序 + 异常检测 + 堆栈降噪，直接指出「先查哪里」 |
| **给 AI Agent 当后端** | Agent 查日志只能 `grep` + 硬塞上下文 | 内置 MCP Server，Claude Code / Codex / mavis 直接调用 |
| **判定「这个问题能不能定论」** | 所有工具都硬给答案 | 证据充分性评估：**敢说我不知道**，并说清缺什么 |

### 为什么是「本地」

主流可观测平台（阿里云 SLS、Datadog、Dynatrace…）必须把日志**上传到云上**，
按 GB 或按主机计费，还要求先装 Agent 埋点。

这不是懒于做云版本，而是有具体理由：**2026 年 7 月 Hugging Face 遭攻击事件中，
他们的取证分析一开始尝试调用商业 LLM API，被安全策略拦截了** ——
护栏无法判断提交者是攻击者还是应急响应人员，最终只能自托管模型完成分析，
并因此保住了「凭据从未离开环境」。

涉密/内网日志根本不能上云，这是结构性的需求，不会被技术进步消除。

代价是它不做「持续监控」：它是取证工具，不是 APM 平台。这是有意的取舍。

---

## 2. 快速开始

### 安装

```bash
git clone https://github.com/hu-chenyu/log-ai-compressor.git
cd log-ai-compressor
pip install -r requirements.txt
```

### 启动（推荐：双击）

双击项目根目录的 **`start.bat`** —— 自动定位 Python、首次运行自动装依赖、启动本地服务并打开浏览器。

端口 8765 被占用时会自动顺延到 8766、8767…，不用手动改。

### 命令行启动

```bash
log-ai-compressor web              # 本地 Web 界面（等价于双击 start.bat）
log-ai-compressor web --port 9000  # 指定端口
log-ai-compressor web --no-browser # 不自动开浏览器
```

---

## 3. 核心特性

**分析引擎（全部本地算法，零出网）**

- 双输入模式：文件导入（超大文件、编码自动适配 UTF-8/GBK/GB2312/UTF-16）+ 文本粘贴
- 通用日志解析：时间戳 / 级别 / 模块 / 内容 / 堆栈（Java、Python、C/C++、gdb 帧全兼容）
- 模糊指纹聚类去重：行号、参数、十六进制 ID、路径差异全部抹平，同类错误只留一份典型样例 + 前后上下文
- 三档相似度：严格 ≥0.95（簇更准）/ 标准 ≥0.85（默认）/ 宽松 ≥0.70（簇更少更狠）
- 智能辅助分析：
  - **证据充分性评估（v2 核心）**：根因置信三档 `CONFIRMED` / `LIKELY` /
    `INSUFFICIENT`。只有 Caused-by 因果链直连才算 CONFIRMED；关键词投票、
    时间连锁这类统计线索只到 LIKELY。证据不足时**不输出「根因」措辞**，
    改为列出「还缺什么才能定论」—— 见第 0 节
  - 错误因果关联（Caused-by 链 / 时间连锁 / 根因关键词）自动区分根因与连锁衍生
  - 统计异常检测（中位数 + MAD 稳健基线）：集中爆发 / 周期发作 / 新型错误 / 罕见异常
  - 优先级综合评分（级别 35% + 频次 25% + 根因 20% + 异常 10% + 持续 5% + 新生 5%），按级别分档钳制
  - 堆栈降噪：折叠 `java.base` / `site-packages` / `node_modules` 等系统库与第三方帧，高亮业务栈帧
- 多文件对比：2~3 个文件的新增 / 消失 / 共同错误与数量变化率，适配版本对比与修复验证
- 可插拔解析规则引擎：YAML 声明规则，改配置不改代码即可接入新格式；内置 generic / embedded / jenkins 三套模板
- 脱敏：内置规则（邮箱/手机号/身份证/密钥）+ 自定义正则，导出与复制时自动生效
- 纯流式逐行处理：内存占用只与错误种类数相关，与日志总行数无关

**Web 界面（v2）**

- 零构建、零 CDN：纯 HTML/CSS/JS，直接改完刷新就生效，完全离线可用
- 三 Tab：文件导入 / 文本粘贴 / 多文件对比
- 内置文件浏览器：服务与日志同机，直接挑本机文件，无需上传（7MB 日志也不走网络）
- 实时 SSE 进度 + 可中途取消
- 错误簇列表（级别/次数/优先级/根因/异常标记）+ 详情面板（典型样例 / 前后上下文 / 降噪堆栈 / 变量分布 / 全部实例）
- 三张图表：错误时间分布（爆发段标红）/ 级别构成 / 模块分布 Top 10
- 实时过滤：支持 `and` / `or` / `not` 布尔表达式（例：`redis and not debug`）
- 一键导出：Markdown（投喂大模型）/ JSON / JSON 全文 / 纯文本 / HTML / 精简摘要
- 亮色 / 暗色双主题

**AI 解读（可选，不配置也完全可用）**

在压缩结果之上再生成一段人话解读：「一句话结论 / 根因链 / 先查哪里 / 证据不足的部分」。

- 三家通吃的 provider 接入：DeepSeek / 阿里百炼 Qwen / 智谱 GLM / Kimi / OpenAI / 本地 Ollama / 任意 OpenAI 兼容端点
- **不配 API Key 也能用**：聚类、根因判定、异常检测、导出全是本地算法，一分钱不花。AI 只是额外加一层
- **只上传压缩后的证据摘要**，不上传原始日志全文
- 提示词显式约束「只依据给定证据、不得编造」，避免模型编造不存在的根因

```bash
log-ai-compressor ai status                      # 看当前配置
log-ai-compressor ai config --provider deepseek  # 选服务商
log-ai-compressor ai config --provider deepseek --key sk-xxx
log-ai-compressor ai test                        # 测连通性
log-ai-compressor ai explain app.log             # 生成解读
log-ai-compressor ai explain app.log --cluster 3 # 只解读某个错误簇
```

也可以在 Web 界面右上角「⚙ AI 设置」里点选配置。

**MCP 接入（给 AI Agent 用）**

2026 年可观测平台几乎都出了 MCP Server（阿里云 SLS、Datadog、Grafana…），MCP 把平台从「人去看的目的地」变成「Agent 可调用的数据源」。**但那些平台的数据都在别人的机房里；本工具的数据就在你本机。**

7 个只读工具：`analyze_log_file` / `analyze_log_text` / `compare_log_files` / `export_report` / `get_cluster_detail` / `list_rules` / `check_environment`

```bash
log-ai-compressor mcp --install claude-code   # 打印配置片段
log-ai-compressor mcp --install codex
log-ai-compressor mcp --install mavis         # JSON 配置
log-ai-compressor mcp                         # 直接以 stdio 启动
```

接入后可以直接对 Agent 说：

> 分析一下 `C:\logs\app.log`，哪些错误是这次故障的根因？

Agent 会调用本工具做聚类、根因排序，并直接引用压缩后的证据摘要 —— 不需要把几十万行日志塞进上下文。

**全部工具只读**：没有删除、修改、上传、联网的接口。

---

## 4. CLI 使用

```bash
# 分析日志并导出 Markdown 报告（默认级别 ERROR,FAIL）
log-ai-compressor run examples/sample_system.log --top 20 -o report.md

# 指定级别、关键字、规则模板
log-ai-compressor run test.log --level ERROR,FAIL,WARN \
    --include "timeout,refused" --rule embedded --top 30 -o report.md

# JSON 格式
log-ai-compressor run test.log --format json -o report.json

# 多文件对比（第一个为基准）
log-ai-compressor compare examples/app_v1.log examples/app_v2.log -o diff.md

# 查看内置解析规则
log-ai-compressor rules list
```

| 子命令 | 用途 |
| --- | --- |
| `web` | 启动本地 Web 界面（主入口） |
| `mcp` | 启动 MCP Server / 打印客户端配置 |
| `ai` | AI 解读的 status / config / test / explain |
| `run` | 分析单个日志文件 |
| `compare` | 多文件对比 |
| `rules` | 查看解析规则模板 |

---

## 5. 性能数据

| 指标 | 实测值（Python 3.11 / 普通办公机） |
| --- | --- |
| 处理速度 | ~25 万行/秒（10 万行 / 7.03MB 用时 **0.40 秒**） |
| 压缩比 | 10 万行 / 7MB → 150 tokens，**12289 倍** |
| 核心层测试 | 347 用例 / 1.9 秒 / 覆盖率 94%（仅 core+rules+export） |
| 接入层测试 | service / web / mcp / ai 共 162 用例 / 2 秒 |
| 全量测试 | 803 用例（不含旧版 GUI 的 509 用例 4.3 秒跑完，覆盖率 91%） |
| 内存 | 与日志总行数无关，只与错误种类数相关 |

复现基准：`python scripts/benchmark.py`

---

## 6. 技术架构

```
log_ai_compressor/
├── service.py               # 共享服务层：参数校验 + JSON 序列化 + 导出门面
├── rules/                   # 可插拔解析规则引擎（YAML 驱动）
│   ├── engine.py            #   规则加载/编译/占位符展开
│   └── presets/             #   generic / embedded / jenkins 三套模板
├── core/                    # 核心处理层（零 UI / 零 Web 依赖）
│   ├── models.py            #   数据模型 + 自适应时间直方图
│   ├── encoding.py          #   编码探测（BOM/严格解码验证/截断容忍）
│   ├── parser.py            #   增量解析器（多行聚合：折行/堆栈/Caused-by）
│   ├── filters.py           #   级别 + 关键词准入过滤
│   ├── clustering.py        #   模糊指纹聚类（三级匹配）
│   ├── analysis.py          #   根因判定/异常检测/优先级/堆栈降噪
│   ├── pipeline.py          #   流式管线（进度/取消/上下文捕获）
│   ├── comparator.py        #   多文件对比
│   └── redact.py            #   脱敏
├── export/reporters.py      # 导出层（Markdown/JSON/文本/HTML/摘要）
├── web/                     # v2 Web 接入层
│   ├── server.py            #   FastAPI：REST + SSE + 文件浏览
│   ├── jobs.py              #   后台任务 + 进度队列 + SSE 帧
│   └── static/              #   零构建前端（index.html / app.js / style.css）
├── mcp/server.py            # MCP 接入层（7 个只读工具）
├── ai/                      # 可选 AI 解读层
│   ├── config.py            #   服务商配置（三层优先级 + Key 不回显）
│   ├── client.py            #   OpenAI 兼容 / Ollama 原生协议
│   └── prompts.py           #   提示词（显式约束防幻觉）
└── cli.py                   # 命令行入口
```

**分层解耦**：`rules → core → export → service → web / mcp / cli` 单向依赖。

- `core` 零 UI 依赖，可独立测试、被脚本直接复用：
  `from log_ai_compressor.core.pipeline import analyze_file`
- `service` 是 core 与接入层之间**唯一**的转换点，Web 和 MCP 共用同一份序列化逻辑，不会各写一份、各写错一份
- 前端零构建：不引入 node/webpack，改完刷新即生效，也不需要 npm

### 核心算法

1. **模糊指纹聚类（两级性能保护）**
   - 指纹 = 级别 + 掩码消息（数字→N、十六进制→H、UUID→U、路径→P、引号串→S）+ 堆栈前 3 行特征
   - 匹配路径：完整指纹精确命中（O(1)）→ (级别, 消息模板) 精确命中 → 同级别桶内编辑距离相似度（上限 256 次比较）
   - 变体命中后回写精确表，后续重复变体继续 O(1)

2. **内存控制**
   - 逐行流式读取，簇内只存「模板 + 计数 + 一份样例 + 有界直方图」
   - 时间直方图桶数上限固定（簇 96 / 全局 512），超限自动 8 倍扩宽桶宽合并旧桶

3. **根因判定（三路证据融合）**：Caused-by 链回溯 + 60 秒窗口内首发且含根因关键词 + 强关键词命中；含 retry/after/downstream 等被动词的簇标记为连锁衍生

---

## 7. 开发与测试

```bash
pip install -r requirements-dev.txt

ruff check log_ai_compressor tests scripts     # 代码规范
python -m pytest                                # 全量测试
python -m pytest --cov=log_ai_compressor --cov-report=term-missing
```

测试分层：

| 文件 | 覆盖 | 速度 |
| --- | --- | --- |
| `test_service.py` | 参数归一化、序列化、导出门面 | < 1s |
| `test_web.py` | REST 契约、SSE 帧格式、参数拦截、文件浏览边界 | < 2s |
| `test_mcp.py` | 工具注册、只读标注、各工具行为与错误路径 | < 1s |
| `test_ai.py` | 配置优先级、提示词、客户端（mock 网络）、可选性 | < 1s |
| `test_*.py`（core） | 解析 / 聚类 / 分析 / 导出 / 编码 / 对比 | ~2s |

CI（GitHub Actions）：矩阵（Ubuntu/Windows × Python 3.9/3.12）自动执行规范检查、测试与覆盖率统计。

### 启动脚本的硬约束

`start.bat` **必须是纯 ASCII + CRLF 行尾**，测试会强制校验。原因见 `tests/test_launcher.py` 顶部注释：cmd.exe 逐字节按控制台代码页解析批处理文件，用裸 LF 或含非 ASCII 字节都会导致解析错位、每行开头被吞，程序永远起不来。所有中文提示都放在 Web 界面里，不放 bat。

---

## 8. 自定义解析规则

新建 `my_format.yaml`：

```yaml
name: my_format
description: 自研日志格式
patterns:
  - name: main
    # {LEVEL} 为引擎占位符，自动展开为标准级别令牌
    regex: '^<(?P<timestamp>\d+)>\s*\[(?P<module>\w+)\]\s*(?P<level>{LEVEL})\s*(?P<message>.*)$'

stack_indicators:
  - '^\s*at\s+[\w$.]+\('

level_hints:            # 无级别字段的行按关键词推断（可选）
  ERROR: ['\bERROR\b', '\berror\b']
```

使用：`log-ai-compressor run app.log --rule my_format.yaml`

---

## 9. License

MIT
