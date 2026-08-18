from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from stylepilot.adapters.lightroom import InMemoryLightroomBridge
from stylepilot.application.probing import (
    LightroomParameterProbe,
    ParameterProbeRecoveryError,
)
from stylepilot.domain.models import (
    DevelopSettings,
    ParameterProbeRequest,
    ParameterProbeStatus,
    PhotoMetadata,
    PhotoMetrics,
    PhotoRef,
)


def metrics(*, luminance: float, contrast: float, chroma: float) -> PhotoMetrics:
    return PhotoMetrics(
        luminance_median=luminance,
        luminance_contrast=contrast,
        mean_chroma=chroma,
        mean_lab_a=1.0,
        mean_lab_b=2.0,
        shadow_clip_ratio=0.001,
        highlight_clip_ratio=0.002,
        noise_risk=0.1,
    )


@dataclass
class SequenceAnalyzer:
    results: list[PhotoMetrics]
    calls: list[tuple[Path, str]] = field(default_factory=list)

    def analyze(self, preview_path: Path, metadata: PhotoMetadata) -> PhotoMetrics:
        self.calls.append((preview_path, metadata.photo_id))
        return self.results.pop(0)


def build_bridge(tmp_path: Path) -> InMemoryLightroomBridge:
    preview = tmp_path / "preview.jpg"
    Image.new("RGB", (32, 24), (120, 130, 140)).save(preview)
    photo = PhotoRef(id="photo-1", path=preview, filename=preview.name)
    return InMemoryLightroomBridge(
        selected_photos=(photo,),
        metadata_by_id={
            photo.id: PhotoMetadata(
                photo_id=photo.id,
                develop_settings=DevelopSettings(values={"Contrast2012": 5.0}),
            )
        },
        preview_by_id={photo.id: preview},
    )


def test_parameter_probe_applies_measures_and_restores_virtual_copy(tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)
    analyzer = SequenceAnalyzer(
        [
            metrics(luminance=50, contrast=30, chroma=15),
            metrics(luminance=51, contrast=42, chroma=15.5),
            metrics(luminance=50.1, contrast=30.2, chroma=15.0),
        ]
    )

    report = asyncio.run(
        LightroomParameterProbe(bridge=bridge, analyzer=analyzer).run(
            ParameterProbeRequest(parameter="Contrast2012", target_value=20)
        )
    )

    assert report.status is ParameterProbeStatus.COMPLETED
    assert report.before_value == 5
    assert report.applied_value == 20
    assert report.restored_value == 5
    assert report.applied_readback_matches is True
    assert report.restoration_readback_matches is True
    assert report.applied_metric_delta is not None
    assert report.applied_metric_delta.luminance_contrast == 12
    assert report.restoration_metric_drift is not None
    assert report.restoration_metric_drift.luminance_contrast == pytest.approx(0.2)
    assert report.probe_photo is not None
    assert report.probe_photo.id != report.source_photo.id
    assert report.recovery_snapshot is not None
    assert bridge.applied_settings == {}
    assert bridge.restored_snapshots == [report.recovery_snapshot.name]


def test_parameter_probe_rejection_never_creates_virtual_copy(tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)
    bridge.approval_granted = False
    analyzer = SequenceAnalyzer([metrics(luminance=50, contrast=30, chroma=15)])

    report = asyncio.run(
        LightroomParameterProbe(bridge=bridge, analyzer=analyzer).run(
            ParameterProbeRequest(parameter="Dehaze", target_value=10)
        )
    )

    assert report.status is ParameterProbeStatus.REJECTED
    assert report.probe_photo is None
    assert bridge.virtual_copies == []
    assert bridge.applied_settings == {}


def test_parameter_probe_restores_when_render_analysis_fails(tmp_path: Path) -> None:
    bridge = build_bridge(tmp_path)

    class FailingAnalyzer:
        calls = 0

        def analyze(self, preview_path: Path, metadata: PhotoMetadata) -> PhotoMetrics:
            del preview_path, metadata
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("synthetic analysis failure")
            return metrics(luminance=50, contrast=30, chroma=15)

    with pytest.raises(RuntimeError, match="synthetic analysis failure"):
        asyncio.run(
            LightroomParameterProbe(bridge=bridge, analyzer=FailingAnalyzer()).run(
                ParameterProbeRequest(parameter="Exposure2012", target_value=0.5)
            )
        )

    assert bridge.applied_settings == {}
    assert len(bridge.restored_snapshots) == 1


def test_parameter_probe_preserves_recovery_token_when_restore_fails(tmp_path: Path) -> None:
    base = build_bridge(tmp_path)

    @dataclass(slots=True)
    class RestoreFailingBridge(InMemoryLightroomBridge):
        async def restore_develop_snapshot(self, photo_id: str, snapshot_name: str) -> None:
            del photo_id, snapshot_name
            raise RuntimeError("synthetic restore failure")

    bridge = RestoreFailingBridge(
        selected_photos=base.selected_photos,
        metadata_by_id=base.metadata_by_id,
        preview_by_id=base.preview_by_id,
    )
    analyzer = SequenceAnalyzer(
        [
            metrics(luminance=50, contrast=30, chroma=15),
            metrics(luminance=51, contrast=40, chroma=15),
        ]
    )

    with pytest.raises(ParameterProbeRecoveryError, match="lightroom rollback") as captured:
        asyncio.run(
            LightroomParameterProbe(bridge=bridge, analyzer=analyzer).run(
                ParameterProbeRequest(parameter="Contrast2012", target_value=20)
            )
        )

    assert captured.value.photo_id.startswith("photo-1:virtual:")
    assert captured.value.snapshot_name.startswith("StylePilot Before ")
    assert str(captured.value.restore_error) == "synthetic restore failure"


@pytest.mark.parametrize(
    ("parameter", "value", "message"),
    [
        ("ImaginarySlider", 1.0, "Unsupported Lightroom Develop parameter"),
        ("Exposure2012", 6.0, "must be between"),
        ("Exposure2012", float("inf"), "must be a finite number"),
    ],
)
def test_parameter_probe_request_rejects_unsafe_values(
    parameter: str,
    value: float,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ParameterProbeRequest(parameter=parameter, target_value=value)
