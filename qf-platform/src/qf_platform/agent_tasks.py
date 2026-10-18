"""V2 real research tasks; paid-call effects differ from replayable Core steps."""

from __future__ import annotations

import copy
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .agent_runtime import AgentRuntime
from .agent_tools import DATASET, AgentCoreBusy
from .storage import PlatformError, digest
from .tasks import TaskService

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "REFUSED"}


class AgentTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["REAL_RESEARCH"]
    dataset: Literal["cn510300-exposed-raw-v1"]
    goal: str = Field(min_length=10, max_length=500)
    max_experiments: Literal[2, 4, 6] = 2
    max_model_calls: Literal[1] = 1

    @field_validator("max_experiments", "max_model_calls", mode="before")
    @classmethod
    def integer_budget(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("exact integer budget required")
        return value

    @field_validator("goal")
    @classmethod
    def goal_text(cls, value: str) -> str:
        if len(value.strip()) < 10:
            raise ValueError("meaningful bounded goal required")
        return value.strip()


class AgentTaskService(TaskService):
    def __init__(
        self,
        root: Path,
        *,
        runtime: AgentRuntime | None = None,
        allow_test_provider: bool = False,
        cache_views: bool = False,
    ) -> None:
        super().__init__(root, namespace="agent-tasks", cache_views=cache_views)
        self._runtime = runtime
        self.allow_test_provider = allow_test_provider

    def runtime(self) -> AgentRuntime:
        if self._runtime is None:
            self._runtime = AgentRuntime()
        return self._runtime

    @staticmethod
    def _snapshot(history: list[dict[str, Any]]) -> dict[str, Any]:
        state = TaskService._snapshot(history)
        state["schema_version"] = "qf-agent-task-v2"
        return state

    def list(self) -> dict[str, Any]:
        value = super().list()
        value.update(
            schema_version="qf-agent-task-list-v2",
            mode="REAL_RESEARCH",
            live_authority="NONE",
        )
        value.pop("paid_model_calls")
        return value

    def capabilities(self) -> dict[str, Any]:
        runtime = self.runtime()
        provider = runtime.provider()
        return {
            "schema_version": "qf-agent-capabilities-v2",
            "mode": "REAL_RESEARCH",
            "dataset_metadata": runtime.tools.inspect_dataset(DATASET),
            "budget": runtime.budget(),
            "effective_budget": runtime.effective_budget(),
            "model": provider.model,
            "provider": provider.name,
            "profile_id": provider.profile_id,
            "max_model_calls_per_task": 1,
            "live_authority": "NONE",
            "validated_alpha": False,
        }

    def evidence(self, task_id: str, evidence_id: str) -> dict[str, Any]:
        from quantforge.product.evidence import evidence_bytes

        task = self.get(task_id)
        item = next(
            (
                value
                for value in task["evidence"]
                if value["evidence_id"] == evidence_id
            ),
            None,
        )
        if item is None:
            raise PlatformError("TASK_EVIDENCE_NOT_FOUND")
        result = item["result"]
        if item["tool"] == "run_experiment":
            self.runtime().tools.compare_results([result])
        return {
            "schema_version": "qf-agent-evidence-v2",
            "task_id": task_id,
            "evidence_id": evidence_id,
            "tool": item["tool"],
            "core_evidence_id": result.get("core_evidence_id"),
            "raw_core_json": evidence_bytes(result["evidence_pack"]).decode()
            if item["tool"] == "run_experiment"
            else None,
            "evidence": item,
            "mode": "REAL_RESEARCH",
            "live_authority": "NONE",
        }

    def create(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        from .tasks import _KEY

        if not _KEY.fullmatch(request_id):
            raise PlatformError("TASK_IDEMPOTENCY_KEY_REQUIRED")
        request = AgentTaskRequest.model_validate(payload)
        signature = digest(request.model_dump())
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT id,signature FROM tasks WHERE request_id=?", (request_id,)
            ).fetchone()
            if prior:
                if prior[1] != signature:
                    raise PlatformError("TASK_IDEMPOTENCY_CONFLICT")
                return self._snapshot(self._projected_history(db, prior[0]))
            if db.execute("SELECT count(*) FROM tasks").fetchone()[0] >= 100:
                raise PlatformError("TASK_WORKSPACE_BUDGET_EXHAUSTED")
            runtime = self.runtime()
            metadata = runtime.preflight(request.max_experiments)
            check = getattr(runtime.provider(), "check_context", None)
            if check is not None:
                check({"goal": request.goal})
            task_id = "task-" + uuid.uuid4().hex
            stamp = datetime.now(UTC).isoformat()
            state = {
                "id": task_id,
                "request": request.model_dump(),
                "mode": "REAL_RESEARCH",
                "basis": "RAW_PRICE_ONLY",
                "planner": "NOT_STARTED",
                "paid_model_calls": 0,
                "live_authority": "NONE",
                "created_at": stamp,
                "updated_at": stamp,
                "status": "PENDING",
                "plan": [{"tool": "plan_research", "input": {}}],
                "completed_steps": 0,
                "total_steps": 1,
                "experiments_used": 0,
                "evidence": [],
                "report": None,
                "error": None,
                "model_plan": None,
                "model_call": None,
                "model_outcome": "NOT_STARTED",
                "dataset_metadata": metadata,
                "validated_alpha": False,
            }
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?)",
                (task_id, request_id, signature, stamp),
            )
            self._append(
                db,
                state,
                "REAL_TASK_CREATED",
                "真实数据受限研究任务已登记",
                max_experiments=request.max_experiments,
                max_model_calls=1,
            )
            return self._snapshot(self._projected_history(db, task_id))

    def control(self, task_id: str, action: str) -> dict[str, Any]:
        if action not in {"cancel", "resume", "recover"}:
            raise PlatformError("TASK_CONTROL_INVALID")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state = copy.deepcopy(self._projected_history(db, task_id)[-1]["state"])
            if action == "recover":
                if (
                    state["status"] != "FAILED"
                    or state["model_outcome"] != "RESPONSE"
                    or not state["model_plan"]
                    or state["completed_steps"] < 1
                ):
                    raise PlatformError(
                        "TASK_RECOVERY_REQUIRES_CONFIRMED_MODEL_AND_CORE_BOUNDARY"
                    )
                step = state["plan"][state["completed_steps"]]
                if step["tool"] != "inspect_dataset":
                    raise PlatformError(
                        "TASK_RECOVERY_REQUIRES_CORE_EFFECT_RECONCILIATION"
                    )
                self.runtime().tools.inspect_dataset(DATASET)
                state.update(status="PENDING", error=None)
                self._append(
                    db,
                    state,
                    "PURE_CHECK_RECOVERED",
                    "已重核数据来源，恢复原响应后的纯检查，不重发模型",
                )
                return self._snapshot(self._projected_history(db, task_id))
            if action == "cancel":
                if state["status"] in {"PENDING", "RUNNING"}:
                    state["status"] = "CANCELLED"
                    self._append(
                        db,
                        state,
                        "TASK_CANCELLED",
                        "停止后续研究步骤，已开始的调用需对账",
                    )
            else:
                if state["status"] != "CANCELLED":
                    raise PlatformError("TASK_RESUME_REQUIRES_CANCELLED")
                if state["model_outcome"] in {"UNKNOWN", "ERROR"}:
                    raise PlatformError("TASK_MODEL_EFFECT_REQUIRES_REVIEW_NO_RETRY")
                state["status"] = "PENDING"
                self._append(
                    db, state, "TASK_RESUMED", "沿用已提交模型响应和 Core 事实继续"
                )
            return self._snapshot(self._projected_history(db, task_id))

    @staticmethod
    def _report(state: dict[str, Any]) -> dict[str, Any]:
        comparison = next(
            value["result"]
            for value in state["evidence"]
            if value["tool"] == "compare_results"
        )
        lines = [
            "# QuantForge real bounded research task",
            "",
            state["request"]["goal"],
            "",
            "Mode: REAL_RESEARCH; raw price return; all dates previously exposed.",
            "Model proposes intent only; metrics below are verified Core computations.",
            "NO VALID ALPHA. No final access, generated code or live authority.",
            "",
            "| Run | Development Return | Validation Return "
            "| Validation Drawdown | Core Evidence |",
            "| --- | ---: | ---: | ---: | --- |",
            *[
                f"| {row['run_id']} "
                f"| {row['development_metrics']['total_return']:.4%} "
                f"| {row['validation_metrics']['total_return']:.4%} "
                f"| {row['validation_metrics']['max_drawdown']:.4%} "
                f"| {row['core_evidence_id']} |"
                for row in comparison["rows"]
            ],
            "",
            "## Limitations",
            *["- " + value for value in comparison["limitations"]],
        ]
        return {
            "title": state["request"]["goal"],
            **comparison,
            "evidence_refs": [value["evidence_id"] for value in state["evidence"]],
            "markdown": "\n".join(lines) + "\n",
        }

    def _step(self) -> bool:
        with closing(self.connect()) as db:
            selected = None
            for (task_id,) in db.execute("SELECT id FROM tasks ORDER BY created_at,id"):
                history = self._projected_history(db, task_id)
                if history[-1]["state"]["status"] not in TERMINAL:
                    selected = task_id, history
                    break
            if selected is None:
                return False
            task_id, history = selected
            state = copy.deepcopy(history[-1]["state"])
            if state["completed_steps"] == state["total_steps"]:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    state = copy.deepcopy(
                        self._projected_history(db, task_id)[-1]["state"]
                    )
                    if state["status"] in TERMINAL:
                        return False
                    if (
                        state["model_plan"]
                        and state["model_plan"]["action"] == "refuse"
                    ):
                        state["status"] = "REFUSED"
                        self._append(
                            db,
                            state,
                            "MODEL_REFUSED",
                            "受限规划器拒绝了不支持的研究请求",
                        )
                        return True
                    state.update(status="SUCCEEDED", report=self._report(state))
                    self._append(db, state, "REPORT_READY", "真实 Core 研究报告已完成")
                return True
            step_index = state["completed_steps"]
            step = state["plan"][step_index]
            with db:
                db.execute("BEGIN IMMEDIATE")
                before = self._projected_history(db, task_id)
                if before[-1]["seq"] != history[-1]["seq"]:
                    return False
                state["status"] = "RUNNING"
                if step["tool"] == "plan_research":
                    state["model_outcome"] = "REQUEST_PENDING"
                self._append(
                    db,
                    state,
                    "STEP_STARTED",
                    "开始受限模型规划"
                    if step["tool"] == "plan_research"
                    else "开始 Core 公开研究工具",
                    tool=step["tool"],
                    step=step_index + 1,
                )
            started = time.perf_counter()
            value, failure, deferred = None, None, False
            try:
                runtime = self.runtime()
                if step["tool"] == "plan_research":
                    value = runtime.plan(
                        task_id,
                        state["request"]["goal"],
                        state["request"]["max_experiments"],
                    )
                    if (
                        value["planner"]
                        not in {"BOUNDED_REAL_LLM_V1", "BOUNDED_REAL_LLM_BM25_V2"}
                        and not self.allow_test_provider
                    ):
                        raise PlatformError(
                            "REAL_MODEL_RESPONSE_REQUIRED_NO_FAKE_FALLBACK"
                        )
                elif step["tool"] == "inspect_dataset":
                    if step["input"] != {"dataset": DATASET}:
                        raise PlatformError("AGENT_DATASET_NOT_ALLOWED")
                    value = runtime.tools.inspect_dataset(DATASET)
                elif step["tool"] == "run_experiment":
                    if state["experiments_used"] >= state["request"]["max_experiments"]:
                        raise PlatformError("TASK_EXPERIMENT_BUDGET_EXHAUSTED")
                    value = runtime.tools.run_experiment(
                        step["input"],
                        task_id=task_id,
                        index=state["experiments_used"],
                        call_id=state["model_call"]["call_id"],
                        goal=state["request"]["goal"],
                        rationale=state["model_plan"]["rationale"],
                    )
                elif step["tool"] == "compare_results":
                    if step["input"]:
                        raise PlatformError("TASK_COMPARE_INPUT_INVALID")
                    value = runtime.tools.compare_results(
                        [
                            row["result"]
                            for row in state["evidence"]
                            if row["tool"] == "run_experiment"
                        ]
                    )
                else:
                    raise PlatformError("AGENT_TOOL_NOT_ALLOWED")
            except AgentCoreBusy:
                deferred = True
            except (ValueError, TypeError, KeyError, OSError, ArithmeticError):
                failure = "AGENT_MODEL_OR_CORE_STEP_REQUIRES_REVIEW"
            with db:
                db.execute("BEGIN IMMEDIATE")
                current = copy.deepcopy(
                    self._projected_history(db, task_id)[-1]["state"]
                )
                if current["completed_steps"] != step_index:
                    raise PlatformError("AGENT_STEP_COMMIT_CONFLICT")
                if step["tool"] == "plan_research":
                    call = self.runtime().call_state(task_id)
                    current["model_call"] = call
                    current["paid_model_calls"] = (
                        call["paid_model_calls"] if call else 0
                    )
                    current["model_outcome"] = call["status"] if call else "NOT_SENT"
                if deferred:
                    self._append(
                        db,
                        current,
                        "CORE_ADMISSION_DEFERRED",
                        "Core 宿主门禁繁忙，尚未派发，不认定研究失败",
                        tool=step["tool"],
                    )
                    return True
                if failure:
                    current.update(status="FAILED", error=failure)
                    self._append(
                        db,
                        current,
                        "STEP_FAILED",
                        "未完成步骤，保留预算和已发生效果，不盲目重发",
                        tool=step["tool"],
                    )
                    return True
                assert value is not None
                if step["tool"] == "plan_research":
                    current.update(
                        planner=value["planner"],
                        model_plan=value["model_plan"],
                        knowledge=value.get("knowledge"),
                        model_call=value["call"],
                        model_outcome="RESPONSE",
                        paid_model_calls=value["call"]["paid_model_calls"],
                    )
                    current["plan"] = current["plan"][:1] + value["steps"]
                    current["total_steps"] = len(current["plan"])
                    if (
                        value["model_plan"]["action"] == "refuse"
                        and current["status"] != "CANCELLED"
                    ):
                        current["status"] = "REFUSED"
                else:
                    evidence = {
                        "tool": step["tool"],
                        "input": step["input"],
                        "result": value,
                    }
                    evidence["evidence_id"] = "task-evidence-" + digest(evidence)
                    current["evidence"].append(evidence)
                    current["experiments_used"] += int(step["tool"] == "run_experiment")
                current["completed_steps"] += 1
                self._append(
                    db,
                    current,
                    "STEP_COMPLETED",
                    "模型意图已提交"
                    if step["tool"] == "plan_research"
                    else "Core 工具事实已提交",
                    tool=step["tool"],
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
                if (
                    current["completed_steps"] == current["total_steps"]
                    and current["status"] not in TERMINAL
                ):
                    current.update(status="SUCCEEDED", report=self._report(current))
                    self._append(
                        db, current, "REPORT_READY", "真实 Core 比较报告已完成"
                    )
            return True
