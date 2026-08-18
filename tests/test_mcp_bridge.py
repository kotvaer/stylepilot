from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image

from stylepilot.adapters.lightroom import (
    LightroomApprovalTimeoutError,
    LightroomCapabilityError,
    LightroomConnectionError,
    LightroomProtocolError,
    LightroomRollbackError,
    McpLightroomBridge,
)
from stylepilot.adapters.lightroom.mcp_bridge import REQUIRED_LIGHTROOM_TOOLS
from stylepilot.domain.models import (
    ActuatorCalibrationManifest,
    DevelopSettings,
    EditPlan,
    PhotoRef,
    StyleProfile,
    SuitabilityReport,
)


@dataclass
class FakeMcpToolClient:
    responses: dict[str, dict[str, object] | Exception]
    tools: frozenset[str] = REQUIRED_LIGHTROOM_TOOLS
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    create_export: bool = True
    server_name: str | None = "lightroom-mcp-server"
    server_version: str | None = "0.9.0"
    protocol_version: str | None = "2025-11-25"

    async def list_tools(self) -> frozenset[str]:
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, arguments))
        if name == "export_photos" and self.create_export:
            destination = Path(str(arguments["destination"]))
            Image.new("RGB", (40, 30), (120, 130, 140)).save(destination / "preview.jpg")
        response = self.responses[name]
        if isinstance(response, Exception):
            raise response
        payload = copy.deepcopy(response)
        if name == "create_develop_snapshot":
            snapshot = payload.get("snapshot")
            if isinstance(snapshot, dict):
                snapshot["name"] = arguments["snapshot_name"]
        if name == "restore_develop_snapshot":
            payload["snapshot_name"] = arguments["snapshot_name"]
        if name in {
            "request_stylepilot_approval",
            "request_stylepilot_calibration_approval",
            "get_stylepilot_approval",
            "cancel_stylepilot_approval",
        }:
            payload["request_id"] = arguments["request_id"]
        return payload


def selection_payload(*, has_more: bool = False) -> dict[str, object]:
    return {
        "count": 1,
        "has_more": has_more,
        "photos": [
            {
                "id": 37246,
                "path": "/photos/source.CR3",
                "filename": "source.CR3",
            }
        ],
    }


def test_maps_upstream_selection_and_metadata_contract(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "get_selected_photos": selection_payload(),
            "get_photo_metadata": {
                "id": "photo-1",
                "path": "/photos/source.CR3",
                "filename": "source.CR3",
                "isoSpeedRating": "ISO 1,600",
                "dimensions": "6,000 x 4,000",
                "developSettings": {
                    "exposure": 0.35,
                    "highlights": -20,
                    "vibrance": 12,
                },
            },
        }
    )
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)

    photos = asyncio.run(bridge.get_selected_photos())
    metadata = asyncio.run(bridge.get_photo_metadata(photos[0].id))

    assert photos[0].id == "37246"
    assert photos[0].filename == "source.CR3"
    assert metadata.iso == 1600
    assert (metadata.width, metadata.height) == (6000, 4000)
    assert metadata.develop_settings.values == {
        "Exposure2012": 0.35,
        "Highlights2012": -20.0,
        "Vibrance": 12.0,
    }


def test_rejects_selection_larger_than_supported_page(tmp_path: Path) -> None:
    client = FakeMcpToolClient(responses={"get_selected_photos": selection_payload(has_more=True)})
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path, connect_attempts=1)

    with pytest.raises(LightroomProtocolError, match="More than 100"):
        asyncio.run(bridge.get_selected_photos())


def test_exports_one_lightroom_rendered_preview(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "export_photos": {
                "success": True,
                "exported": 1,
                "destination": str(tmp_path),
            }
        }
    )
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)

    preview = asyncio.run(bridge.render_analysis_preview("photo-1"))

    assert preview.name == "preview.jpg"
    export_call = client.calls[0]
    assert export_call[0] == "export_photos"
    assert export_call[1]["width"] == 2048
    assert export_call[1]["format"] == "jpeg"


def test_rejects_export_without_a_rendered_file(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "export_photos": {
                "success": True,
                "exported": 1,
                "destination": str(tmp_path),
            }
        },
        create_export=False,
    )
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)

    with pytest.raises(LightroomProtocolError, match="found 0"):
        asyncio.run(bridge.render_analysis_preview("photo-1"))


