from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from stylepilot.adapters.lightroom.errors import (
    LightroomApprovalTimeoutError,
    LightroomCapabilityError,
    LightroomConnectionError,
    LightroomProtocolError,
    LightroomRollbackError,
)
from stylepilot.adapters.lightroom.mcp_client import McpToolClient
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

REQUIRED_LIGHTROOM_TOOLS = frozenset(
    {
        "create_virtual_copy",
        "cancel_stylepilot_approval",
        "create_develop_snapshot",
        "export_photos",
        "get_photo_metadata",
        "get_selected_photos",
        "get_stylepilot_approval",
        "request_stylepilot_approval",
        "request_stylepilot_calibration_approval",
        "restore_develop_snapshot",
        "set_stylepilot_develop_settings",
    }
)

_DEVELOP_FIELD_MAP = {
    "exposure": "Exposure2012",
    "contrast": "Contrast2012",
    "highlights": "Highlights2012",
    "shadows": "Shadows2012",
    "whites": "Whites2012",
    "blacks": "Blacks2012",
    "texture": "Texture",
    "clarity": "Clarity2012",
    "dehaze": "Dehaze",
    "vibrance": "Vibrance",
    "saturation": "Saturation",
}

_IMAGE_SUFFIXES = frozenset({".jpeg", ".jpg", ".png", ".tif", ".tiff"})


class _McpModel(BaseModel):
    # Lightroom's localIdentifier is numeric in production even though the
    # upstream MCP tool schema describes photo IDs as strings.
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)


class _SelectedPhoto(_McpModel):
    id: str
    path: Path
    filename: str


class _SelectionResponse(_McpModel):
    count: int = Field(ge=0)
    photos: tuple[_SelectedPhoto, ...]
    has_more: bool = False


class _DevelopMetadata(_McpModel):
    exposure: float | None = None
    contrast: float | None = None
    highlights: float | None = None
    shadows: float | None = None
    whites: float | None = None
    blacks: float | None = None
    texture: float | None = None
    clarity: float | None = None
    dehaze: float | None = None
    vibrance: float | None = None
    saturation: float | None = None


class _MetadataResponse(_McpModel):
    id: str
    path: Path
    filename: str
    iso_speed_rating: str | int | None = Field(default=None, alias="isoSpeedRating")
    dimensions: str | None = None
    develop_settings: _DevelopMetadata = Field(
        default_factory=_DevelopMetadata,
        alias="developSettings",
    )


class _ExportResponse(_McpModel):
    success: bool
    exported: int = Field(ge=0)
    destination: Path


class _CreateVirtualCopyResponse(_McpModel):
    success: bool
    source_photo_id: str
    virtual_copy: _SelectedPhoto


class _DevelopSnapshot(_McpModel):
    id: str
    global_id: str | None = None
    name: str


class _CreateDevelopSnapshotResponse(_McpModel):
    success: bool
    photo_id: str
    snapshot: _DevelopSnapshot


class _RestoreDevelopSnapshotResponse(_McpModel):
    success: bool
    photo_id: str
    snapshot_id: str
    snapshot_name: str


class _ApprovalResponse(_McpModel):
    success: bool
    request_id: str
    status: Literal["pending", "approved", "rejected", "unknown"]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class LightroomMcpStatus:
    server_name: str | None
    server_version: str | None
    protocol_version: str | None
    tools: tuple[str, ...]
    missing_tools: tuple[str, ...]
    plugin_connected: bool
    selected_photo_count: int | None = None
    plugin_error: str | None = None


