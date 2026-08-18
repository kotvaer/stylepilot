from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from stylepilot.adapters.lightroom import InMemoryLightroomBridge
from stylepilot.application.calibration import LightroomActuatorCalibrator
from stylepilot.domain.models import (
    ActuatorCalibrationManifest,
    CalibrationPointStatus,
    CalibrationRunStatus,
    DevelopSettings,
    PhotoMetadata,
    PhotoMetrics,
    PhotoRef,
)


def metrics(luminance: float, contrast: float = 30.0) -> PhotoMetrics:
    return PhotoMetrics(
        luminance_median=luminance,
        luminance_contrast=contrast,
        mean_chroma=15.0,
        mean_lab_a=1.0,
        mean_lab_b=2.0,
        shadow_clip_ratio=0.001,
        highlight_clip_ratio=0.002,
        noise_risk=0.1,
    )


@dataclass
class SequenceAnalyzer:
    results: list[PhotoMetrics | Exception]
    calls: list[str] = field(default_factory=list)

    def analyze(self, preview_path: Path, metadata: PhotoMetadata) -> PhotoMetrics:
        del preview_path
        self.calls.append(metadata.photo_id)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def manifest() -> ActuatorCalibrationManifest:
    return ActuatorCalibrationManifest.model_validate(
        {
            "id": "contrast-smoke",
            "name": "Contrast smoke calibration",
            "baseline_repeats": 2,
            "parameters": [
                {"parameter": "Contrast2012", "values": [-20, 20]},
            ],
        }
    )


def bridge(tmp_path: Path) -> InMemoryLightroomBridge:
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


def test_calibrator_runs_response_curve_and_restores_every_point(tmp_path: Path) -> None:
    lightroom = bridge(tmp_path)
    analyzer = SequenceAnalyzer(
        [
            metrics(50.0),
            metrics(50.2),
            metrics(50.1),
            metrics(49.0, 25.0),
            metrics(50.1),
            metrics(51.0, 36.0),
            metrics(50.1),
        ]
    )

    report = asyncio.run(
        LightroomActuatorCalibrator(bridge=lightroom, analyzer=analyzer).run(manifest())
    )

    assert report.status is CalibrationRunStatus.COMPLETED
    assert report.planned_sample_count == 2
    assert report.planned_render_count == 7
    assert report.duration_seconds >= 0
    assert report.completed_sample_count == 2
    assert report.failed_sample_count == 0
    assert lightroom.approval_requests == 1
    assert len(lightroom.virtual_copies) == 1
    assert len(lightroom.recovery_snapshots) == 2
    assert lightroom.restored_snapshots == [
        snapshot.name for snapshot in lightroom.recovery_snapshots
    ]
    photo_report = report.photos[0]
    assert photo_report.baseline_metrics.luminance_median == pytest.approx(50.1)
    assert photo_report.baseline_repeatability_drift.luminance_median == pytest.approx(0.1)
    assert photo_report.samples[0].status is CalibrationPointStatus.COMPLETED
    assert photo_report.samples[0].applied_value == -20
    assert photo_report.samples[1].applied_metric_delta is not None
    assert photo_report.samples[1].applied_metric_delta.luminance_contrast == 6
    assert lightroom.applied_settings == {}


def test_calibrator_rejection_has_no_lightroom_side_effects(tmp_path: Path) -> None:
    lightroom = bridge(tmp_path)
    lightroom.approval_granted = False

    report = asyncio.run(
        LightroomActuatorCalibrator(
            bridge=lightroom,
            analyzer=SequenceAnalyzer([]),
        ).run(manifest())
    )

    assert report.status is CalibrationRunStatus.REJECTED
    assert report.planned_sample_count == 2
    assert report.planned_render_count == 7
    assert report.completed_sample_count == 0
    assert lightroom.virtual_copies == []
    assert lightroom.recovery_snapshots == []


def test_calibrator_records_measurement_failure_after_restoration(tmp_path: Path) -> None:
    lightroom = bridge(tmp_path)
    one_point = manifest().model_copy(
        update={
            "parameters": manifest().parameters[:1],
        }
    )
    one_point = ActuatorCalibrationManifest.model_validate(
        {
            **one_point.model_dump(),
            "parameters": [{"parameter": "Contrast2012", "values": [20]}],
        }
    )
    analyzer = SequenceAnalyzer(
        [metrics(50.0), metrics(50.0), metrics(50.0), RuntimeError("decode failed"), metrics(50.0)]
    )

    report = asyncio.run(
        LightroomActuatorCalibrator(bridge=lightroom, analyzer=analyzer).run(one_point)
    )

    assert report.status is CalibrationRunStatus.COMPLETED_WITH_ERRORS
    assert report.failed_sample_count == 1
    assert report.photos[0].samples[0].status is CalibrationPointStatus.FAILED
    assert "decode failed" in (report.photos[0].samples[0].error or "")
    assert len(lightroom.restored_snapshots) == 1
    assert lightroom.applied_settings == {}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "id": "duplicate",
                "name": "Duplicate",
                "parameters": [
                    {"parameter": "Contrast2012", "values": [-20]},
                    {"parameter": "Contrast2012", "values": [20]},
                ],
            },
            "only once",
        ),
        (
            {
                "id": "bad-value",
                "name": "Bad value",
                "parameters": [{"parameter": "Exposure2012", "values": [6]}],
            },
            "between -5.0 and 5.0",
        ),
    ],
)
def test_manifest_rejects_unsafe_experiment(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ActuatorCalibrationManifest.model_validate(payload)
