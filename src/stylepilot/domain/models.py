from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


class DomainModel(BaseModel):
    """Base model with strict, immutable domain semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkflowStatus(StrEnum):
    NO_SELECTION = "no_selection"
    UNSUITABLE = "unsuitable"
    PLANNED = "planned"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"


class ParameterProbeStatus(StrEnum):
    REJECTED = "rejected"
    COMPLETED = "completed"


class CalibrationRunStatus(StrEnum):
    REJECTED = "rejected"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"


class CalibrationPointStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class SceneType(StrEnum):
    PORTRAIT = "portrait"
    PEOPLE = "people"
    LANDSCAPE = "landscape"
    CITYSCAPE = "cityscape"
    STREET = "street"
    ARCHITECTURE = "architecture"
    INTERIOR = "interior"
    WILDLIFE = "wildlife"
    PRODUCT = "product"
    FOOD = "food"
    MACRO = "macro"
    NIGHT_SKY = "night_sky"
    DOCUMENTARY = "documentary"
    ABSTRACT = "abstract"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class SceneCompatibility(StrEnum):
    NOT_REQUIRED = "not_required"
    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class PhotoRef(DomainModel):
    id: str = Field(min_length=1)
    path: Path
    filename: str = Field(min_length=1)


class DevelopSettings(DomainModel):
    """A guarded subset of Lightroom Classic Develop settings.

    Values are absolute Lightroom SDK values, not deltas. The Lua plugin will
    apply the same validation again before touching a photo.
    """

    values: dict[str, float] = Field(default_factory=dict)

    @field_validator("values", mode="before")
    @classmethod
    def validate_raw_value_types(cls, values: object) -> object:
        if not isinstance(values, dict):
            msg = "Lightroom Develop settings must be an object"
            raise ValueError(msg)
        for key, raw_value in values.items():
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                msg = f"{key} must be a finite number"
                raise ValueError(msg)
            if not math.isfinite(float(raw_value)):
                msg = f"{key} must be a finite number"
                raise ValueError(msg)
        return values

    @field_validator("values")
    @classmethod
    def validate_values(cls, values: dict[str, float]) -> dict[str, float]:
        validated: dict[str, float] = {}
        for key, raw_value in values.items():
            value_range = DEVELOP_PARAMETER_RANGES.get(key)
            if value_range is None:
                msg = f"Unsupported Lightroom Develop parameter: {key}"
                raise ValueError(msg)
            minimum, maximum = value_range
            value = float(raw_value)
            if not minimum <= value <= maximum:
                msg = f"{key} must be between {minimum} and {maximum}, got {value}"
                raise ValueError(msg)
            validated[key] = value
        return validated

    def get(self, key: str, default: float = 0.0) -> float:
        return self.values.get(key, default)


DEVELOP_PARAMETER_RANGES = MappingProxyType(
    {
        "Exposure2012": (-5.0, 5.0),
        "Contrast2012": (-100.0, 100.0),
        "Highlights2012": (-100.0, 100.0),
        "Shadows2012": (-100.0, 100.0),
        "Whites2012": (-100.0, 100.0),
        "Blacks2012": (-100.0, 100.0),
        "Texture": (-100.0, 100.0),
        "Clarity2012": (-100.0, 100.0),
        "Dehaze": (-100.0, 100.0),
        "Vibrance": (-100.0, 100.0),
        "Saturation": (-100.0, 100.0),
    }
)


class ParameterProbeRequest(DomainModel):
    """One explicitly approved point in Lightroom's Develop response surface."""

    parameter: str = Field(min_length=1)
    target_value: float

    @field_validator("parameter")
    @classmethod
    def validate_parameter(cls, parameter: str) -> str:
        if parameter not in DEVELOP_PARAMETER_RANGES:
            msg = f"Unsupported Lightroom Develop parameter: {parameter}"
            raise ValueError(msg)
        return parameter

    @field_validator("target_value")
    @classmethod
    def validate_target_value(cls, target_value: float, info: ValidationInfo) -> float:
        if not math.isfinite(target_value):
            msg = "target_value must be a finite number"
            raise ValueError(msg)
        parameter = info.data.get("parameter")
        if parameter is not None:
            minimum, maximum = DEVELOP_PARAMETER_RANGES[parameter]
            if not minimum <= target_value <= maximum:
                msg = f"{parameter} must be between {minimum} and {maximum}, got {target_value}"
                raise ValueError(msg)
        return target_value


