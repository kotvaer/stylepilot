from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from stylepilot.application.evaluation import (
    CalibrationReportAggregator,
    render_calibration_aggregate_markdown,
)
from stylepilot.domain.evaluation import (
    EvaluationAssetKind,
    EvaluationDatasetManifest,
    EvaluationDatasetPhoto,
    ScalarDistribution,
)
from stylepilot.domain.models import (
    ActuatorCalibrationManifest,
    ActuatorCalibrationReport,
    CalibrationPhotoReport,
    CalibrationPointStatus,
    CalibrationRunStatus,
    CalibrationSampleReport,
    PhotoMetricDelta,
    PhotoMetrics,
    PhotoRef,
    SceneType,
)
from stylepilot.services.synthetic_dataset import SyntheticEvaluationDatasetBuilder

_CREATED_AT = datetime(2026, 8, 19, tzinfo=UTC)


def _metrics(value: float = 0.0) -> PhotoMetrics:
    return PhotoMetrics(
        luminance_median=50 + value,
        luminance_contrast=20 + value,
        mean_chroma=10 + value,
        mean_lab_a=value,
        mean_lab_b=value,
        shadow_clip_ratio=max(0.0, value / 1000),
        highlight_clip_ratio=max(0.0, value / 1000),
        noise_risk=max(0.0, value / 100),
    )


def _delta(response: float = 0.0, *, absolute: bool = False) -> PhotoMetricDelta:
    value = abs(response) if absolute else response
    return PhotoMetricDelta(
        luminance_median=value * 0.5,
        luminance_contrast=value,
        mean_chroma=value * 0.25,
        mean_lab_a=value * 0.1,
        mean_lab_b=value * -0.1 if not absolute else value * 0.1,
        shadow_clip_ratio=value * 0.0001,
        highlight_clip_ratio=value * 0.0002,
        noise_risk=value * 0.001,
    )


def _sample(
    requested_value: float,
    response: float,
    *,
    status: CalibrationPointStatus = CalibrationPointStatus.COMPLETED,
) -> CalibrationSampleReport:
    if status is CalibrationPointStatus.FAILED:
        return CalibrationSampleReport(
            status=status,
            parameter="Contrast2012",
            requested_value=requested_value,
            before_value=0,
            error="render failed after safe restoration",
        )
    return CalibrationSampleReport(
        status=status,
        parameter="Contrast2012",
        requested_value=requested_value,
        before_value=0,
        applied_value=requested_value,
        restored_value=0,
        applied_readback_error=0,
        restoration_readback_error=0,
        applied_readback_matches=True,
        restoration_readback_matches=True,
        applied_metrics=_metrics(response),
        restored_metrics=_metrics(),
        applied_metric_delta=_delta(response),
        restoration_metric_drift=_delta(0, absolute=True),
    )


def _report(
    run_id: str,
    filename: str,
    samples: tuple[CalibrationSampleReport, ...],
    *,
    status: CalibrationRunStatus = CalibrationRunStatus.COMPLETED,
    duration_seconds: float = 12.5,
    planned_render_count: int = 10,
) -> ActuatorCalibrationReport:
    source = PhotoRef(
        id=f"source-{run_id}",
        path=Path("/dataset/images") / filename,
        filename=filename,
    )
    calibration = PhotoRef(
        id=f"copy-{run_id}",
        path=source.path,
        filename=filename,
    )
    values = tuple(dict.fromkeys(sample.requested_value for sample in samples)) or (0.0,)
    manifest = ActuatorCalibrationManifest.model_validate(
        {
            "id": "contrast-v1",
            "name": "Contrast v1",
            "baseline_repeats": 2,
            "parameters": [{"parameter": "Contrast2012", "values": values}],
        }
    )
    photo = CalibrationPhotoReport(
        source_photo=source,
        calibration_photo=calibration,
        baseline_measurements=(_metrics(), _metrics(0.01)),
        baseline_metrics=_metrics(),
        baseline_repeatability_drift=_delta(0.02, absolute=True),
        calibration_copy_baseline_metrics=_metrics(),
        copy_inheritance_drift=_delta(0.01, absolute=True),
        samples=samples,
    )
    completed = sum(sample.status is CalibrationPointStatus.COMPLETED for sample in samples)
    return ActuatorCalibrationReport(
        run_id=run_id,
        created_at=_CREATED_AT,
        completed_at=_CREATED_AT + timedelta(seconds=duration_seconds),
        status=status,
        manifest=manifest,
        selected_photos=(source,),
        planned_sample_count=len(samples),
        planned_render_count=planned_render_count,
        duration_seconds=duration_seconds,
        completed_sample_count=completed,
        failed_sample_count=len(samples) - completed,
        photos=(photo,),
    )


