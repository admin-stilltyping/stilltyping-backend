from datetime import date
from enum import IntEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .catalog import TYPE_BY_ID

UseCase = Literal["support", "clinic", "retail", "services"]


class Period(IntEnum):
    WEEK = 7
    MONTH = 30
    QUARTER = 90


WidgetType = Literal[
    "kpi",
    "progress",
    "gauge",
    "sparkline",
    "line",
    "area",
    "horizontal_bar",
    "vertical_bar",
    "grouped_bar",
    "stacked_bar",
    "pie",
    "donut",
    "funnel",
    "heatmap",
    "table",
    "activity",
]


class GenerateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    use_case: UseCase
    expected_revision: int = Field(ge=1)


class SaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    widget_ids: list[str] = Field(min_length=10, max_length=10)
    expected_revision: int = Field(ge=1)

    @field_validator("widget_ids")
    @classmethod
    def validate_widgets(cls, value):
        if len(set(value)) != 10 or any(key not in TYPE_BY_ID for key in value):
            raise ValueError("Choose exactly 10 different widgets from the catalogue.")
        return value


class WidgetDefinition(BaseModel):
    id: WidgetType
    type: WidgetType
    type_label: str
    group: Literal["summary", "chart", "detail"]
    title: str
    description: str


class Point(BaseModel):
    label: str
    value: float = Field(ge=0)
    secondary: float | None = Field(default=None, ge=0)


class HeatCell(BaseModel):
    day: str
    hour: str
    value: int = Field(ge=0)


class Activity(BaseModel):
    title: str
    detail: str
    time: str


class WidgetData(BaseModel):
    value: float = Field(default=0, ge=0)
    delta: float = 0
    unit: str = ""
    target: float = Field(default=100, gt=0)
    points: list[Point] = Field(default_factory=list)
    series_labels: list[str] = Field(default_factory=list)
    cells: list[HeatCell] = Field(default_factory=list)
    items: list[Activity] = Field(default_factory=list)


class WidgetOutput(WidgetDefinition):
    data: WidgetData


class ConfigOutput(BaseModel):
    use_case: UseCase
    widget_ids: list[WidgetType]
    revision: int
    as_of: date


class DashboardOutput(BaseModel):
    mode: Literal["demo"] = "demo"
    business_id: UUID
    business_slug: str
    business_name: str
    period: Period
    config: ConfigOutput
    use_cases: dict[str, str]
    catalog: list[WidgetDefinition]
    widgets: list[WidgetOutput]