class CalibrationParameterSweep(DomainModel):
    """Explicit sample values for one guarded Lightroom Develop parameter."""

    parameter: str = Field(min_length=1)
    values: tuple[float, ...] = Field(min_length=1, max_length=21)

    @field_validator("parameter")
    @classmethod
    def validate_parameter(cls, parameter: str) -> str:
        if parameter not in DEVELOP_PARAMETER_RANGES:
            msg = f"Unsupported Lightroom Develop parameter: {parameter}"
            raise ValueError(msg)
        return parameter

    @field_validator("values", mode="before")
    @classmethod
    def validate_raw_values(cls, values: object) -> object:
        if not isinstance(values, (list, tuple)):
            msg = "Calibration sweep values must be an array"
            raise ValueError(msg)
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                msg = "Calibration sweep values must be finite numbers"
                raise ValueError(msg)
        return values

    @model_validator(mode="after")
    def validate_values(self) -> CalibrationParameterSweep:
        minimum, maximum = DEVELOP_PARAMETER_RANGES[self.parameter]
        if len(set(self.values)) != len(self.values):
            msg = f"{self.parameter} calibration values must be unique"
            raise ValueError(msg)
        for value in self.values:
            if not math.isfinite(value):
                msg = f"{self.parameter} calibration values must be finite"
                raise ValueError(msg)
            if not minimum <= value <= maximum:
                msg = f"{self.parameter} must be between {minimum} and {maximum}, got {value}"
                raise ValueError(msg)
        return self


class ActuatorCalibrationManifest(DomainModel):
    """Bounded, user-auditable experiment definition for Lightroom calibration."""

    schema_version: str = "lightroom-actuator-calibration-manifest-v1"
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=255)
    baseline_repeats: Annotated[int, Field(ge=2, le=5)] = 3
    parameters: tuple[CalibrationParameterSweep, ...] = Field(
        min_length=1,
        max_length=len(DEVELOP_PARAMETER_RANGES),
    )

    @model_validator(mode="after")
    def validate_experiment_size(self) -> ActuatorCalibrationManifest:
        names = [sweep.parameter for sweep in self.parameters]
        if len(set(names)) != len(names):
            msg = "Each Lightroom parameter may appear only once in a calibration manifest"
            raise ValueError(msg)
        if self.sample_points > 60:
            msg = "A calibration manifest may contain at most 60 sample points per photo"
            raise ValueError(msg)
        return self

    @property
    def sample_points(self) -> int:
        return sum(len(sweep.values) for sweep in self.parameters)


class PhotoMetadata(DomainModel):
    photo_id: str = Field(min_length=1)
    iso: int | None = Field(default=None, ge=1)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    develop_settings: DevelopSettings = Field(default_factory=DevelopSettings)


class DevelopSnapshotRef(DomainModel):
    id: str = Field(min_length=1)
    photo_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=255)


class SceneAnalysis(DomainModel):
    primary_scene: SceneType
    scene_tags: tuple[SceneType, ...]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    has_person: bool
    has_face: bool
    dominant_subjects: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)


class PhotoMetrics(DomainModel):
    luminance_median: Annotated[float, Field(ge=0.0, le=100.0)]
    mean_chroma: Annotated[float, Field(ge=0.0)]
    shadow_clip_ratio: Annotated[float, Field(ge=0.0, le=1.0)]
    highlight_clip_ratio: Annotated[float, Field(ge=0.0, le=1.0)]
    noise_risk: Annotated[float, Field(ge=0.0, le=1.0)]
    luminance_contrast: Annotated[float, Field(ge=0.0, le=100.0)] = 0.0
    mean_lab_a: float = 0.0
    mean_lab_b: float = 0.0
    scene_type: SceneType = SceneType.UNKNOWN
    scene_tags: tuple[SceneType, ...] = ()
    scene_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    scene_analysis_error: str | None = None
    has_face: bool = False


