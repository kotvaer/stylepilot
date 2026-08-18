from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from stylepilot.application.ports import LightroomBridge, PhotoAnalyzer
from stylepilot.domain.models import (
    DevelopSettings,
    EditPlan,
    ParameterProbeReport,
    ParameterProbeRequest,
    ParameterProbeStatus,
    PhotoMetricDelta,
    PhotoMetrics,
    StyleProfile,
    SuitabilityReport,
)

_METRIC_FIELDS = (
    "luminance_median",
    "luminance_contrast",
    "mean_chroma",
    "mean_lab_a",
    "mean_lab_b",
    "shadow_clip_ratio",
    "highlight_clip_ratio",
    "noise_risk",
)


class ParameterProbeRecoveryError(RuntimeError):
    """A probe could not restore its recovery snapshot automatically."""

    def __init__(
        self,
        *,
        photo_id: str,
        snapshot_name: str,
        restore_error: Exception,
        measurement_error: Exception | None = None,
    ) -> None:
        self.photo_id = photo_id
        self.snapshot_name = snapshot_name
        self.restore_error = restore_error
        self.measurement_error = measurement_error
        context = (
            " after a measurement failure"
            if measurement_error is not None
            else " after measurement"
        )
        super().__init__(
            f"Parameter probe could not restore {snapshot_name!r} on photo {photo_id}"
            f"{context}. Use `stylepilot lightroom rollback --photo-id {photo_id} "
            f"--snapshot-name {snapshot_name!r}` before continuing."
        )