def test_creates_virtual_copy_and_returns_new_photo(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "create_virtual_copy": {
                "success": True,
                "source_photo_id": 37246,
                "virtual_copy": {
                    "id": 37247,
                    "path": "/photos/source.CR3",
                    "filename": "source.CR3",
                    "copy_name": "StylePilot Preview",
                },
            }
        }
    )
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)

    virtual_copy = asyncio.run(bridge.create_virtual_copy("37246", "StylePilot Preview"))

    assert virtual_copy.id == "37247"
    assert client.calls == [
        (
            "create_virtual_copy",
            {"photo_id": "37246", "copy_name": "StylePilot Preview"},
        )
    ]


def test_apply_serializes_validated_settings(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "create_virtual_copy": {
                "success": True,
                "source_photo_id": "photo-1",
                "virtual_copy": {
                    "id": "virtual-1",
                    "path": "/photos/source.CR3",
                    "filename": "source.CR3",
                },
            },
            "create_develop_snapshot": {
                "success": True,
                "photo_id": "virtual-1",
                "snapshot": {
                    "id": "snapshot-1",
                    "global_id": "global-snapshot-1",
                    "name": "replaced-by-fake",
                },
            },
            "set_stylepilot_develop_settings": {
                "success": True,
                "photo_id": "virtual-1",
            },
        }
    )
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)

    virtual_copy = asyncio.run(bridge.create_virtual_copy("photo-1", "StylePilot Preview"))
    snapshot = asyncio.run(
        bridge.apply_develop_settings(
            virtual_copy.id,
            DevelopSettings(values={"Exposure2012": 0.5}),
            "StylePilot Preview",
        )
    )

    assert snapshot.id == "snapshot-1"
    assert snapshot.photo_id == "virtual-1"
    assert snapshot.name.startswith("StylePilot Before ")
    assert client.calls[0:1] == [
        (
            "create_virtual_copy",
            {"photo_id": "photo-1", "copy_name": "StylePilot Preview"},
        )
    ]
    assert client.calls[1] == (
        "create_develop_snapshot",
        {"photo_id": "virtual-1", "snapshot_name": snapshot.name},
    )
    assert client.calls[2] == (
        "set_stylepilot_develop_settings",
        {
            "photo_id": "virtual-1",
            "history_name": "StylePilot Preview",
            "settings": {"Exposure2012": 0.5},
        },
    )


def test_rejects_original_write_before_mcp_call(tmp_path: Path) -> None:
    client = FakeMcpToolClient(responses={})
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)

    with pytest.raises(LightroomCapabilityError, match="Refusing to write photo source-1"):
        asyncio.run(
            bridge.apply_develop_settings(
                "source-1",
                DevelopSettings(values={"Exposure2012": 0.5}),
                "Unsafe write",
            )
        )

    assert client.calls == []


def test_requests_native_panel_approval_and_polls_request_id(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "request_stylepilot_approval": {
                "success": True,
                "request_id": "replaced-by-fake",
                "status": "pending",
            },
            "get_stylepilot_approval": {
                "success": True,
                "request_id": "replaced-by-fake",
                "status": "approved",
                "reason": "user_approved",
            },
        }
    )
    bridge = McpLightroomBridge(
        client=client,
        preview_root=tmp_path,
        approval_poll_interval_seconds=0,
    )
    photo = PhotoRef(id="photo-1", path=Path("/photos/source.CR3"), filename="source.CR3")
    style = StyleProfile(
        id="bright-clean",
        name="Bright Clean",
        target_luminance_median=65,
        target_mean_chroma=22,
    )
    suitability = SuitabilityReport(
        score=85,
        eligible=True,
        recommended_strength=0.9,
        reasons=("Brightness shift is material.",),
    )
    plan = EditPlan(
        source_photo_id=photo.id,
        style_profile_id=style.id,
        strength=0.9,
        settings=DevelopSettings(values={"Exposure2012": 1.2}),
        rationale=("Lift exposure.",),
    )

    approved = asyncio.run(bridge.request_write_approval(photo, style, suitability, plan))

    assert approved is True
    request_id = client.calls[0][1]["request_id"]
    assert client.calls == [
        (
            "request_stylepilot_approval",
            {
                "request_id": request_id,
                "photo_id": "photo-1",
                "filename": "source.CR3",
                "style_name": "Bright Clean",
                "suitability_score": 85.0,
                "recommended_strength": 0.9,
                "settings": {"Exposure2012": 1.2},
                "risks": ["Brightness shift is material."],
            },
        ),
        ("get_stylepilot_approval", {"request_id": request_id}),
    ]