class PhotoMetricDelta(DomainModel):
    """Signed or absolute differences between two rendered metric snapshots."""

    luminance_median: float
    luminance_contrast: float
    mean_chroma: float
    mean_lab_a: float
    mean_lab_b: float
    shadow_clip_ratio: float
    highlight_clip_ratio: float
    noise_risk: float


class ParameterProbeReport(DomainModel):
    """Auditable result of one temporary Develop-setting experiment."""

    schema_version: str = "lightroom-parameter-probe-v1"
    run_id: str = Field(min_length=1)
    created_at: datetime
    status: ParameterProbeStatus
    source_photo: PhotoRef
    probe_photo: PhotoRef | None = None
    parameter: str
    requested_value: float
    before_value: float
    applied_value: float | None = None
    restored_value: float | None = None
    applied_readback_error: Annotated[float | None, Field(ge=0.0)] = None
    restoration_readback_error: Annotated[float | None, Field(ge=0.0)] = None
    applied_readback_matches: bool | None = None
    restoration_readback_matches: bool | None = None
    baseline_metrics: PhotoMetrics
    applied_metrics: PhotoMetrics | None = None
    restored_metrics: PhotoMetrics | None = None
    applied_metric_delta: PhotoMetricDelta | None = None
    restoration_metric_drift: PhotoMetricDelta | None = None
    recovery_snapshot: DevelopSnapshotRef | None = None
    warnings: tuple[str, ...] = ()


class CalibrationSampleReport(DomainModel):
    """One applied-and-restored point on a calibration virtual copy."""

    status: CalibrationPointStatus
    parameter: str
    requested_value: float
    before_value: float
    applied_value: float | None = None
    restored_value: float | None = None
    applied_readback_error: Annotated[float | None, Field(ge=0.0)] = None
    restoration_readback_error: Annotated[float | None, Field(ge=0.0)] = None
    applied_readback_matches: bool | None = None
    restoration_readback_matches: bool | None = None
    applied_metrics: PhotoMetrics | None = None
    restored_metrics: PhotoMetrics | None = None
    applied_metric_delta: PhotoMetricDelta | None = None
    restoration_metric_drift: PhotoMetricDelta | None = None
    recovery_snapshot: DevelopSnapshotRef | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()


class CalibrationPhotoReport(DomainModel):
    """Repeatability baseline and response samples for one selected source photo."""

    source_photo: PhotoRef
    calibration_photo: PhotoRef
    baseline_measurements: tuple[PhotoMetrics, ...] = Field(min_length=2, max_length=5)
    baseline_metrics: PhotoMetrics
    baseline_repeatability_drift: PhotoMetricDelta
    calibration_copy_baseline_metrics: PhotoMetrics
    copy_inheritance_drift: PhotoMetricDelta
    samples: tuple[CalibrationSampleReport, ...]
    warnings: tuple[str, ...] = ()


class ActuatorCalibrationReport(DomainModel):
    """Auditable multi-photo Lightroom actuator calibration result."""

    schema_version: str = "lightroom-actuator-calibration-report-v1"
    run_id: str = Field(min_length=1)
    created_at: datetime
    completed_at: datetime
    status: CalibrationRunStatus
    manifest: ActuatorCalibrationManifest
    selected_photos: tuple[PhotoRef, ...]
    planned_sample_count: Annotated[int, Field(ge=0)]
    planned_render_count: Annotated[int, Field(ge=0)] = 0
    duration_seconds: Annotated[float, Field(ge=0.0)] = 0.0
    completed_sample_count: Annotated[int, Field(ge=0)] = 0
    failed_sample_count: Annotated[int, Field(ge=0)] = 0
    photos: tuple[CalibrationPhotoReport, ...] = ()
    warnings: tuple[str, ...] = ()


