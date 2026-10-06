# -*- coding: utf-8 -*-
"""后台任务管理：后台线程跑分析，进度经内存队列回传，前端用 SSE 订阅。

为什么不直接在请求里同步跑完？
- 百万行日志要跑十几秒，同步会让 HTTP 请求长时间挂起，进度条也没处更新；
- 前端无法在分析途中点「取消」；
- 旧版 GUI 用的就是「后台线程 + 轮询」，这里把轮询换成 SSE，
  语义一致但延迟从 200ms 级降到推送级。

与 GUI 的差异：本模块不碰任何 Tk 对象，纯内存状态，进程退出即释放。
"""
from __future__ import annotations

import json
import queue
import threading
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional

from log_ai_compressor import service as S

# 内存里保留的结果上限：超出后淘汰最旧的已完成任务
MAX_RETAINED = 8
# 单个订阅者的推送间隔下限（秒），防止极快任务把浏览器刷爆
_MIN_SSE_INTERVAL = 0.05


class Job:
    """一次分析任务的生命周期与结果容器。"""

    def __init__(self, mode: str, payload: Dict[str, Any]):
        self.id = uuid.uuid4().hex[:12]
        self.mode = mode
        self.payload = payload
        self.queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.created = time.time()
        self.finished: Optional[float] = None
        self.result: Optional[Any] = None          # AnalysisResult
        self.payload_out: Optional[Dict[str, Any]] = None   # 序列化后的 dict
        self.error: Optional[str] = None
        self.traceback: Optional[str] = None
        self._lock = threading.Lock()
        self._done = threading.Event()

    # -- 状态 ---------------------------------------------------------------
    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def running(self) -> bool:
        return not self._done.is_set()

    def snapshot(self) -> Dict[str, Any]:
        """给 /api/jobs/{id} 用的轻量状态（不含结果体）。"""
        with self._lock:
            state = "done" if self._done.is_set() else "running"
            if self.error:
                state = "error"
            return {
                "id": self.id, "mode": self.mode, "state": state,
                "created": self.created, "finished": self.finished,
                "elapsed": round((self.finished or time.time()) - self.created, 3),
                "error": self.error, "has_result": self.result is not None,
            }

    def _emit(self, event: str, **payload: Any) -> None:
        self.queue.put({"type": event, **payload})

    def _progress_cb(self, info: Dict[str, Any]) -> None:
        """适配 core 层 ProgressCallback 签名：**单个 dict 位置参数**。

        core 传来的键：lines / lps / clusters / elapsed / phase。
        这里补一个 percent（按已读行数与 stats 里的总量估算），前端才有
        东西可画；core 不给总行数，所以按「见过多少就当多少」的保守口径。
        """
        if self.cancel_event.is_set():
            return
        self._emit("progress", **(info or {}))


class JobRegistry:
    """任务表 + 淘汰策略。进程内全局单例。"""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()

    def create(self, mode: str, payload: Dict[str, Any]) -> Job:
        job = Job(mode, payload)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._evict_locked()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def _evict_locked(self) -> None:
        while len(self._order) > MAX_RETAINED:
            oldest = self._order[0]
            job = self._jobs.get(oldest)
            # 运行中的任务不淘汰，否则前端轮询会拿到 404
            if job is not None and job.running:
                break
            self._order.pop(0)
            self._jobs.pop(oldest, None)


REGISTRY = JobRegistry()


def _run_job(job: Job) -> None:
    """工作线程主体：跑分析 → 序列化 → 推 done 事件。"""
    params = dict(job.payload.get("params") or {})
    params["progress_cb"] = job._progress_cb
    params["cancel_event"] = job.cancel_event
    try:
        if job.mode == "compare":
            results = S.compare(job.payload.get("paths") or [], params)
            out = {"compare": S.compare_to_dicts(results),
                   "markdown": S.compare_to_markdown(results)}
            job.result = results
        else:
            result = S.analyze(
                paths=job.payload.get("paths"),
                text=job.payload.get("text"),
                params=params,
            )
            top_n = (job.payload.get("params") or {}).get("top_n")
            out = S.result_to_dict(result, top_n=top_n)
            job.result = result
        job.payload_out = out
        job._emit("done", result=out)
    except S.ServiceError as exc:
        # 参数问题属于用户可修正的 4xx，不要打完整 traceback 吓人
        job.error = str(exc)
        job._emit("error", message=str(exc), kind="validation")
    except Exception as exc:                      # noqa: BLE001 - 兜底防线程静默死
        job.error = f"{type(exc).__name__}: {exc}"
        job.traceback = traceback.format_exc()
        job._emit("error", message=job.error, kind="internal",
                  traceback=job.traceback)
    finally:
        job.finished = time.time()
        job._done.set()
        # 结束哨兵：让还在排队的订阅者立刻退出循环
        job._emit("eof")


def start_job(mode: str, payload: Dict[str, Any]) -> Job:
    """建任务并起后台线程。mode: file / text / compare。"""
    job = REGISTRY.create(mode, payload)
    t = threading.Thread(target=_run_job, args=(job,), daemon=True,
                         name=f"lac-job-{job.id}")
    t.start()
    return job


def stream_events(job: Job, max_wait: float = 3600.0):
    """SSE 事件生成器：透传队列内容，结束后补一个终态。

    注意：EventSourceResponse 的 data 字段若传 dict，会被 str() 成 Python
    字面量（单引号），**不是合法 JSON**，前端 JSON.parse 会直接抛错。
    所以这里统一自己 json.dumps 成字符串再发出去。

    订阅者中途断开时抛 GeneratorExit —— 由 ASGI 层消化，这里只需保证
    生成器干净退出、不再往队列里塞新数据。
    """
    deadline = time.time() + max_wait
    last_push = 0.0

    def _frame(event: str, payload: Dict[str, Any]):
        return {"event": event, "data": json.dumps(payload, ensure_ascii=False)}

    while True:
        if time.time() > deadline:
            yield _frame("error", {"type": "error", "message": "等待超时",
                                   "kind": "timeout"})
            return
        try:
            item = job.queue.get(timeout=0.25)
        except queue.Empty:
            if job.done:
                break
            continue
        kind = item.get("type")
        if kind == "eof":
            break
        # 合并过密的进度帧：两次推送至少间隔 _MIN_SSE_INTERVAL
        now = time.time()
        if kind == "progress" and now - last_push < _MIN_SSE_INTERVAL:
            continue
        last_push = now
        yield _frame(kind, item)
    # 订阅者可能错过了终态帧（eof 之前队列已清空），这里补一次
    if job.error:
        yield _frame("error", {"type": "error", "message": job.error,
                               "kind": "internal"})
    elif job.done and job.payload_out is not None:
        yield _frame("done", {"type": "done", "result": job.payload_out})