def test_requests_one_native_approval_for_bounded_calibration(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "request_stylepilot_calibration_approval": {
                "success": True,
                "request_id": "replaced-by-fake",
                "status": "approved",
            }
        }
    )
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)
    photos = (
        PhotoRef(id="photo-1", path=Path("/photos/one.CR3"), filename="one.CR3"),
        PhotoRef(id="photo-2", path=Path("/photos/two.CR3"), filename="two.CR3"),
    )
    manifest = ActuatorCalibrationManifest.model_validate(
        {
            "id": "tone-smoke",
            "name": "Tone smoke",
            "baseline_repeats": 3,
            "parameters": [
                {"parameter": "Exposure2012", "values": [-0.5, 0.5]},
                {"parameter": "Contrast2012", "values": [-20, 0, 20]},
            ],
        }
    )

    approved = asyncio.run(bridge.request_calibration_approval(photos, manifest))

    assert approved is True
    tool_name, arguments = client.calls[0]
    assert tool_name == "request_stylepilot_calibration_approval"
    assert arguments["photo_ids"] == ["photo-1", "photo-2"]
    assert arguments["sample_count"] == 10
    assert arguments["render_count"] == 28
    assert arguments["parameters"] == [
        {"parameter": "Exposure2012", "values": [-0.5, 0.5]},
        {"parameter": "Contrast2012", "values": [-20.0, 0.0, 20.0]},
    ]


def test_native_panel_approval_times_out_without_writing(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "request_stylepilot_approval": {
                "success": True,
                "request_id": "replaced-by-fake",
                "status": "pending",
            },
            "get_stylepilot_approval": {
                "success": True,
                "request_id": "replaced-by-fake",
                "status": "pending",
            },
            "cancel_stylepilot_approval": {
                "success": True,
                "request_id": "replaced-by-fake",
                "status": "rejected",
                "reason": "client_cancelled",
            },
        }
    )
    bridge = McpLightroomBridge(
        client=client,
        preview_root=tmp_path,
        approval_timeout_seconds=0,
        approval_poll_interval_seconds=0,
    )
    photo = PhotoRef(id="photo-1", path=Path("/photos/source.CR3"), filename="source.CR3")
    style = StyleProfile(
        id="bright-clean",
        name="Bright Clean",
        target_luminance_median=65,
        target_mean_chroma=22,
    )
    suitability = SuitabilityReport(
        score=85,
        eligible=True,
        recommended_strength=0.9,
        reasons=("Safe.",),
    )
    plan = EditPlan(
        source_photo_id=photo.id,
        style_profile_id=style.id,
        strength=0.9,
        settings=DevelopSettings(values={"Exposure2012": 1.2}),
        rationale=("Lift exposure.",),
    )

    with pytest.raises(
        LightroomApprovalTimeoutError,
        match="stale Lightroom panel was cancelled",
    ):
        asyncio.run(bridge.request_write_approval(photo, style, suitability, plan))

    assert [name for name, _ in client.calls] == [
        "request_stylepilot_approval",
        "get_stylepilot_approval",
        "cancel_stylepilot_approval",
    ]