class StyleFeatureSpread(DomainModel):
    """Robust per-feature dispersion across a style's reference images."""

    luminance_median: Annotated[float, Field(ge=0.0)]
    luminance_contrast: Annotated[float, Field(ge=0.0)]
    mean_chroma: Annotated[float, Field(ge=0.0)]
    mean_lab_a: Annotated[float, Field(ge=0.0)]
    mean_lab_b: Annotated[float, Field(ge=0.0)]
    shadow_clip_ratio: Annotated[float, Field(ge=0.0)]
    highlight_clip_ratio: Annotated[float, Field(ge=0.0)]


class StyleProfile(DomainModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    target_luminance_median: Annotated[float, Field(ge=0.0, le=100.0)]
    target_mean_chroma: Annotated[float, Field(ge=0.0)]
    target_luminance_contrast: Annotated[float, Field(ge=0.0, le=100.0)] | None = None
    target_mean_lab_a: float | None = None
    target_mean_lab_b: float | None = None
    target_shadow_clip_ratio: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    target_highlight_clip_ratio: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    feature_spread: StyleFeatureSpread | None = None
    reference_count: Annotated[int, Field(ge=0, le=20)] = 0
    algorithm_version: str | None = None
    semantic_reference_count: Annotated[int, Field(ge=0, le=20)] = 0
    semantic_provider: str | None = None
    semantic_model: str | None = None
    semantic_prompt_version: str | None = None
    max_shadow_clip_ratio: Annotated[float, Field(ge=0.0, le=1.0)] = 0.01
    max_highlight_clip_ratio: Annotated[float, Field(ge=0.0, le=1.0)] = 0.01
    minimum_suitability_score: Annotated[float, Field(ge=0.0, le=100.0)] = 45.0
    preferred_scene_types: tuple[SceneType, ...] = ()


class ReferenceImageAssessment(DomainModel):
    path: Path
    metrics: PhotoMetrics
    robust_z_score: Annotated[float, Field(ge=0.0)]
    is_outlier_candidate: bool
    reasons: tuple[str, ...] = ()
    scene_analysis: SceneAnalysis | None = None


class StyleProfileBuildResult(DomainModel):
    profile: StyleProfile
    references: tuple[ReferenceImageAssessment, ...]
    warnings: tuple[str, ...] = ()


class SuitabilityReport(DomainModel):
    score: Annotated[float, Field(ge=0.0, le=100.0)]
    eligible: bool
    recommended_strength: Annotated[float, Field(ge=0.0, le=1.0)]
    reasons: tuple[str, ...]
    scene_compatibility: SceneCompatibility = SceneCompatibility.NOT_REQUIRED


class RenderVerificationReport(DomainModel):
    passed: bool
    before_style_distance: Annotated[float, Field(ge=0.0)]
    after_style_distance: Annotated[float, Field(ge=0.0)]
    improvement_ratio: float
    issues: tuple[str, ...]
    warnings: tuple[str, ...]


class EditPlan(DomainModel):
    source_photo_id: str
    style_profile_id: str
    strength: Annotated[float, Field(ge=0.0, le=1.0)]
    settings: DevelopSettings
    rationale: tuple[str, ...]


class WorkflowRequest(DomainModel):
    style_profile: StyleProfile
    requested_strength: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    apply: bool = False
    write_approved: bool = False


class WorkflowResult(DomainModel):
    status: WorkflowStatus
    message: str
    photo: PhotoRef | None = None
    metrics: PhotoMetrics | None = None
    suitability: SuitabilityReport | None = None
    plan: EditPlan | None = None
    applied_photo: PhotoRef | None = None
    recovery_snapshot: DevelopSnapshotRef | None = None
    rendered_metrics: PhotoMetrics | None = None
    verification: RenderVerificationReport | None = None
