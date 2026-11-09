# agent/background_jobs.py
#
# Lets BRINKO keep working while you do something else.
#
# Slow tools (building code, processing files, generating images, research)
# used to block the whole voice session until they finished. Now they're
# handed to this manager: the tool call returns instantly ("on it, I'll tell
# you when it's done"), the work runs on its own thread, and when it
# finishes BRINKO is told the result and announces it in one line.

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Job:
    job_id:     int
    tool:       str
    summary:    str
    started_at: float = field(default_factory=time.time)
    ended_at:   float | None = None
    status:     str = "running"        # running | done | failed
    result:     str = ""
    origin:     str = "pc"             # pc | phone
    on_done:    Any = None             # callback(job) instead of the global on_finish

    @property
    def elapsed(self) -> float:
        return (self.ended_at or time.time()) - self.started_at

    def as_dict(self) -> dict:
        return {
            "id": self.job_id, "tool": self.tool, "summary": self.summary,
            "status": self.status, "elapsed": round(self.elapsed, 1),
            "result": self.result[:300],
        }


class BackgroundJobs:
    MAX_CONCURRENT = 4
    KEEP_FINISHED  = 20

    def __init__(self):
        self._jobs: dict[int, Job] = {}
        self._lock = threading.Lock()
        self._next_id = 1
        self._slots = threading.Semaphore(self.MAX_CONCURRENT)
        self.on_change: Callable[[list[dict]], None] | None = None
        self.on_finish: Callable[[Job], None] | None = None

    # ── public ──────────────────────────────────────────────────────────
    def submit(self, tool: str, summary: str, fn: Callable[[], Any],
               on_done: Callable[["Job"], None] | None = None, origin: str = "pc") -> Job:
        with self._lock:
            job = Job(self._next_id, tool, summary[:80] or tool, origin=origin, on_done=on_done)
            self._jobs[job.job_id] = job
            self._next_id += 1
            self._prune()
        self._notify()
        threading.Thread(target=self._run, args=(job, fn), daemon=True,
                         name=f"job-{job.job_id}-{tool}").start()
        return job

    def running(self) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status == "running"]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [j.as_dict() for j in sorted(self._jobs.values(), key=lambda j: -j.job_id)]

    def describe(self) -> str:
        jobs = self.snapshot()
        if not jobs:
            return "No background jobs right now."
        lines = []
        for j in jobs[:8]:
            if j["status"] == "running":
                lines.append(f"#{j['id']} {j['tool']} ({j['summary']}) — running for {int(j['elapsed'])}s")
            else:
                lines.append(f"#{j['id']} {j['tool']} ({j['summary']}) — {j['status']}: {j['result'][:120]}")
        return "\n".join(lines)

    # ── internals ───────────────────────────────────────────────────────
    def _run(self, job: Job, fn: Callable[[], Any]):
        with self._slots:
            try:
                r = fn()
                job.result = str(r) if r else "Done."
                job.status = "done"
            except Exception as e:
                traceback.print_exc()
                job.result = f"{type(e).__name__}: {e}"
                job.status = "failed"
            finally:
                job.ended_at = time.time()
        print(f"[Jobs] #{job.job_id} {job.tool} → {job.status} in {job.elapsed:.1f}s")
        self._notify()
        handler = job.on_done or self.on_finish
        if handler:
            try:
                handler(job)
            except Exception as e:
                print(f"[Jobs] ⚠️ on_finish failed: {e}")

    def _prune(self):
        finished = [j for j in self._jobs.values() if j.status != "running"]
        finished.sort(key=lambda j: j.job_id)
        for j in finished[:-self.KEEP_FINISHED]:
            self._jobs.pop(j.job_id, None)

    def _notify(self):
        if self.on_change:
            try:
                self.on_change(self.snapshot())
            except Exception:
                pass


_instance: BackgroundJobs | None = None


def get_jobs() -> BackgroundJobs:
    global _instance
    if _instance is None:
        _instance = BackgroundJobs()
    return _instance