def test_native_panel_timeout_reports_failed_stale_request_cleanup(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "request_stylepilot_approval": {
                "success": True,
                "request_id": "replaced-by-fake",
                "status": "pending",
            },
            "get_stylepilot_approval": {
                "success": True,
                "request_id": "replaced-by-fake",
                "status": "pending",
            },
            "cancel_stylepilot_approval": LightroomProtocolError("cancel failed"),
        }
    )
    bridge = McpLightroomBridge(
        client=client,
        preview_root=tmp_path,
        approval_timeout_seconds=0,
        approval_poll_interval_seconds=0,
    )
    photo = PhotoRef(id="photo-1", path=Path("/photos/source.CR3"), filename="source.CR3")
    style = StyleProfile(
        id="bright-clean",
        name="Bright Clean",
        target_luminance_median=65,
        target_mean_chroma=22,
    )
    suitability = SuitabilityReport(
        score=85,
        eligible=True,
        recommended_strength=0.9,
        reasons=("Safe.",),
    )
    plan = EditPlan(
        source_photo_id=photo.id,
        style_profile_id=style.id,
        strength=0.9,
        settings=DevelopSettings(values={"Exposure2012": 1.2}),
        rationale=("Lift exposure.",),
    )

    with pytest.raises(LightroomApprovalTimeoutError, match="could not be cancelled"):
        asyncio.run(bridge.request_write_approval(photo, style, suitability, plan))


def test_approval_won_at_timeout_cancellation_boundary_is_honoured(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "request_stylepilot_approval": {
                "success": True,
                "request_id": "replaced-by-fake",
                "status": "pending",
            },
            "get_stylepilot_approval": {
                "success": True,
                "request_id": "replaced-by-fake",
                "status": "pending",
            },
            "cancel_stylepilot_approval": {
                "success": True,
                "request_id": "replaced-by-fake",
                "status": "approved",
                "reason": "user_approved",
            },
        }
    )
    bridge = McpLightroomBridge(
        client=client,
        preview_root=tmp_path,
        approval_timeout_seconds=0,
        approval_poll_interval_seconds=0,
    )
    photo = PhotoRef(id="photo-1", path=Path("/photos/source.CR3"), filename="source.CR3")
    style = StyleProfile(
        id="bright-clean",
        name="Bright Clean",
        target_luminance_median=65,
        target_mean_chroma=22,
    )
    suitability = SuitabilityReport(
        score=85,
        eligible=True,
        recommended_strength=0.9,
        reasons=("Safe.",),
    )
    plan = EditPlan(
        source_photo_id=photo.id,
        style_profile_id=style.id,
        strength=0.9,
        settings=DevelopSettings(values={"Exposure2012": 1.2}),
        rationale=("Lift exposure.",),
    )

    approved = asyncio.run(bridge.request_write_approval(photo, style, suitability, plan))

    assert approved is True


def test_rolls_back_named_snapshot_when_guarded_write_fails(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "create_virtual_copy": {
                "success": True,
                "source_photo_id": "photo-1",
                "virtual_copy": {
                    "id": "virtual-1",
                    "path": "/photos/source.CR3",
                    "filename": "source.CR3",
                },
            },
            "create_develop_snapshot": {
                "success": True,
                "photo_id": "virtual-1",
                "snapshot": {"id": "snapshot-1", "name": "replaced-by-fake"},
            },
            "set_stylepilot_develop_settings": LightroomProtocolError("write timed out"),
            "restore_develop_snapshot": {
                "success": True,
                "photo_id": "virtual-1",
                "snapshot_id": "snapshot-1",
                "snapshot_name": "replaced-by-test",
            },
        }
    )
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)
    virtual_copy = asyncio.run(bridge.create_virtual_copy("photo-1", "StylePilot Preview"))

    with pytest.raises(LightroomProtocolError, match="write timed out"):
        asyncio.run(
            bridge.apply_develop_settings(
                virtual_copy.id,
                DevelopSettings(values={"Exposure2012": 0.5}),
                "StylePilot Preview",
            )
        )

    assert [name for name, _ in client.calls] == [
        "create_virtual_copy",
        "create_develop_snapshot",
        "set_stylepilot_develop_settings",
        "restore_develop_snapshot",
    ]


def test_reports_both_apply_and_rollback_failures(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "create_virtual_copy": {
                "success": True,
                "source_photo_id": "photo-1",
                "virtual_copy": {
                    "id": "virtual-1",
                    "path": "/photos/source.CR3",
                    "filename": "source.CR3",
                },
            },
            "create_develop_snapshot": {
                "success": True,
                "photo_id": "virtual-1",
                "snapshot": {"id": "snapshot-1", "name": "replaced-by-fake"},
            },
            "set_stylepilot_develop_settings": LightroomProtocolError("write failed"),
            "restore_develop_snapshot": LightroomProtocolError("rollback failed"),
        }
    )
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)
    virtual_copy = asyncio.run(bridge.create_virtual_copy("photo-1", "StylePilot Preview"))

    with pytest.raises(LightroomRollbackError, match="automatic rollback also failed") as captured:
        asyncio.run(
            bridge.apply_develop_settings(
                virtual_copy.id,
                DevelopSettings(values={"Exposure2012": 0.5}),
                "StylePilot Preview",
            )
        )

    assert str(captured.value.apply_error) == "write failed"
    assert str(captured.value.rollback_error) == "rollback failed"