@dataclass(slots=True)
class McpLightroomBridge:
    """Lightroom bridge backed by Automaat/lightroom-mcp's public tools."""

    client: McpToolClient
    preview_root: Path = Path(".stylepilot/previews")
    preview_long_edge: int = 2048
    preview_quality: int = 90
    connect_attempts: int = 20
    connect_retry_delay_seconds: float = 0.5
    approval_timeout_seconds: float = 180.0
    approval_poll_interval_seconds: float = 0.5
    _authorized_write_targets: set[str] = field(default_factory=set, init=False, repr=False)
    _authorized_snapshots: dict[str, set[str]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    async def diagnose(self) -> LightroomMcpStatus:
        tools = await self.client.list_tools()
        missing = REQUIRED_LIGHTROOM_TOOLS - tools
        selected_count: int | None = None
        plugin_error: str | None = None
        try:
            payload = await self._call_tool(
                "get_selected_photos",
                {"limit": 1, "offset": 0},
            )
            selected_count = _SelectionResponse.model_validate(payload).count
            capability_request_id = f"doctor-{uuid4().hex}"
            capability_payload = await self._call_tool(
                "cancel_stylepilot_approval",
                {"request_id": capability_request_id},
            )
            capability = _ApprovalResponse.model_validate(capability_payload)
            if (
                not capability.success
                or capability.request_id != capability_request_id
                or capability.status != "unknown"
            ):
                msg = "Lightroom plugin returned an invalid approval-cancellation probe."
                raise LightroomProtocolError(msg)
        except Exception as error:  # diagnosis intentionally converts boundary errors to data
            plugin_error = str(error)

        return LightroomMcpStatus(
            server_name=self.client.server_name,
            server_version=self.client.server_version,
            protocol_version=self.client.protocol_version,
            tools=tuple(sorted(tools)),
            missing_tools=tuple(sorted(missing)),
            plugin_connected=plugin_error is None,
            selected_photo_count=selected_count,
            plugin_error=plugin_error,
        )

    async def get_selected_photos(self) -> tuple[PhotoRef, ...]:
        await self._require_capabilities()
        payload = await self._call_tool(
            "get_selected_photos",
            {"limit": 100, "offset": 0},
        )
        response = _SelectionResponse.model_validate(payload)
        if response.has_more:
            msg = "More than 100 Lightroom photos are selected; select a smaller working set."
            raise LightroomProtocolError(msg)
        return tuple(
            PhotoRef(id=photo.id, path=photo.path, filename=photo.filename)
            for photo in response.photos
        )

    async def get_photo_metadata(self, photo_id: str) -> PhotoMetadata:
        payload = await self._call_tool("get_photo_metadata", {"photo_id": photo_id})
        response = _MetadataResponse.model_validate(payload)
        width, height = _parse_dimensions(response.dimensions)
        raw_settings = response.develop_settings.model_dump()
        settings = {
            sdk_key: float(value)
            for field, sdk_key in _DEVELOP_FIELD_MAP.items()
            if (value := raw_settings[field]) is not None
        }
        return PhotoMetadata(
            photo_id=response.id,
            iso=_parse_iso(response.iso_speed_rating),
            width=width,
            height=height,
            develop_settings=DevelopSettings(values=settings),
        )

    async def render_analysis_preview(self, photo_id: str) -> Path:
        export_directory = self.preview_root / uuid4().hex
        export_directory.mkdir(parents=True, exist_ok=False)
        payload = await self._call_tool(
            "export_photos",
            {
                "photo_ids": [photo_id],
                "destination": str(export_directory.resolve()),
                "format": "jpeg",
                "quality": self.preview_quality,
                "width": self.preview_long_edge,
                "height": self.preview_long_edge,
            },
        )
        response = _ExportResponse.model_validate(payload)
        if not response.success or response.exported != 1:
            msg = f"Expected Lightroom to export one preview, got {response.exported}."
            raise LightroomProtocolError(msg)

        rendered = tuple(
            path
            for path in export_directory.rglob("*")
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
        )
        if len(rendered) != 1:
            msg = f"Expected one rendered preview in {export_directory}, found {len(rendered)}."
            raise LightroomProtocolError(msg)
        return rendered[0]

    async def create_virtual_copy(self, photo_id: str, copy_name: str) -> PhotoRef:
        await self._require_capabilities()
        _require_bounded_label(copy_name, "copy_name")
        payload = await self._call_tool(
            "create_virtual_copy",
            {"photo_id": photo_id, "copy_name": copy_name},
        )
        response = _CreateVirtualCopyResponse.model_validate(payload)
        if not response.success or response.source_photo_id != photo_id:
            msg = f"Lightroom did not confirm a virtual copy for source photo {photo_id}."
            raise LightroomProtocolError(msg)
        copy = response.virtual_copy
        if copy.id == photo_id:
            msg = "Lightroom returned the source photo instead of a new virtual copy."
            raise LightroomProtocolError(msg)
        self._authorized_write_targets.add(copy.id)
        return PhotoRef(id=copy.id, path=copy.path, filename=copy.filename)

    async def request_write_approval(
        self,
        photo: PhotoRef,
        style: StyleProfile,
        suitability: SuitabilityReport,
        plan: EditPlan,
    ) -> bool:
        await self._require_capabilities()
        request_id = uuid4().hex
        return await self._request_approval(
            "request_stylepilot_approval",
            {
                "request_id": request_id,
                "photo_id": photo.id,
                "filename": photo.filename,
                "style_name": style.name,
                "suitability_score": suitability.score,
                "recommended_strength": suitability.recommended_strength,
                "settings": plan.settings.values,
                "risks": list(suitability.reasons),
            },
        )

    async def request_calibration_approval(
        self,
        photos: tuple[PhotoRef, ...],
        manifest: ActuatorCalibrationManifest,
    ) -> bool:
        await self._require_capabilities()
        if not photos or len(photos) > 20:
            msg = "A Lightroom calibration approval requires between 1 and 20 photos."
            raise LightroomCapabilityError(msg)
        request_id = uuid4().hex
        sample_count = len(photos) * manifest.sample_points
        render_count = len(photos) * (manifest.baseline_repeats + 1 + (2 * manifest.sample_points))
        return await self._request_approval(
            "request_stylepilot_calibration_approval",
            {
                "request_id": request_id,
                "experiment_id": manifest.id,
                "experiment_name": manifest.name,
                "photo_ids": [photo.id for photo in photos],
                "filenames": [photo.filename for photo in photos],
                "baseline_repeats": manifest.baseline_repeats,
                "sample_count": sample_count,
                "render_count": render_count,
                "parameters": [
                    {
                        "parameter": sweep.parameter,
                        "values": list(sweep.values),
                    }
                    for sweep in manifest.parameters
                ],
                "risks": [
                    "Creates one Lightroom virtual copy per selected photo.",
                    "Applies each declared value temporarily and restores a recovery snapshot.",
                    "Leaves virtual copies and named recovery snapshots in the catalog for audit.",
                ],
            },
        )

    async def _request_approval(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> bool:
        request_id = str(arguments["request_id"])
        payload = await self._call_tool(tool_name, arguments)
        opened = _ApprovalResponse.model_validate(payload)
        if not opened.success or opened.request_id != request_id or opened.status == "unknown":
            msg = "Lightroom did not open the request-bound StylePilot approval panel."
            raise LightroomProtocolError(msg)
        if opened.status == "approved":
            return True
        if opened.status == "rejected":
            return False

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, self.approval_timeout_seconds)
        while True:
            decision_payload = await self._call_tool(
                "get_stylepilot_approval",
                {"request_id": request_id},
            )
            decision = _ApprovalResponse.model_validate(decision_payload)
            if not decision.success or decision.request_id != request_id:
                msg = "Lightroom returned an approval decision for the wrong request."
                raise LightroomProtocolError(msg)
            if decision.status == "approved":
                return True
            if decision.status == "rejected":
                return False
            if decision.status == "unknown":
                msg = "The StylePilot approval request disappeared before a decision."
                raise LightroomProtocolError(msg)
            if loop.time() >= deadline:
                try:
                    cancelled_payload = await self._call_tool(
                        "cancel_stylepilot_approval",
                        {"request_id": request_id},
                    )
                    cancelled = _ApprovalResponse.model_validate(cancelled_payload)
                    if not cancelled.success or cancelled.request_id != request_id:
                        msg = (
                            "Lightroom did not confirm cancellation of timed-out "
                            f"approval request {request_id}."
                        )
                        raise LightroomProtocolError(msg)
                    if cancelled.status == "approved":
                        return True
                    if cancelled.status not in {"rejected", "unknown"}:
                        msg = (
                            "Lightroom left timed-out approval request "
                            f"{request_id} in state {cancelled.status!r}."
                        )
                        raise LightroomProtocolError(msg)
                except Exception as cancel_error:
                    msg = (
                        f"StylePilot approval timed out for request {request_id}, and the "
                        f"stale Lightroom panel could not be cancelled: {cancel_error}"
                    )
                    raise LightroomApprovalTimeoutError(msg) from cancel_error
                msg = (
                    f"StylePilot approval timed out for request {request_id}; "
                    "the stale Lightroom panel was cancelled."
                )
                raise LightroomApprovalTimeoutError(msg)
            await asyncio.sleep(max(0.0, self.approval_poll_interval_seconds))

    async def apply_develop_settings(
        self,
        photo_id: str,
        settings: DevelopSettings,
        history_name: str,
    ) -> DevelopSnapshotRef:
        _require_bounded_label(history_name, "history_name")
        if photo_id not in self._authorized_write_targets:
            msg = (
                f"Refusing to write photo {photo_id}: it was not created as a virtual copy "
                "by this StylePilot session."
            )
            raise LightroomCapabilityError(msg)
        snapshot = await self._create_develop_snapshot(photo_id)
        try:
            payload = await self._call_tool(
                "set_stylepilot_develop_settings",
                {
                    "photo_id": photo_id,
                    "history_name": history_name,
                    "settings": settings.values,
                },
            )
            if payload.get("success") is not True or str(payload.get("photo_id")) != photo_id:
                msg = f"Lightroom did not confirm guarded Develop settings for photo {photo_id}."
                raise LightroomProtocolError(msg)
        except Exception as apply_error:
            try:
                await self.restore_develop_snapshot(photo_id, snapshot.name)
            except Exception as rollback_error:
                raise LightroomRollbackError(
                    photo_id,
                    apply_error,
                    rollback_error,
                ) from rollback_error
            raise
        return snapshot

    async def restore_develop_snapshot(self, photo_id: str, snapshot_name: str) -> None:
        if photo_id not in self._authorized_write_targets:
            msg = f"Refusing to restore photo {photo_id}: it is not an authorized virtual copy."
            raise LightroomCapabilityError(msg)
        if snapshot_name not in self._authorized_snapshots.get(photo_id, set()):
            msg = (
                f"Refusing to restore snapshot {snapshot_name!r}: it was not created "
                "by this StylePilot session."
            )
            raise LightroomCapabilityError(msg)

        await self._restore_develop_snapshot(photo_id, snapshot_name)

    async def restore_recovery_snapshot(self, photo_id: str, snapshot_name: str) -> None:
        """Restore a prior CLI run using its explicit recovery token.

        This path intentionally survives process restarts. The name prefix is
        part of the recovery token, while the Lua boundary independently
        verifies that ``photo_id`` is a virtual copy before applying it.
        """

        await self._require_capabilities()
        _require_stylepilot_snapshot_name(snapshot_name)
        await self._restore_develop_snapshot(photo_id, snapshot_name)

    async def _restore_develop_snapshot(self, photo_id: str, snapshot_name: str) -> None:

        payload = await self._call_tool(
            "restore_develop_snapshot",
            {"photo_id": photo_id, "snapshot_name": snapshot_name},
        )
        response = _RestoreDevelopSnapshotResponse.model_validate(payload)
        if (
            not response.success
            or response.photo_id != photo_id
            or response.snapshot_name != snapshot_name
        ):
            msg = f"Lightroom did not confirm snapshot rollback for photo {photo_id}."
            raise LightroomProtocolError(msg)

    async def _create_develop_snapshot(self, photo_id: str) -> DevelopSnapshotRef:
        snapshot_name = f"StylePilot Before {uuid4().hex}"
        payload = await self._call_tool(
            "create_develop_snapshot",
            {"photo_id": photo_id, "snapshot_name": snapshot_name},
        )
        response = _CreateDevelopSnapshotResponse.model_validate(payload)
        if (
            not response.success
            or response.photo_id != photo_id
            or response.snapshot.name != snapshot_name
        ):
            msg = f"Lightroom did not confirm a recovery snapshot for photo {photo_id}."
            raise LightroomProtocolError(msg)
        snapshot = DevelopSnapshotRef(
            id=response.snapshot.id,
            photo_id=photo_id,
            name=response.snapshot.name,
        )
        self._authorized_snapshots.setdefault(photo_id, set()).add(snapshot.name)
        return snapshot

    async def _require_capabilities(self) -> None:
        tools = await self.client.list_tools()
        missing = REQUIRED_LIGHTROOM_TOOLS - tools
        if missing:
            msg = f"Lightroom MCP server is missing required tools: {', '.join(sorted(missing))}."
            raise LightroomCapabilityError(msg)

    async def _call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        attempts = max(1, self.connect_attempts)
        for attempt in range(attempts):
            try:
                return await self.client.call_tool(name, arguments)
            except LightroomConnectionError as error:
                is_startup_race = "plugin not connected" in str(error).lower()
                if not is_startup_race or attempt == attempts - 1:
                    raise
                await asyncio.sleep(max(0.0, self.connect_retry_delay_seconds))
        msg = "Unreachable Lightroom MCP retry state."
        raise RuntimeError(msg)


def _parse_iso(value: str | int | None) -> int | None:
    if isinstance(value, int):
        return value if value >= 1 else None
    if value is None:
        return None
    digits = "".join(re.findall(r"\d", value))
    return int(digits) if digits and int(digits) >= 1 else None


def _parse_dimensions(value: str | None) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    numbers = [int(number) for number in re.findall(r"\d+", value.replace(",", ""))]
    if len(numbers) < 2 or numbers[0] < 1 or numbers[1] < 1:
        return None, None
    return numbers[0], numbers[1]


def _require_bounded_label(value: str, name: str) -> None:
    if not value or len(value) > 255:
        msg = f"{name} must contain between 1 and 255 characters."
        raise LightroomCapabilityError(msg)


def _require_stylepilot_snapshot_name(value: str) -> None:
    _require_bounded_label(value, "snapshot_name")
    if not value.startswith("StylePilot Before "):
        msg = "snapshot_name is not a StylePilot recovery snapshot."
        raise LightroomCapabilityError(msg)
