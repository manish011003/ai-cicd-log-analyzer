from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FailedStage(BaseModel):
    stage_name: str
    stage_id: str | None = None
    status: str = "FAILED"
    log_excerpt: str = ""


class FailureEvent(BaseModel):
    event_type: Literal["stage_failure", "build_failure"]
    jenkins_url: str
    job_full_name: str
    build_number: int
    build_url: str
    build_result: str
    failed_stages: list[FailedStage] = Field(default_factory=list)
    timestamp: datetime
    correlation_id: str
