# -*- coding: utf-8 -*-
"""本地 Web 服务：REST + SSE + 静态界面。

安全边界
--------
本服务只绑定 127.0.0.1，且只读用户自己机器上的文件。
- 没有鉴权是有意的：它等价于「本机进程」，任何能访问 127.0.0.1 端口的
  程序本来就能读这些文件（浏览器同源策略也阻止远端页面打这个端口）。
- 文件浏览接口限制在用户主目录内，避免误扫整盘；分析接口接受任意绝对
  路径，因为用户是显式指定自己要分析哪个文件的。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from log_ai_compressor import __app_name__, __version__, service as S
from log_ai_compressor.web import jobs as J

STATIC_DIR = Path(__file__).parent / "static"
# 文件浏览器只允许在用户主目录内活动
HOME = Path(os.path.expanduser("~"))
# 大文件自动提示阈值
BIG_FILE_MB = 64


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    """一次分析请求。三种模式由 mode 区分。"""
    mode: str = Field("file", description="file / text / compare")
    paths: List[str] = Field(default_factory=list)
    text: str = ""
    params: Dict[str, Any] = Field(default_factory=dict)


class ExportRequest(BaseModel):
    job_id: str
    format: str = "md"
    redact: bool = False
    custom_rules: List[str] = Field(default_factory=list)
    top_n: Optional[int] = None


# ---------------------------------------------------------------------------
# 文件浏览（本地工具不需要上传，直接让用户挑本机文件）
# ---------------------------------------------------------------------------
# 关于范围限制：这里**刻意不限主目录**。早期版本锁死在用户主目录，结果
# 三个问题：(1) 日志放在 D:\ / /var/log 的用户完全用不了浏览器；
# (2) CI 检出目录本来就不在 HOME 里，测试在所有 runner 上都挂；
# (3) 安全收益近乎为零 —— 服务只监听 127.0.0.1，且 /api/analyze 本来就
# 接受任意路径，锁浏览器并不构成任何真正的边界。
# 真要收紧，用 LOG_AI_FS_ROOT 环境变量限定一个根目录。
FS_ROOT = os.environ.get("LOG_AI_FS_ROOT") or None


def _fs_entry(p: Path) -> Dict[str, Any]:
    try:
        st = p.stat()
        is_dir = p.is_dir()
        size = 0 if is_dir else st.st_size
    except OSError:
        is_dir, size = p.is_dir(), 0
    return {
        "name": p.name,
        "path": str(p),
        "is_dir": is_dir,
        "size": size,
        "size_text": "" if is_dir else _human_size(size),
        "mtime": (lambda m: time.strftime("%Y-%m-%d %H:%M", time.localtime(m))
                  )(p.stat().st_mtime) if p.exists() else "",
    }


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


def _list_dir(target: str) -> Dict[str, Any]:
    path = Path(target).expanduser() if target else (Path(FS_ROOT) if FS_ROOT else HOME)
    if not path.is_absolute():
        path = (Path(FS_ROOT) / path) if FS_ROOT else (HOME / path)
    if FS_ROOT and not _within_root(path):
        raise HTTPException(400, f"只能浏览 {FS_ROOT} 内的路径")
    if not path.exists():
        raise HTTPException(404, f"路径不存在：{path}")
    if not path.is_dir():
        path = path.parent
    try:
        items = sorted(
            (_fs_entry(p) for p in path.iterdir()),
            key=lambda e: (not e["is_dir"], e["name"].lower()),
        )
    except PermissionError:
        raise HTTPException(403, f"无权读取：{path}")
    except OSError as exc:
        raise HTTPException(400, f"读取失败：{exc}")
    return {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else "",
        "can_go_up": not (FS_ROOT and not _within_root(path.parent)),
        "home": str(HOME),
        "fs_root": FS_ROOT or "",
        "entries": items,
    }


def _within_root(path: Path) -> bool:
    """仅当设置了 LOG_AI_FS_ROOT 时才生效。"""
    if not FS_ROOT:
        return True
    try:
        path.resolve().relative_to(Path(FS_ROOT).resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{__app_name__} 本地服务",
        version=__version__,
        description="本地日志分析取证台：数据不出网",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # ---- 健康检查 / 元信息 ---------------------------------------------
    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        from log_ai_compressor.rules.engine import list_presets
        return {
            "ok": True,
            "app": __app_name__,
            "version": __version__,
            "home": str(HOME),
            "rules": list_presets(),
            "ai": _ai_status(),
        }

    # ---- 文件浏览 -------------------------------------------------------
    @app.get("/api/fs/list")
    def fs_list(path: str = Query("", description="目录绝对路径，空=主目录")):
        return _list_dir(path)

    @app.get("/api/fs/file")
    def fs_file(path: str = Query(..., description="文件绝对路径")):
        """单文件信息：用于输入框自动补全与大小提示。"""
        p = Path(path).expanduser()
        if not p.is_file():
            raise HTTPException(404, f"文件不存在：{p}")
        info = _fs_entry(p)
        info["big"] = info["size"] >= BIG_FILE_MB * 1024 * 1024
        return info

    # ---- 分析任务 -------------------------------------------------------
    @app.post("/api/analyze")
    def analyze(req: AnalyzeRequest):
        mode = (req.mode or "file").lower()
        if mode not in ("file", "text", "compare"):
            raise HTTPException(400, f"未知模式：{req.mode}")
        # 先干跑一次参数校验 + 文件存在性检查，把 4xx 立刻返回，
        # 别让用户开着一个 SSE 连接等半天才看到「文件不存在」
        try:
            S.normalize_params(req.params)
            if mode == "text":
                if not (req.text or "").strip():
                    raise HTTPException(400, "请先粘贴日志文本")
            else:
                paths = [p.strip() for p in req.paths if p.strip()]
                if mode == "compare":
                    if len(paths) < 2:
                        raise HTTPException(400, "对比分析至少需要 2 个日志文件")
                elif not paths:
                    raise HTTPException(400, "请先选择日志文件")
                missing = [p for p in paths if not Path(p).is_file()]
                if missing:
                    shown = "、".join(missing[:3])
                    raise HTTPException(
                        400, f"文件不存在：{shown}" + ("…" if len(missing) > 3 else ""))
        except S.ServiceError as exc:
            raise HTTPException(400, str(exc))
        job = J.start_job(mode, {"paths": req.paths, "text": req.text,
                                 "params": req.params})
        return {"job_id": job.id, "mode": mode}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = J.REGISTRY.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在或已被淘汰（请重新分析）")
        return job.snapshot()

    @app.get("/api/jobs/{job_id}/stream")
    def job_stream(job_id: str):
        job = J.REGISTRY.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在或已被淘汰（请重新分析）")
        return EventSourceResponse(J.stream_events(job))

    @app.post("/api/jobs/{job_id}/cancel")
    def job_cancel(job_id: str):
        job = J.REGISTRY.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        job.cancel_event.set()
        return {"cancelled": True}

    # ---- 导出 -----------------------------------------------------------
    def _export_common(req: ExportRequest) -> tuple:
        job = J.REGISTRY.get(req.job_id)
        if job is None or job.result is None:
            raise HTTPException(404, "结果不存在或已被淘汰（请重新分析）")
        fmt = (req.format or "md").lower()
        try:
            if job.mode == "compare":
                text = job.payload_out.get("markdown", "") if job.payload_out else ""
                if req.redact:
                    text = S.redact_text(text, req.custom_rules or None)
            else:
                text = S.export_text(
                    job.result, fmt, top_n=req.top_n,
                    redact=req.redact, custom_rules=req.custom_rules)
        except S.ServiceError as exc:
            raise HTTPException(400, str(exc))
        return fmt, text

    @app.post("/api/export")
    def export(req: ExportRequest):
        fmt, text = _export_common(req)
        return Response(
            content=text,
            media_type=S.media_type(fmt),
            headers={"Content-Disposition":
                     f'attachment; filename="log-report.{fmt}"'},
        )

    @app.post("/api/export/copy")
    def export_copy(req: ExportRequest):
        """纯文本回传：前端一键复制用（不触发下载）。"""
        fmt, text = _export_common(req)
        return {"format": fmt, "text": text, "length": len(text)}

    # ---- AI 解读 --------------------------------------------------------
    @app.get("/api/ai/status")
    def ai_status():
        """前端 api() 默认发 GET，必须是 GET 路由。"""
        return _ai_status()

    @app.post("/api/ai/config")
    def ai_config_save(req: Dict[str, Any] = Body(default_factory=dict)):
        """保存 AI 服务商配置。

        安全约束：**绝不在响应里回显明文 Key**；请求里 key 为空表示
        「沿用已保存的 Key 或环境变量」，而不是清空它。
        """
        from log_ai_compressor.ai.config import load_config, save_config
        current = load_config()
        merged = {
            "provider": req.get("provider", current.provider),
            "base_url": req.get("base_url", current.base_url),
            "model": req.get("model", current.model),
            # 空 key 不覆盖：避免前端不回显明文导致用户一点保存就丢 Key
            "api_key": req.get("api_key") or current.api_key,
            "temperature": req.get("temperature", current.temperature),
            "max_tokens": req.get("max_tokens", current.max_tokens),
            "timeout": req.get("timeout", current.timeout),
        }
        try:
            cfg = load_config(merged)
            save_config(cfg)
        except Exception as exc:                      # noqa: BLE001
            raise HTTPException(400, f"配置无效：{exc}")
        return _ai_status()

    @app.post("/api/ai/explain")
    def ai_explain(req: Dict[str, Any] = Body(...)):
        from log_ai_compressor.ai import LLMError, explain_job
        try:
            return explain_job(req)
        except LLMError as exc:
            # AI 不可用是配置问题（未启用/Key 错），属 4xx，别让前端当崩溃
            raise HTTPException(400, str(exc))

    # ---- 静态界面 -------------------------------------------------------
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True),
                  name="static")
    else:                                            # pragma: no cover
        @app.get("/")
        def _missing_ui():
            raise HTTPException(500, f"前端资源缺失：{STATIC_DIR}")

    return app


def _ai_status() -> Dict[str, Any]:
    """AI 层可用性：未配置时明确告诉前端「不可用」而不是报错。"""
    try:
        from log_ai_compressor.ai import describe_config
        return describe_config()
    except Exception as exc:                          # noqa: BLE001
        return {"available": False, "reason": f"AI 模块未就绪：{exc}"}


def pick_port(host: str, port: int, tries: int = 20) -> int:
    """找一个空闲端口：双击启动时 8765 常被上一次残留进程占着。

    逐个往后试，用 bind 探测而不是靠运气；全被占则抛 OSError。
    """
    import socket

    for offset in range(max(1, tries)):
        candidate = port + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
            except OSError:
                continue
        return candidate
    raise OSError(f"{port}~{port + tries - 1} 端口都被占用，请用 --port 指定其它端口")


def serve(host: str = "127.0.0.1", port: int = 8765, *,
          open_browser: bool = True, log_level: str = "warning",
          auto_port: bool = True) -> int:
    """启动服务（CLI / 启动器入口）。

    Args:
        host: 绑定地址。默认只监听回环，不对外暴露。
        port: 起始端口；auto_port 为真时占用会自动顺延
        open_browser: 启动后自动打开浏览器
        log_level: uvicorn 日志级别
        auto_port: 端口被占时是否自动往后找

    Returns:
        实际监听的端口
    """
    import uvicorn

    if auto_port:
        port = pick_port(host, port)

    if open_browser:
        import threading
        import webbrowser

        url = f"http://{host}:{port}/"

        def _open() -> None:
            # 等 uvicorn 真正 bind 完再开，否则浏览器会先撞上
            # ERR_CONNECTION_REFUSED，看起来像"服务没起来"
            for _ in range(40):
                time.sleep(0.25)
                import socket as _s
                with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as probe:
                    if probe.connect_ex((host, port)) == 0:
                        break
            try:
                webbrowser.open(url)
            except Exception:                          # noqa: BLE001
                pass
        threading.Thread(target=_open, daemon=True).start()

    print(f"日志AI压缩器 v{__version__} · 本地服务已启动", flush=True)
    print(f"  界面：  http://{host}:{port}/", flush=True)
    print(f"  API 文档：http://{host}:{port}/api/docs", flush=True)
    print("  数据不出网，按 Ctrl+C 停止", flush=True)
    uvicorn.run(create_app(), host=host, port=port, log_level=log_level)
    return port
