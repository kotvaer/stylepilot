from __future__ import annotations

from pathlib import Path
from typing import Protocol

from stylepilot.domain.models import (
    ActuatorCalibrationManifest,
    DevelopSettings,
    DevelopSnapshotRef,
    EditPlan,
    PhotoMetadata,
    PhotoMetrics,
    PhotoRef,
    RenderVerificationReport,
    SceneAnalysis,
    StyleProfile,
    SuitabilityReport,
)


class SceneAnalysisError(RuntimeError):
    """A semantic provider could not return a valid scene analysis."""


class LightroomBridge(Protocol):
    """Operations that the Lua plugin must expose to the agent runtime."""

    async def get_selected_photos(self) -> tuple[PhotoRef, ...]: ...

    async def get_photo_metadata(self, photo_id: str) -> PhotoMetadata: ...

    async def render_analysis_preview(self, photo_id: str) -> Path: ...

    async def create_virtual_copy(self, photo_id: str, copy_name: str) -> PhotoRef: ...

    async def request_write_approval(
        self,
        photo: PhotoRef,
        style: StyleProfile,
        suitability: SuitabilityReport,
        plan: EditPlan,
    ) -> bool: ...

    async def request_calibration_approval(
        self,
        photos: tuple[PhotoRef, ...],
        manifest: ActuatorCalibrationManifest,
    ) -> bool: ...

    async def apply_develop_settings(
        self,
        photo_id: str,
        settings: DevelopSettings,
        history_name: str,
    ) -> DevelopSnapshotRef: ...

    async def restore_develop_snapshot(self, photo_id: str, snapshot_name: str) -> None: ...


class PhotoAnalyzer(Protocol):
    def analyze(self, preview_path: Path, metadata: PhotoMetadata) -> PhotoMetrics: ...


class SceneAnalyzer(Protocol):
    def analyze(self, image_path: Path) -> SceneAnalysis: ...


class SuitabilityEngine(Protocol):
    def evaluate(self, metrics: PhotoMetrics, style: StyleProfile) -> SuitabilityReport: ...


class EditPlanner(Protocol):
    def plan(
        self,
        metadata: PhotoMetadata,
        metrics: PhotoMetrics,
        style: StyleProfile,
        strength: float,
    ) -> EditPlan: ...


class PostconditionVerifier(Protocol):
    def verify(
        self,
        before: PhotoMetrics,
        after: PhotoMetrics,
        style: StyleProfile,
    ) -> RenderVerificationReport: ...
