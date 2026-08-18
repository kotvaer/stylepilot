from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from stylepilot.domain.models import (
    ActuatorCalibrationManifest,
    DevelopSettings,
    DevelopSnapshotRef,
    EditPlan,
    PhotoMetadata,
    PhotoRef,
    StyleProfile,
    SuitabilityReport,
)


@dataclass(slots=True)
class InMemoryLightroomBridge:
    """Deterministic Lightroom test double used by unit tests and the CLI demo."""

    selected_photos: tuple[PhotoRef, ...]
    metadata_by_id: dict[str, PhotoMetadata]
    preview_by_id: dict[str, Path]
    applied_settings: dict[str, DevelopSettings] = field(default_factory=dict)
    virtual_copies: list[PhotoRef] = field(default_factory=list)
    recovery_snapshots: list[DevelopSnapshotRef] = field(default_factory=list)
    snapshot_settings: dict[str, DevelopSettings] = field(default_factory=dict)
    restored_snapshots: list[str] = field(default_factory=list)
    approval_granted: bool = True
    approval_requests: int = 0

    async def get_selected_photos(self) -> tuple[PhotoRef, ...]:
        return self.selected_photos

    async def get_photo_metadata(self, photo_id: str) -> PhotoMetadata:
        return self.metadata_by_id[photo_id]

    async def render_analysis_preview(self, photo_id: str) -> Path:
        return self.preview_by_id[photo_id]

    async def create_virtual_copy(self, photo_id: str, copy_name: str) -> PhotoRef:
        source = next(photo for photo in self.selected_photos if photo.id == photo_id)
        virtual_copy = PhotoRef(
            id=f"{photo_id}:virtual:{len(self.virtual_copies) + 1}",
            path=source.path,
            filename=f"{source.filename} ({copy_name})",
        )
        self.virtual_copies.append(virtual_copy)
        self.metadata_by_id[virtual_copy.id] = self.metadata_by_id[photo_id].model_copy(
            update={"photo_id": virtual_copy.id}
        )
        self.preview_by_id[virtual_copy.id] = self.preview_by_id[photo_id]
        return virtual_copy

    async def request_write_approval(
        self,
        photo: PhotoRef,
        style: StyleProfile,
        suitability: SuitabilityReport,
        plan: EditPlan,
    ) -> bool:
        del photo, style, suitability, plan
        self.approval_requests += 1
        return self.approval_granted

    async def request_calibration_approval(
        self,
        photos: tuple[PhotoRef, ...],
        manifest: ActuatorCalibrationManifest,
    ) -> bool:
        del photos, manifest
        self.approval_requests += 1
        return self.approval_granted

    async def apply_develop_settings(
        self,
        photo_id: str,
        settings: DevelopSettings,
        history_name: str,
    ) -> DevelopSnapshotRef:
        snapshot = DevelopSnapshotRef(
            id=f"memory-snapshot:{len(self.recovery_snapshots) + 1}",
            photo_id=photo_id,
            name=f"StylePilot Before {len(self.recovery_snapshots) + 1}",
        )
        self.recovery_snapshots.append(snapshot)
        current_settings = self.metadata_by_id[photo_id].develop_settings
        self.snapshot_settings[snapshot.name] = current_settings
        del history_name
        self.applied_settings[photo_id] = settings
        merged_settings = DevelopSettings(values=current_settings.values | settings.values)
        self.metadata_by_id[photo_id] = self.metadata_by_id[photo_id].model_copy(
            update={"develop_settings": merged_settings}
        )
        return snapshot

    async def restore_develop_snapshot(self, photo_id: str, snapshot_name: str) -> None:
        settings = self.snapshot_settings[snapshot_name]
        self.metadata_by_id[photo_id] = self.metadata_by_id[photo_id].model_copy(
            update={"develop_settings": settings}
        )
        self.applied_settings.pop(photo_id, None)
        self.restored_snapshots.append(snapshot_name)