def test_synthetic_dataset_is_reproducible_and_self_verifying(tmp_path: Path) -> None:
    output_dir = tmp_path / "dataset"
    builder = SyntheticEvaluationDatasetBuilder()

    first = builder.build(output_dir)
    second = builder.build(output_dir)

    assert first == second
    assert len(first.photos) == 5
    assert {photo.capture_series_id for photo in first.photos} == {
        "synthetic-tone",
        "synthetic-color",
        "synthetic-detail",
    }
    for photo in first.photos:
        photo_path = output_dir / photo.relative_path
        payload = photo_path.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == photo.sha256
        with Image.open(photo_path) as image:
            assert image.info["icc_profile"]

    corrupted = output_dir / first.photos[0].relative_path
    corrupted.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="Refusing to replace"):
        builder.build(output_dir)


def test_synthetic_dataset_refuses_a_file_as_output_root(tmp_path: Path) -> None:
    output_file = tmp_path / "not-a-directory"
    output_file.write_text("occupied", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        SyntheticEvaluationDatasetBuilder().build(output_file)


def test_aggregate_keeps_actuation_render_response_and_coverage_separate(
    tmp_path: Path,
) -> None:
    dataset_root = tmp_path / "dataset"
    dataset = SyntheticEvaluationDatasetBuilder().build(dataset_root)
    report = _report(
        "run-1",
        "neutral-tone-ramp.tiff",
        (_sample(-20, -2), _sample(0, 0), _sample(20, 3)),
    )

    aggregate = CalibrationReportAggregator().aggregate(
        (report,),
        dataset=dataset,
        dataset_root=dataset_root,
        aggregate_id="aggregate-1",
        created_at=_CREATED_AT,
    )

    assert aggregate.source_run_ids == ("run-1",)
    assert aggregate.actuation.completion_rate == 1
    assert aggregate.actuation.applied_readback_error.maximum == 0
    baseline_contrast = aggregate.render_integrity.baseline_repeatability_drift.luminance_contrast
    restored_contrast = aggregate.render_integrity.restoration_metric_drift.luminance_contrast
    assert baseline_contrast.maximum == 0.02
    assert restored_contrast.maximum == 0
    contrast = aggregate.parameters[0]
    assert contrast.requested_values == (-20, 0, 20)
    assert contrast.response_points[0].actuator_delta.median == -20
    trend = next(item for item in contrast.response_trends if item.metric == "luminance_contrast")
    assert trend.spearman_rank_correlation == pytest.approx(1)
    assert aggregate.dataset_coverage is not None
    assert aggregate.dataset_coverage.observed_asset_count == 1
    assert aggregate.dataset_coverage.verified_asset_count == 5
    assert aggregate.dataset_coverage.missing_asset_ids == (
        "color-patches",
        "fine-detail",
        "high-key-tone",
        "low-key-tone",
    )

    markdown = render_calibration_aggregate_markdown(aggregate)
    assert "Composite quality score: intentionally not defined" in markdown
    assert "`Contrast2012`" in markdown
    assert "rho=1.000" in markdown


def test_aggregate_reports_dataset_integrity_and_unexpected_sources(tmp_path: Path) -> None:
    dataset_root = tmp_path / "dataset"
    dataset = SyntheticEvaluationDatasetBuilder().build(dataset_root)
    (dataset_root / dataset.photos[0].relative_path).write_bytes(b"changed")
    (dataset_root / dataset.photos[1].relative_path).unlink()
    report = _report("run-unknown", "not-declared.tiff", (_sample(0, 0),))

    aggregate = CalibrationReportAggregator().aggregate(
        (report,),
        dataset=dataset,
        dataset_root=dataset_root,
    )

    coverage = aggregate.dataset_coverage
    assert coverage is not None
    assert coverage.verified_asset_count == 3
    assert coverage.checksum_mismatch_asset_ids == (dataset.photos[0].id,)
    assert coverage.missing_file_asset_ids == (dataset.photos[1].id,)
    assert coverage.unexpected_source_filenames == ("not-declared.tiff",)
    assert any("integrity" in warning for warning in aggregate.warnings)
    assert any("not declared" in warning for warning in aggregate.warnings)
    assert any("fewer than three" in warning for warning in aggregate.warnings)


def test_aggregate_exposes_missing_observations_without_dividing_by_zero() -> None:
    failed_report = _report(
        "run-failed",
        "failed.tiff",
        (_sample(10, 0, status=CalibrationPointStatus.FAILED),),
        status=CalibrationRunStatus.COMPLETED_WITH_ERRORS,
        duration_seconds=0,
        planned_render_count=0,
    )

    aggregate = CalibrationReportAggregator().aggregate((failed_report,))

    assert aggregate.actuation.completion_rate == 0
    assert aggregate.actuation.applied_readback_match_rate is None
    assert aggregate.actuation.applied_readback_error.count == 0
    assert aggregate.duration_seconds.count == 0
    assert aggregate.parameters[0].response_trends[0].spearman_rank_correlation is None
    assert any("failed" in warning for warning in aggregate.warnings)
    assert any("duration" in warning for warning in aggregate.warnings)
    assert any("planned-render" in warning for warning in aggregate.warnings)


def test_aggregate_requires_non_duplicate_source_runs() -> None:
    report = _report("run-1", "one.tiff", (_sample(-1, -1),))
    aggregator = CalibrationReportAggregator()

    with pytest.raises(ValueError, match="At least one"):
        aggregator.aggregate(())
    with pytest.raises(ValueError, match="run IDs must be unique"):
        aggregator.aggregate((report, report))


def test_evaluation_domain_models_reject_ambiguous_or_invalid_evidence() -> None:
    with pytest.raises(ValidationError, match="empty distribution"):
        ScalarDistribution(count=0, median=0)
    with pytest.raises(ValidationError, match="complete five-number"):
        ScalarDistribution(count=1, minimum=0)
    with pytest.raises(ValidationError, match="must be ordered"):
        ScalarDistribution(
            count=1,
            minimum=1,
            first_quartile=0,
            median=1,
            third_quartile=1,
            maximum=1,
        )

    photo = EvaluationDatasetPhoto(
        id="one",
        relative_path=Path("images/one.tiff"),
        sha256="0" * 64,
        kind=EvaluationAssetKind.USER_OWNED,
        scene_type=SceneType.LANDSCAPE,
        capture_series_id="series-one",
        condition_tags=("bright",),
        width=100,
        height=100,
    )
    with pytest.raises(ValidationError, match="filenames must be unique"):
        EvaluationDatasetManifest(
            id="ambiguous",
            name="Ambiguous",
            description="Duplicate filenames cannot map to Lightroom reports.",
            photos=(
                photo,
                photo.model_copy(update={"id": "two", "relative_path": Path("other/one.tiff")}),
            ),
        )
    with pytest.raises(ValidationError, match="portable relative paths"):
        EvaluationDatasetPhoto.model_validate(
            {**photo.model_dump(), "relative_path": "/absolute/one.tiff"}
        )
    with pytest.raises(ValidationError, match="condition tags must be unique"):
        EvaluationDatasetPhoto.model_validate(
            {**photo.model_dump(), "condition_tags": ["bright", "bright"]}
        )
