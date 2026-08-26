from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from stylepilot.domain.models import DomainModel, SceneType


class EvaluationAssetKind(StrEnum):
    SYNTHETIC = "synthetic"
    USER_OWNED = "user_owned"


class EvaluationDatasetPhoto(DomainModel):
    """One redistributable or local-only photo declared by an evaluation dataset."""

    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    relative_path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: EvaluationAssetKind
    scene_type: SceneType
    capture_series_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    condition_tags: tuple[str, ...] = Field(min_length=1)
    width: Annotated[int, Field(ge=1)]
    height: Annotated[int, Field(ge=1)]

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, path: Path) -> Path:
        if path.is_absolute() or ".." in path.parts or path.name == "":
            msg = "Evaluation photo paths must be portable relative paths"
            raise ValueError(msg)
        return path

    @field_validator("condition_tags")
    @classmethod
    def validate_condition_tags(cls, tags: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(tags)) != len(tags):
            msg = "Evaluation photo condition tags must be unique"
            raise ValueError(msg)
        if any(not tag or tag.casefold() != tag for tag in tags):
            msg = "Evaluation photo condition tags must be non-empty lowercase strings"
            raise ValueError(msg)
        return tags


class EvaluationDatasetManifest(DomainModel):
    """Versioned local dataset inventory; image bytes are never embedded in reports."""

    schema_version: str = "stylepilot-evaluation-dataset-v1"
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=1000)
    generator_version: str | None = Field(default=None, min_length=1, max_length=100)
    color_space: str = Field(default="sRGB", min_length=1, max_length=100)
    photos: tuple[EvaluationDatasetPhoto, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_photos(self) -> EvaluationDatasetManifest:
        ids = [photo.id for photo in self.photos]
        paths = [photo.relative_path for photo in self.photos]
        filenames = [photo.relative_path.name for photo in self.photos]
        if len(set(ids)) != len(ids):
            msg = "Evaluation dataset photo IDs must be unique"
            raise ValueError(msg)
        if len(set(paths)) != len(paths):
            msg = "Evaluation dataset photo paths must be unique"
            raise ValueError(msg)
        if len(set(filenames)) != len(filenames):
            msg = "Evaluation dataset filenames must be unique for Lightroom report matching"
            raise ValueError(msg)
        return self


class ScalarDistribution(DomainModel):
    """A transparent five-number summary without a hidden pass threshold."""

    count: Annotated[int, Field(ge=0)]
    minimum: float | None = None
    first_quartile: float | None = None
    median: float | None = None
    third_quartile: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def validate_distribution(self) -> ScalarDistribution:
        values = (
            self.minimum,
            self.first_quartile,
            self.median,
            self.third_quartile,
            self.maximum,
        )
        if self.count == 0 and any(value is not None for value in values):
            msg = "An empty distribution cannot contain summary values"
            raise ValueError(msg)
        if self.count > 0:
            if any(value is None for value in values):
                msg = "A non-empty distribution requires a complete five-number summary"
                raise ValueError(msg)
            numeric_values = tuple(float(value) for value in values if value is not None)
            if any(not math.isfinite(value) for value in numeric_values):
                msg = "Distribution summary values must be finite"
                raise ValueError(msg)
            if numeric_values != tuple(sorted(numeric_values)):
                msg = "Distribution summary values must be ordered"
                raise ValueError(msg)
        return self


class PhotoMetricDistributions(DomainModel):
    luminance_median: ScalarDistribution
    luminance_contrast: ScalarDistribution
    mean_chroma: ScalarDistribution
    mean_lab_a: ScalarDistribution
    mean_lab_b: ScalarDistribution
    shadow_clip_ratio: ScalarDistribution
    highlight_clip_ratio: ScalarDistribution
    noise_risk: ScalarDistribution


class DatasetConditionCoverage(DomainModel):
    condition: str = Field(min_length=1)
    expected_asset_count: Annotated[int, Field(ge=1)]
    observed_asset_count: Annotated[int, Field(ge=0)]


class EvaluationDatasetCoverage(DomainModel):
    dataset_id: str = Field(min_length=1)
    expected_asset_count: Annotated[int, Field(ge=1)]
    observed_asset_count: Annotated[int, Field(ge=0)]
    matched_photo_observation_count: Annotated[int, Field(ge=0)]
    coverage_rate: Annotated[float, Field(ge=0.0, le=1.0)]
    observed_asset_ids: tuple[str, ...]
    missing_asset_ids: tuple[str, ...]
    unexpected_source_filenames: tuple[str, ...]
    integrity_checked: bool
    verified_asset_count: Annotated[int, Field(ge=0)] = 0
    missing_file_asset_ids: tuple[str, ...] = ()
    checksum_mismatch_asset_ids: tuple[str, ...] = ()
    conditions: tuple[DatasetConditionCoverage, ...]


class CalibrationActuationAggregate(DomainModel):
    planned_sample_count: Annotated[int, Field(ge=0)]
    observed_sample_count: Annotated[int, Field(ge=0)]
    completed_sample_count: Annotated[int, Field(ge=0)]
    failed_sample_count: Annotated[int, Field(ge=0)]
    completion_rate: Annotated[float | None, Field(ge=0.0, le=1.0)]
    applied_readback_match_count: Annotated[int, Field(ge=0)]
    applied_readback_observation_count: Annotated[int, Field(ge=0)]
    applied_readback_match_rate: Annotated[float | None, Field(ge=0.0, le=1.0)]
    restoration_readback_match_count: Annotated[int, Field(ge=0)]
    restoration_readback_observation_count: Annotated[int, Field(ge=0)]
    restoration_readback_match_rate: Annotated[float | None, Field(ge=0.0, le=1.0)]
    applied_readback_error: ScalarDistribution
    restoration_readback_error: ScalarDistribution


class CalibrationRenderIntegrityAggregate(DomainModel):
    photo_observation_count: Annotated[int, Field(ge=0)]
    restoration_observation_count: Annotated[int, Field(ge=0)]
    baseline_repeatability_drift: PhotoMetricDistributions
    copy_inheritance_drift: PhotoMetricDistributions
    restoration_metric_drift: PhotoMetricDistributions


class CalibrationResponsePointAggregate(DomainModel):
    requested_value: float
    observed_sample_count: Annotated[int, Field(ge=0)]
    completed_sample_count: Annotated[int, Field(ge=0)]
    failed_sample_count: Annotated[int, Field(ge=0)]
    actuator_delta: ScalarDistribution
    rendered_metric_delta: PhotoMetricDistributions


class CalibrationResponseTrend(DomainModel):
    metric: str = Field(min_length=1)
    observation_count: Annotated[int, Field(ge=0)]
    unique_actuator_delta_count: Annotated[int, Field(ge=0)]
    spearman_rank_correlation: Annotated[float | None, Field(ge=-1.0, le=1.0)]


class CalibrationParameterAggregate(DomainModel):
    parameter: str = Field(min_length=1)
    observed_sample_count: Annotated[int, Field(ge=0)]
    completed_sample_count: Annotated[int, Field(ge=0)]
    failed_sample_count: Annotated[int, Field(ge=0)]
    requested_values: tuple[float, ...]
    response_points: tuple[CalibrationResponsePointAggregate, ...]
    response_trends: tuple[CalibrationResponseTrend, ...]


class ActuatorCalibrationAggregateReport(DomainModel):
    """Cross-run evidence with actuation, integrity, response, and noise kept separate."""

    schema_version: str = "lightroom-actuator-calibration-aggregate-v1"
    aggregate_id: str = Field(min_length=1)
    created_at: datetime
    source_report_count: Annotated[int, Field(ge=1)]
    source_run_ids: tuple[str, ...]
    source_status_counts: dict[str, int]
    source_manifest_ids: tuple[str, ...]
    photo_observation_count: Annotated[int, Field(ge=0)]
    distinct_source_photo_count: Annotated[int, Field(ge=0)]
    planned_render_count: Annotated[int, Field(ge=0)]
    duration_seconds: ScalarDistribution
    dataset_coverage: EvaluationDatasetCoverage | None = None
    actuation: CalibrationActuationAggregate
    render_integrity: CalibrationRenderIntegrityAggregate
    parameters: tuple[CalibrationParameterAggregate, ...]
    warnings: tuple[str, ...] = ()
