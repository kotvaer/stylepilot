from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from stylepilot.domain.evaluation import (
    ActuatorCalibrationAggregateReport,
    CalibrationActuationAggregate,
    CalibrationParameterAggregate,
    CalibrationRenderIntegrityAggregate,
    CalibrationResponsePointAggregate,
    CalibrationResponseTrend,
    DatasetConditionCoverage,
    EvaluationDatasetCoverage,
    EvaluationDatasetManifest,
    PhotoMetricDistributions,
    ScalarDistribution,
)
from stylepilot.domain.models import (
    ActuatorCalibrationReport,
    CalibrationPhotoReport,
    CalibrationPointStatus,
    CalibrationSampleReport,
    PhotoMetricDelta,
)

_METRIC_FIELDS = tuple(PhotoMetricDelta.model_fields)


@dataclass(frozen=True, slots=True)
class CalibrationReportAggregator:
    """Combine versioned calibration runs without manufacturing a quality score."""

    def aggregate(
        self,
        reports: Sequence[ActuatorCalibrationReport],
        *,
        dataset: EvaluationDatasetManifest | None = None,
        dataset_root: Path | None = None,
        aggregate_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ActuatorCalibrationAggregateReport:
        source_reports = tuple(reports)
        if not source_reports:
            msg = "At least one actuator calibration report is required"
            raise ValueError(msg)
        run_ids = [report.run_id for report in source_reports]
        if len(set(run_ids)) != len(run_ids):
            msg = "Actuator calibration run IDs must be unique"
            raise ValueError(msg)

        photos = tuple(photo for report in source_reports for photo in report.photos)
        samples = tuple(sample for photo in photos for sample in photo.samples)
        coverage = (
            _dataset_coverage(dataset, photos, dataset_root=dataset_root)
            if dataset is not None
            else None
        )
        warnings = _aggregate_warnings(source_reports, samples, coverage)
        status_counts = Counter(report.status.value for report in source_reports)

        return ActuatorCalibrationAggregateReport(
            aggregate_id=aggregate_id or uuid4().hex,
            created_at=created_at or datetime.now(UTC),
            source_report_count=len(source_reports),
            source_run_ids=tuple(run_ids),
            source_status_counts=dict(sorted(status_counts.items())),
            source_manifest_ids=tuple(sorted({report.manifest.id for report in source_reports})),
            photo_observation_count=len(photos),
            distinct_source_photo_count=len(
                {(str(photo.source_photo.path), photo.source_photo.filename) for photo in photos}
            ),
            planned_render_count=sum(report.planned_render_count for report in source_reports),
            duration_seconds=_distribution(
                report.duration_seconds for report in source_reports if report.duration_seconds > 0
            ),
            dataset_coverage=coverage,
            actuation=_actuation_aggregate(source_reports, samples),
            render_integrity=_render_integrity_aggregate(photos),
            parameters=_parameter_aggregates(samples),
            warnings=warnings,
        )


def render_calibration_aggregate_markdown(
    report: ActuatorCalibrationAggregateReport,
) -> str:
    """Render a compact audit view while JSON retains every component statistic."""

    actuation = report.actuation
    lines = [
        "# Lightroom actuator calibration aggregate",
        "",
        f"- Aggregate ID: `{report.aggregate_id}`",
        f"- Source runs: {report.source_report_count}",
        f"- Photo observations: {report.photo_observation_count} "
        f"({report.distinct_source_photo_count} distinct)",
        f"- Planned renders reported: {report.planned_render_count}",
        "- Composite quality score: intentionally not defined",
        "",
        "## Actuation and restoration",
        "",
        "| Evidence | Observed | Matched / completed | Rate | Error median | Error max |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| Completed samples | {actuation.observed_sample_count} | "
            f"{actuation.completed_sample_count} | {_format_rate(actuation.completion_rate)} | "
            "— | — |"
        ),
        (
            f"| Applied readback | {actuation.applied_readback_observation_count} | "
            f"{actuation.applied_readback_match_count} | "
            f"{_format_rate(actuation.applied_readback_match_rate)} | "
            f"{_format_number(actuation.applied_readback_error.median)} | "
            f"{_format_number(actuation.applied_readback_error.maximum)} |"
        ),
        (
            f"| Restored readback | {actuation.restoration_readback_observation_count} | "
            f"{actuation.restoration_readback_match_count} | "
            f"{_format_rate(actuation.restoration_readback_match_rate)} | "
            f"{_format_number(actuation.restoration_readback_error.median)} | "
            f"{_format_number(actuation.restoration_readback_error.maximum)} |"
        ),
        "",
        "## Render integrity",
        "",
        "Values are absolute metric drift; they are evidence, not universal pass thresholds.",
        "",
        "| Metric | Baseline repeat max | Copy inheritance max | Post-restore max |",
        "| --- | ---: | ---: | ---: |",
    ]
    integrity = report.render_integrity
    for metric in _METRIC_FIELDS:
        lines.append(
            f"| `{metric}` | "
            f"{_format_number(getattr(integrity.baseline_repeatability_drift, metric).maximum)} "
            f"| {_format_number(getattr(integrity.copy_inheritance_drift, metric).maximum)} "
            f"| {_format_number(getattr(integrity.restoration_metric_drift, metric).maximum)} |"
        )

    if report.dataset_coverage is not None:
        coverage = report.dataset_coverage
        lines.extend(
            [
                "",
                "## Dataset coverage",
                "",
                f"- Dataset: `{coverage.dataset_id}`",
                f"- Observed assets: {coverage.observed_asset_count}/"
                f"{coverage.expected_asset_count} ({_format_rate(coverage.coverage_rate)})",
                f"- Matched photo observations: {coverage.matched_photo_observation_count}",
                f"- Integrity checked: {'yes' if coverage.integrity_checked else 'no'}",
                f"- Verified files: {coverage.verified_asset_count}",
            ]
        )

    lines.extend(
        [
            "",
            "## Parameter response",
            "",
            "Spearman correlation uses actual actuator delta versus signed rendered-metric "
            "delta. It is omitted when the observations are insufficient or constant.",
            "",
            "| Parameter | Samples | Completed | Requested values | Strongest monotonic feature |",
            "| --- | ---: | ---: | --- | --- |",
        ]
    )
    for parameter in report.parameters:
        trends = [
            trend
            for trend in parameter.response_trends
            if trend.spearman_rank_correlation is not None
        ]
        strongest = max(
            trends,
            key=lambda trend: abs(trend.spearman_rank_correlation or 0.0),
            default=None,
        )
        trend_text = (
            "—"
            if strongest is None
            else f"`{strongest.metric}` (rho={strongest.spearman_rank_correlation:.3f})"
        )
        requested_values = ", ".join(f"{value:g}" for value in parameter.requested_values)
        lines.append(
            f"| `{parameter.parameter}` | {parameter.observed_sample_count} | "
            f"{parameter.completed_sample_count} | {requested_values} | {trend_text} |"
        )

    if report.warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report.warnings)
    return "\n".join(lines) + "\n"


