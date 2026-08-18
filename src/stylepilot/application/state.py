from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypedDict

from stylepilot.domain.models import (
    DevelopSnapshotRef,
    EditPlan,
    PhotoMetadata,
    PhotoMetrics,
    PhotoRef,
    RenderVerificationReport,
    SuitabilityReport,
    WorkflowRequest,
    WorkflowStatus,
)


class WorkflowState(BaseModel):
    """Validated state passed between LangGraph nodes."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    request: WorkflowRequest
    status: WorkflowStatus | None = None
    message: str = ""
    photo: PhotoRef | None = None
    metadata: PhotoMetadata | None = None
    preview_path: Path | None = None
    metrics: PhotoMetrics | None = None
    suitability: SuitabilityReport | None = None
    plan: EditPlan | None = None
    applied_photo: PhotoRef | None = None
    recovery_snapshot: DevelopSnapshotRef | None = None
    rendered_metrics: PhotoMetrics | None = None
    verification: RenderVerificationReport | None = None
    write_approved: bool = False


class WorkflowUpdate(TypedDict, total=False):
    """Partial state emitted by a graph node."""

    status: WorkflowStatus
    message: str
    photo: PhotoRef
    metadata: PhotoMetadata
    preview_path: Path
    metrics: PhotoMetrics
    suitability: SuitabilityReport
    plan: EditPlan
    applied_photo: PhotoRef
    recovery_snapshot: DevelopSnapshotRef
    rendered_metrics: PhotoMetrics
    verification: RenderVerificationReport
    write_approved: bool
