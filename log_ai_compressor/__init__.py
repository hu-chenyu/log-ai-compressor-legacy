# -*- coding: utf-8 -*-
"""log-ai-compressor：本地日志分析取证台（零数据出网）。

核心定位：
1. 日志压缩投喂大模型 —— 将海量日志压缩为结构化错误报告，适配 LLM 上下文窗口；
2. 快速故障排查 —— 聚类去重、根因定位、优先级排序，辅助人工快速定位问题；
3. 本地优先 —— 全部计算在本机完成，日志不出网，不需要账号、云服务或数据上传。

分层架构（单向依赖 rules → core → export → 接入层）：
- log_ai_compressor.rules        可插拔解析规则引擎（YAML 配置驱动）
- log_ai_compressor.core         核心处理层（解析/过滤/聚类/分析/管线/对比，零 UI 依赖）
- log_ai_compressor.export       导出层（Markdown / JSON / 纯文本 / HTML 报告）
- log_ai_compressor.web          本地 Web 服务（FastAPI，浏览器界面 + REST + SSE）
- log_ai_compressor.mcp          MCP 服务器（把分析能力暴露给 AI Agent）
- log_ai_compressor.ai           可选 AI 解读层（OpenAI 兼容端点 / 本地 Ollama，不配 key 可用）
- log_ai_compressor.cli          命令行入口（run / compare / rules / web / mcp / ai）
"""

__version__ = "2.0.0"
__app_name__ = "log-ai-compressor"