def _actuation_aggregate(
    reports: tuple[ActuatorCalibrationReport, ...],
    samples: tuple[CalibrationSampleReport, ...],
) -> CalibrationActuationAggregate:
    completed = sum(sample.status is CalibrationPointStatus.COMPLETED for sample in samples)
    failed = len(samples) - completed
    applied_matches = [
        sample.applied_readback_matches
        for sample in samples
        if sample.applied_readback_matches is not None
    ]
    restored_matches = [
        sample.restoration_readback_matches
        for sample in samples
        if sample.restoration_readback_matches is not None
    ]
    return CalibrationActuationAggregate(
        planned_sample_count=sum(report.planned_sample_count for report in reports),
        observed_sample_count=len(samples),
        completed_sample_count=completed,
        failed_sample_count=failed,
        completion_rate=_rate(completed, len(samples)),
        applied_readback_match_count=sum(applied_matches),
        applied_readback_observation_count=len(applied_matches),
        applied_readback_match_rate=_rate(sum(applied_matches), len(applied_matches)),
        restoration_readback_match_count=sum(restored_matches),
        restoration_readback_observation_count=len(restored_matches),
        restoration_readback_match_rate=_rate(sum(restored_matches), len(restored_matches)),
        applied_readback_error=_distribution(
            sample.applied_readback_error
            for sample in samples
            if sample.applied_readback_error is not None
        ),
        restoration_readback_error=_distribution(
            sample.restoration_readback_error
            for sample in samples
            if sample.restoration_readback_error is not None
        ),
    )


def _render_integrity_aggregate(
    photos: tuple[CalibrationPhotoReport, ...],
) -> CalibrationRenderIntegrityAggregate:
    baseline_drifts = tuple(photo.baseline_repeatability_drift for photo in photos)
    copy_drifts = tuple(photo.copy_inheritance_drift for photo in photos)
    restoration_drifts = tuple(
        sample.restoration_metric_drift
        for photo in photos
        for sample in photo.samples
        if sample.restoration_metric_drift is not None
    )
    return CalibrationRenderIntegrityAggregate(
        photo_observation_count=len(photos),
        restoration_observation_count=len(restoration_drifts),
        baseline_repeatability_drift=_metric_distributions(baseline_drifts),
        copy_inheritance_drift=_metric_distributions(copy_drifts),
        restoration_metric_drift=_metric_distributions(restoration_drifts),
    )