@dataclass(frozen=True, slots=True)
class LightroomParameterProbe:
    """Measure one guarded Lightroom parameter and restore its virtual copy."""

    bridge: LightroomBridge
    analyzer: PhotoAnalyzer
    readback_tolerance: float = 1e-4

    def __post_init__(self) -> None:
        if self.readback_tolerance < 0:
            msg = "readback_tolerance must not be negative"
            raise ValueError(msg)

    async def run(self, request: ParameterProbeRequest) -> ParameterProbeReport:
        selected = await self.bridge.get_selected_photos()
        if not selected:
            msg = "Select one Lightroom photo before running a parameter probe."
            raise ValueError(msg)

        source = selected[0]
        source_metadata = await self.bridge.get_photo_metadata(source.id)
        baseline_preview = await self.bridge.render_analysis_preview(source.id)
        baseline_metrics = self.analyzer.analyze(baseline_preview, source_metadata)
        source_before_value = source_metadata.develop_settings.get(request.parameter)
        settings = DevelopSettings(values={request.parameter: request.target_value})
        run_id = uuid4().hex
        created_at = datetime.now(UTC)

        approved = await self.bridge.request_write_approval(
            source,
            self._approval_profile(request, baseline_metrics),
            SuitabilityReport(
                score=100.0,
                eligible=True,
                recommended_strength=1.0,
                reasons=(
                    "Temporary single-parameter actuator probe on a virtual copy.",
                    "The recovery snapshot will be restored after measurement.",
                ),
            ),
            EditPlan(
                source_photo_id=source.id,
                style_profile_id="lightroom-actuator-probe",
                strength=1.0,
                settings=settings,
                rationale=(f"Measure Lightroom's rendered response to {request.parameter}.",),
            ),
        )
        if not approved:
            return ParameterProbeReport(
                run_id=run_id,
                created_at=created_at,
                status=ParameterProbeStatus.REJECTED,
                source_photo=source,
                parameter=request.parameter,
                requested_value=request.target_value,
                before_value=source_before_value,
                baseline_metrics=baseline_metrics,
                warnings=("The photographer rejected the Lightroom parameter probe.",),
            )

        probe_photo = await self.bridge.create_virtual_copy(
            source.id,
            f"StylePilot Probe {request.parameter}",
        )
        probe_metadata = await self.bridge.get_photo_metadata(probe_photo.id)
        before_value = probe_metadata.develop_settings.get(request.parameter)
        warnings: list[str] = []
        if abs(before_value - source_before_value) > self.readback_tolerance:
            warnings.append(
                "The new virtual copy did not inherit the source parameter value exactly."
            )

        snapshot = await self.bridge.apply_develop_settings(
            probe_photo.id,
            settings,
            f"StylePilot Probe {request.parameter}",
        )
        try:
            applied_metadata = await self.bridge.get_photo_metadata(probe_photo.id)
            applied_value = applied_metadata.develop_settings.get(request.parameter)
            applied_preview = await self.bridge.render_analysis_preview(probe_photo.id)
            applied_metrics = self.analyzer.analyze(applied_preview, applied_metadata)
        except Exception as measurement_error:
            await self._restore_or_raise(
                probe_photo.id,
                snapshot.name,
                measurement_error=measurement_error,
            )
            raise
        await self._restore_or_raise(probe_photo.id, snapshot.name)

        restored_metadata = await self.bridge.get_photo_metadata(probe_photo.id)
        restored_value = restored_metadata.develop_settings.get(request.parameter)
        restored_preview = await self.bridge.render_analysis_preview(probe_photo.id)
        restored_metrics = self.analyzer.analyze(restored_preview, restored_metadata)

        applied_error = abs(applied_value - request.target_value)
        restoration_error = abs(restored_value - before_value)
        applied_matches = applied_error <= self.readback_tolerance
        restoration_matches = restoration_error <= self.readback_tolerance
        if not applied_matches:
            warnings.append("Lightroom's applied readback differs from the requested value.")
        if not restoration_matches:
            warnings.append("Lightroom's restored readback differs from the baseline value.")

        applied_delta = _metric_delta(applied_metrics, baseline_metrics)
        if all(abs(getattr(applied_delta, field)) <= 1e-9 for field in _METRIC_FIELDS):
            warnings.append("The rendered metrics showed no measurable response to this probe.")

        return ParameterProbeReport(
            run_id=run_id,
            created_at=created_at,
            status=ParameterProbeStatus.COMPLETED,
            source_photo=source,
            probe_photo=probe_photo,
            parameter=request.parameter,
            requested_value=request.target_value,
            before_value=before_value,
            applied_value=applied_value,
            restored_value=restored_value,
            applied_readback_error=applied_error,
            restoration_readback_error=restoration_error,
            applied_readback_matches=applied_matches,
            restoration_readback_matches=restoration_matches,
            baseline_metrics=baseline_metrics,
            applied_metrics=applied_metrics,
            restored_metrics=restored_metrics,
            applied_metric_delta=applied_delta,
            restoration_metric_drift=_metric_delta(
                restored_metrics,
                baseline_metrics,
                absolute=True,
            ),
            recovery_snapshot=snapshot,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _approval_profile(
        request: ParameterProbeRequest,
        metrics: PhotoMetrics,
    ) -> StyleProfile:
        return StyleProfile(
            id="lightroom-actuator-probe",
            name=f"Actuator probe: {request.parameter} = {request.target_value:g}",
            target_luminance_median=metrics.luminance_median,
            target_mean_chroma=metrics.mean_chroma,
            minimum_suitability_score=0.0,
        )

    async def _restore_or_raise(
        self,
        photo_id: str,
        snapshot_name: str,
        *,
        measurement_error: Exception | None = None,
    ) -> None:
        try:
            await self.bridge.restore_develop_snapshot(photo_id, snapshot_name)
        except Exception as restore_error:
            raise ParameterProbeRecoveryError(
                photo_id=photo_id,
                snapshot_name=snapshot_name,
                restore_error=restore_error,
                measurement_error=measurement_error,
            ) from restore_error


def _metric_delta(
    after: PhotoMetrics,
    before: PhotoMetrics,
    *,
    absolute: bool = False,
) -> PhotoMetricDelta:
    values = {field: getattr(after, field) - getattr(before, field) for field in _METRIC_FIELDS}
    if absolute:
        values = {field: abs(value) for field, value in values.items()}
    return PhotoMetricDelta.model_validate(values)