def test_restores_recovery_token_after_process_restart(tmp_path: Path) -> None:
    snapshot_name = "StylePilot Before persisted-token"
    client = FakeMcpToolClient(
        responses={
            "restore_develop_snapshot": {
                "success": True,
                "photo_id": "virtual-1",
                "snapshot_id": "snapshot-1",
                "snapshot_name": snapshot_name,
            }
        }
    )
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)

    asyncio.run(bridge.restore_recovery_snapshot("virtual-1", snapshot_name))

    assert client.calls == [
        (
            "restore_develop_snapshot",
            {"photo_id": "virtual-1", "snapshot_name": snapshot_name},
        )
    ]


def test_rejects_non_stylepilot_recovery_token(tmp_path: Path) -> None:
    client = FakeMcpToolClient(responses={})
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)

    with pytest.raises(LightroomCapabilityError, match="not a StylePilot recovery"):
        asyncio.run(bridge.restore_recovery_snapshot("virtual-1", "Untrusted Snapshot"))

    assert client.calls == []


def test_diagnose_reports_plugin_offline_and_missing_tools(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "get_selected_photos": LightroomConnectionError("Lightroom plugin not connected")
        },
        tools=REQUIRED_LIGHTROOM_TOOLS - {"export_photos"},
    )
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path, connect_attempts=1)

    status = asyncio.run(bridge.diagnose())

    assert status.plugin_connected is False
    assert status.plugin_error == "Lightroom plugin not connected"
    assert status.missing_tools == ("export_photos",)


def test_diagnose_probes_installed_plugin_cancellation_capability(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "get_selected_photos": selection_payload(),
            "cancel_stylepilot_approval": {
                "success": True,
                "request_id": "replaced-by-fake",
                "status": "unknown",
            },
        }
    )
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)

    status = asyncio.run(bridge.diagnose())

    assert status.plugin_connected is True
    assert status.selected_photo_count == 1
    assert [name for name, _ in client.calls] == [
        "get_selected_photos",
        "cancel_stylepilot_approval",
    ]


def test_diagnose_reports_outdated_plugin_without_cancel_handler(tmp_path: Path) -> None:
    client = FakeMcpToolClient(
        responses={
            "get_selected_photos": selection_payload(),
            "cancel_stylepilot_approval": LightroomProtocolError(
                "Unknown action: cancel_stylepilot_approval"
            ),
        }
    )
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path, connect_attempts=1)

    status = asyncio.run(bridge.diagnose())

    assert status.plugin_connected is False
    assert status.plugin_error == "Unknown action: cancel_stylepilot_approval"


def test_missing_required_tool_fails_before_selection_call(tmp_path: Path) -> None:
    client = FakeMcpToolClient(responses={}, tools=frozenset())
    bridge = McpLightroomBridge(client=client, preview_root=tmp_path)

    with pytest.raises(LightroomCapabilityError, match="missing required tools"):
        asyncio.run(bridge.get_selected_photos())

    assert client.calls == []


def test_retries_only_transient_plugin_startup_race(tmp_path: Path) -> None:
    class StartingMcpToolClient(FakeMcpToolClient):
        attempts = 0

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, object],
        ) -> dict[str, object]:
            self.attempts += 1
            if self.attempts == 1:
                raise LightroomConnectionError("Lightroom plugin not connected")
            return await super().call_tool(name, arguments)

    client = StartingMcpToolClient(responses={"get_selected_photos": selection_payload()})
    bridge = McpLightroomBridge(
        client=client,
        preview_root=tmp_path,
        connect_attempts=2,
        connect_retry_delay_seconds=0,
    )

    photos = asyncio.run(bridge.get_selected_photos())

    assert photos[0].id == "37246"
    assert client.attempts == 2