def _parameter_aggregates(
    samples: tuple[CalibrationSampleReport, ...],
) -> tuple[CalibrationParameterAggregate, ...]:
    grouped: defaultdict[str, list[CalibrationSampleReport]] = defaultdict(list)
    for sample in samples:
        grouped[sample.parameter].append(sample)

    aggregates: list[CalibrationParameterAggregate] = []
    for parameter, parameter_samples in sorted(grouped.items()):
        requested_values = tuple(sorted({sample.requested_value for sample in parameter_samples}))
        response_points = tuple(
            _response_point(value, parameter_samples) for value in requested_values
        )
        completed = sum(
            sample.status is CalibrationPointStatus.COMPLETED for sample in parameter_samples
        )
        aggregates.append(
            CalibrationParameterAggregate(
                parameter=parameter,
                observed_sample_count=len(parameter_samples),
                completed_sample_count=completed,
                failed_sample_count=len(parameter_samples) - completed,
                requested_values=requested_values,
                response_points=response_points,
                response_trends=tuple(
                    _response_trend(metric, parameter_samples) for metric in _METRIC_FIELDS
                ),
            )
        )
    return tuple(aggregates)


def _response_point(
    requested_value: float,
    samples: list[CalibrationSampleReport],
) -> CalibrationResponsePointAggregate:
    point_samples = [sample for sample in samples if sample.requested_value == requested_value]
    completed = sum(sample.status is CalibrationPointStatus.COMPLETED for sample in point_samples)
    return CalibrationResponsePointAggregate(
        requested_value=requested_value,
        observed_sample_count=len(point_samples),
        completed_sample_count=completed,
        failed_sample_count=len(point_samples) - completed,
        actuator_delta=_distribution(
            sample.applied_value - sample.before_value
            for sample in point_samples
            if sample.applied_value is not None
        ),
        rendered_metric_delta=_metric_distributions(
            tuple(
                sample.applied_metric_delta
                for sample in point_samples
                if sample.applied_metric_delta is not None
            )
        ),
    )


def _response_trend(
    metric: str,
    samples: list[CalibrationSampleReport],
) -> CalibrationResponseTrend:
    pairs = [
        (
            sample.applied_value - sample.before_value,
            getattr(sample.applied_metric_delta, metric),
        )
        for sample in samples
        if sample.applied_value is not None and sample.applied_metric_delta is not None
    ]
    actuator_deltas = [pair[0] for pair in pairs]
    metric_deltas = [pair[1] for pair in pairs]
    return CalibrationResponseTrend(
        metric=metric,
        observation_count=len(pairs),
        unique_actuator_delta_count=len(set(actuator_deltas)),
        spearman_rank_correlation=_spearman(actuator_deltas, metric_deltas),
    )


def _dataset_coverage(
    dataset: EvaluationDatasetManifest,
    photos: tuple[CalibrationPhotoReport, ...],
    *,
    dataset_root: Path | None,
) -> EvaluationDatasetCoverage:
    dataset_by_filename = {photo.relative_path.name: photo for photo in dataset.photos}
    observed_ids: set[str] = set()
    unexpected: set[str] = set()
    matched_observations = 0
    for photo_report in photos:
        filename = photo_report.source_photo.filename
        asset = dataset_by_filename.get(filename)
        if asset is None:
            unexpected.add(filename)
        else:
            observed_ids.add(asset.id)
            matched_observations += 1

    verified_ids: set[str] = set()
    missing_file_ids: set[str] = set()
    checksum_mismatch_ids: set[str] = set()
    if dataset_root is not None:
        for asset in dataset.photos:
            asset_path = dataset_root / asset.relative_path
            if not asset_path.is_file():
                missing_file_ids.add(asset.id)
            elif _sha256(asset_path) == asset.sha256:
                verified_ids.add(asset.id)
            else:
                checksum_mismatch_ids.add(asset.id)

    conditions = []
    for condition in sorted({tag for photo in dataset.photos for tag in photo.condition_tags}):
        expected_ids = {photo.id for photo in dataset.photos if condition in photo.condition_tags}
        conditions.append(
            DatasetConditionCoverage(
                condition=condition,
                expected_asset_count=len(expected_ids),
                observed_asset_count=len(expected_ids & observed_ids),
            )
        )
    expected_ids = {photo.id for photo in dataset.photos}
    return EvaluationDatasetCoverage(
        dataset_id=dataset.id,
        expected_asset_count=len(dataset.photos),
        observed_asset_count=len(observed_ids),
        matched_photo_observation_count=matched_observations,
        coverage_rate=len(observed_ids) / len(dataset.photos),
        observed_asset_ids=tuple(sorted(observed_ids)),
        missing_asset_ids=tuple(sorted(expected_ids - observed_ids)),
        unexpected_source_filenames=tuple(sorted(unexpected)),
        integrity_checked=dataset_root is not None,
        verified_asset_count=len(verified_ids),
        missing_file_asset_ids=tuple(sorted(missing_file_ids)),
        checksum_mismatch_asset_ids=tuple(sorted(checksum_mismatch_ids)),
        conditions=tuple(conditions),
    )


