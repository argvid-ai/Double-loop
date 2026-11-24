"""Task-owned semantic alternatives; deployment resources never enter these arguments."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .contracts import ID, Contract
from .visual_contracts import VisualRule


class SemanticChoice(Contract):
    id: str = Field(pattern=ID)
    description: str = Field(min_length=1, max_length=1000)
    branch: Literal["keep", "discard", "protect"] = Field(
        description="keep包含所有符合目标的情况，程序会保护并延长事件；discard是明确不符合；protect仅用于证据不足，不能把已符合目标的活动类型放入protect。"
    )


class SemanticDecision(Contract):
    question: str = Field(min_length=1, max_length=2000)
    choices: tuple[SemanticChoice, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def unambiguous_routes(self):
        if not self.question.strip() or any(
            not choice.description.strip() for choice in self.choices
        ):
            raise ValueError("semantic text must be nonempty")
        if len({choice.id for choice in self.choices}) != len(self.choices):
            raise ValueError("semantic choice identities must be distinct")
        if {choice.branch for choice in self.choices} != {"keep", "discard", "protect"}:
            raise ValueError("this executor requires keep, discard and protect routes")
        return self

    def labelled(self):
        return list(zip("ABC", self.choices))


class StartObservation(Contract):
    continuous: bool = Field(
        default=False,
        description="用户明确选择常驻录像时为true；不设置截止，不能同时传duration_seconds。仍需保存默认录像配置以跨重启自动接续。仅适用于已安装的受管实时录像。",
    )
    duration_seconds: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=86400,
        description="截止秒数，不改变采样频率。受管增量摄像且只产时间标记时，这是采集截止，已保存尾段另有最多30秒分析收尾；其他任务沿用包含准备和排空的严格执行截止。省略时用执行单元default_duration_seconds。有限回放的实际长度由来源配置决定，不要把录制长度直接当作严格执行截止而截掉尾部。",
    )
    executor: str = Field(min_length=1, max_length=100)
    goal: str = Field(min_length=1, max_length=8000)
    decision: SemanticDecision | None = None
    visual_rule: VisualRule | None = None
    sampling: str = Field(
        default="standard",
        pattern=ID,
        description="端侧已安装的采样档ID；先查询执行单元能力，不传底层采样参数。",
    )

    @model_validator(mode="after")
    def nonempty(self):
        if self.continuous and self.duration_seconds is not None:
            raise ValueError("continuous recording cannot have a duration")
        if (self.decision is None) == (self.visual_rule is None):
            raise ValueError("provide exactly one semantic decision or native visual rule")
        if not self.executor.strip() or not self.goal.strip():
            raise ValueError("executor and goal must be nonempty")
        return self


class ListObservationExecutors(Contract):
    pass


class ControlObservationTask(Contract):
    scope: Literal["task", "analysis"] = "task"
    task_id: str = Field(pattern=ID)
    action: Literal["pause", "resume", "stop"]