def _aggregate_warnings(
    reports: tuple[ActuatorCalibrationReport, ...],
    samples: tuple[CalibrationSampleReport, ...],
    coverage: EvaluationDatasetCoverage | None,
) -> tuple[str, ...]:
    warnings: list[str] = []
    failed = sum(sample.status is CalibrationPointStatus.FAILED for sample in samples)
    if failed:
        warnings.append(f"{failed} observed sample point(s) failed; inspect source reports.")
    incomplete_reports = [report.run_id for report in reports if report.status.value != "completed"]
    if incomplete_reports:
        warnings.append(
            f"{len(incomplete_reports)} source run(s) were rejected or completed with errors."
        )
    missing_duration = sum(report.duration_seconds <= 0 for report in reports)
    if missing_duration:
        warnings.append(f"{missing_duration} source report(s) do not contain usable duration data.")
    missing_render_count = sum(
        report.planned_render_count == 0 and bool(report.photos) for report in reports
    )
    if missing_render_count:
        warnings.append(
            f"{missing_render_count} source report(s) predate planned-render accounting."
        )
    values_by_parameter: defaultdict[str, set[float]] = defaultdict(set)
    for sample in samples:
        values_by_parameter[sample.parameter].add(sample.requested_value)
    for parameter, values in sorted(values_by_parameter.items()):
        if len(values) < 3:
            warnings.append(
                f"{parameter} has fewer than three requested values; "
                "response trend evidence is limited."
            )
    if coverage is not None:
        if coverage.observed_asset_count < coverage.expected_asset_count:
            warnings.append(
                "The aggregate does not cover every asset declared by the evaluation dataset."
            )
        if coverage.unexpected_source_filenames:
            warnings.append("Some source photos are not declared by the evaluation dataset.")
        if coverage.missing_file_asset_ids or coverage.checksum_mismatch_asset_ids:
            warnings.append("Evaluation dataset file integrity verification did not fully pass.")
    return tuple(warnings)


def _metric_distributions(
    deltas: Sequence[PhotoMetricDelta],
) -> PhotoMetricDistributions:
    return PhotoMetricDistributions.model_validate(
        {
            metric: _distribution(getattr(delta, metric) for delta in deltas)
            for metric in _METRIC_FIELDS
        }
    )


def _distribution(values: Iterable[float]) -> ScalarDistribution:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return ScalarDistribution(count=0)
    return ScalarDistribution(
        count=len(ordered),
        minimum=ordered[0],
        first_quartile=_quantile(ordered, 0.25),
        median=_quantile(ordered, 0.5),
        third_quartile=_quantile(ordered, 0.75),
        maximum=ordered[-1],
    )


def _quantile(ordered: list[float], probability: float) -> float:
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3 or len(set(left)) < 2 or len(set(right)) < 2:
        return None
    left_ranks = _ranks(left)
    right_ranks = _ranks(right)
    left_mean = sum(left_ranks) / len(left_ranks)
    right_mean = sum(right_ranks) / len(right_ranks)
    numerator = sum(
        (left_rank - left_mean) * (right_rank - right_mean)
        for left_rank, right_rank in zip(left_ranks, right_ranks, strict=True)
    )
    left_scale = sum((rank - left_mean) ** 2 for rank in left_ranks)
    right_scale = sum((rank - right_mean) ** 2 for rank in right_ranks)
    denominator = math.sqrt(left_scale * right_scale)
    if denominator == 0:
        return None
    return max(-1.0, min(1.0, numerator / denominator))


def _ranks(values: list[float]) -> list[float]:
    ranks = [0.0] * len(values)
    ordered_indices = sorted(range(len(values)), key=values.__getitem__)
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and values[ordered_indices[end]] == values[ordered_indices[start]]:
            end += 1
        average_rank = (start + 1 + end) / 2
        for index in ordered_indices[start:end]:
            ranks[index] = average_rank
        start = end
    return ranks


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _format_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _format_number(value: float | None) -> str:
    return "—" if value is None else f"{value:.6g}"
