from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from uuid import uuid4

from stylepilot.application.ports import LightroomBridge, PhotoAnalyzer
from stylepilot.application.probing import _METRIC_FIELDS, _metric_delta
from stylepilot.domain.models import (
    ActuatorCalibrationManifest,
    ActuatorCalibrationReport,
    CalibrationPhotoReport,
    CalibrationPointStatus,
    CalibrationRunStatus,
    CalibrationSampleReport,
    DevelopSettings,
    DevelopSnapshotRef,
    PhotoMetricDelta,
    PhotoMetrics,
    PhotoRef,
)


class CalibrationRecoveryError(RuntimeError):
    """A calibration point could not restore its Lightroom recovery snapshot."""

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
        super().__init__(
            f"Calibration could not restore {snapshot_name!r} on photo {photo_id}. "
            f"Use `stylepilot lightroom rollback --photo-id {photo_id} "
            f"--snapshot-name {snapshot_name!r}` before continuing."
        )


@dataclass(frozen=True, slots=True)
class LightroomActuatorCalibrator:
    """Measure bounded Lightroom response curves on disposable virtual copies."""

    bridge: LightroomBridge
    analyzer: PhotoAnalyzer
    readback_tolerance: float = 1e-4
    maximum_photos: int = 20

    def __post_init__(self) -> None:
        if self.readback_tolerance < 0:
            msg = "readback_tolerance must not be negative"
            raise ValueError(msg)
        if self.maximum_photos < 1:
            msg = "maximum_photos must be positive"
            raise ValueError(msg)

    async def run(self, manifest: ActuatorCalibrationManifest) -> ActuatorCalibrationReport:
        selected = await self.bridge.get_selected_photos()
        if not selected:
            msg = "Select between 1 and 20 Lightroom photos before calibration."
            raise ValueError(msg)
        if len(selected) > self.maximum_photos:
            msg = (
                f"Calibration is bounded to {self.maximum_photos} photos; "
                f"{len(selected)} are selected."
            )
            raise ValueError(msg)

        run_id = uuid4().hex
        created_at = datetime.now(UTC)
        planned_samples = len(selected) * manifest.sample_points
        planned_renders = len(selected) * (
            manifest.baseline_repeats + 1 + (2 * manifest.sample_points)
        )
        approved = await self.bridge.request_calibration_approval(selected, manifest)
        if not approved:
            now = datetime.now(UTC)
            return ActuatorCalibrationReport(
                run_id=run_id,
                created_at=created_at,
                completed_at=now,
                status=CalibrationRunStatus.REJECTED,
                manifest=manifest,
                selected_photos=selected,
                planned_sample_count=planned_samples,
                planned_render_count=planned_renders,
                duration_seconds=(now - created_at).total_seconds(),
                warnings=("The photographer rejected the Lightroom calibration run.",),
            )

        photo_reports = tuple(
            [await self._calibrate_photo(source, manifest) for source in selected]
        )
        samples = tuple(sample for photo_report in photo_reports for sample in photo_report.samples)
        failed_count = sum(sample.status is CalibrationPointStatus.FAILED for sample in samples)
        completed_count = len(samples) - failed_count
        warnings: list[str] = []
        if failed_count:
            warnings.append(
                f"{failed_count} sample point(s) failed after safe restoration; inspect errors."
            )

        completed_at = datetime.now(UTC)
        return ActuatorCalibrationReport(
            run_id=run_id,
            created_at=created_at,
            completed_at=completed_at,
            status=(
                CalibrationRunStatus.COMPLETED_WITH_ERRORS
                if failed_count
                else CalibrationRunStatus.COMPLETED
            ),
            manifest=manifest,
            selected_photos=selected,
            planned_sample_count=planned_samples,
            planned_render_count=planned_renders,
            duration_seconds=(completed_at - created_at).total_seconds(),
            completed_sample_count=completed_count,
            failed_sample_count=failed_count,
            photos=photo_reports,
            warnings=tuple(warnings),
        )

    async def _calibrate_photo(
        self,
        source: PhotoRef,
        manifest: ActuatorCalibrationManifest,
    ) -> CalibrationPhotoReport:
        source_metadata = await self.bridge.get_photo_metadata(source.id)
        baseline_measurements: list[PhotoMetrics] = []
        for _ in range(manifest.baseline_repeats):
            preview = await self.bridge.render_analysis_preview(source.id)
            baseline_measurements.append(self.analyzer.analyze(preview, source_metadata))
        baseline_metrics = _median_metrics(tuple(baseline_measurements))

        calibration_photo = await self.bridge.create_virtual_copy(
            source.id,
            f"StylePilot Calibration {manifest.id}",
        )
        copy_metadata = await self.bridge.get_photo_metadata(calibration_photo.id)
        copy_preview = await self.bridge.render_analysis_preview(calibration_photo.id)
        copy_baseline_metrics = self.analyzer.analyze(copy_preview, copy_metadata)
        inheritance_drift = _metric_delta(
            copy_baseline_metrics,
            baseline_metrics,
            absolute=True,
        )
        warnings: list[str] = []
        if any(
            abs(
                copy_metadata.develop_settings.get(parameter)
                - source_metadata.develop_settings.get(parameter)
            )
            > self.readback_tolerance
            for parameter in (sweep.parameter for sweep in manifest.parameters)
        ):
            warnings.append(
                "The calibration virtual copy did not inherit every sampled setting exactly."
            )

        samples: list[CalibrationSampleReport] = []
        for sweep in manifest.parameters:
            for value in sweep.values:
                samples.append(
                    await self._sample_point(
                        calibration_photo,
                        copy_baseline_metrics,
                        sweep.parameter,
                        value,
                    )
                )

        return CalibrationPhotoReport(
            source_photo=source,
            calibration_photo=calibration_photo,
            baseline_measurements=tuple(baseline_measurements),
            baseline_metrics=baseline_metrics,
            baseline_repeatability_drift=_maximum_drift(
                tuple(baseline_measurements), baseline_metrics
            ),
            calibration_copy_baseline_metrics=copy_baseline_metrics,
            copy_inheritance_drift=inheritance_drift,
            samples=tuple(samples),
            warnings=tuple(warnings),
        )

    async def _sample_point(
        self,
        photo: PhotoRef,
        baseline_metrics: PhotoMetrics,
        parameter: str,
        requested_value: float,
    ) -> CalibrationSampleReport:
        before_metadata = await self.bridge.get_photo_metadata(photo.id)
        before_value = before_metadata.develop_settings.get(parameter)
        settings = DevelopSettings(values={parameter: requested_value})
        snapshot: DevelopSnapshotRef | None = None
        try:
            snapshot = await self.bridge.apply_develop_settings(
                photo.id,
                settings,
                f"StylePilot Calibration {parameter} {requested_value:g}",
            )
        except Exception as error:
            return CalibrationSampleReport(
                status=CalibrationPointStatus.FAILED,
                parameter=parameter,
                requested_value=requested_value,
                before_value=before_value,
                error=f"Apply failed after automatic rollback: {error}",
            )

        applied_value: float | None = None
        applied_metrics: PhotoMetrics | None = None
        try:
            applied_metadata = await self.bridge.get_photo_metadata(photo.id)
            applied_value = applied_metadata.develop_settings.get(parameter)
            applied_preview = await self.bridge.render_analysis_preview(photo.id)
            applied_metrics = self.analyzer.analyze(applied_preview, applied_metadata)
        except Exception as measurement_error:
            await self._restore_or_raise(
                photo.id,
                snapshot.name,
                measurement_error=measurement_error,
            )
            return await self._failed_after_restoration(
                photo=photo,
                baseline_metrics=baseline_metrics,
                parameter=parameter,
                requested_value=requested_value,
                before_value=before_value,
                snapshot=snapshot,
                applied_value=applied_value,
                applied_metrics=applied_metrics,
                error=f"Applied render measurement failed: {measurement_error}",
            )

        await self._restore_or_raise(photo.id, snapshot.name)
        try:
            restored_metadata = await self.bridge.get_photo_metadata(photo.id)
            restored_value = restored_metadata.develop_settings.get(parameter)
            restored_preview = await self.bridge.render_analysis_preview(photo.id)
            restored_metrics = self.analyzer.analyze(restored_preview, restored_metadata)
        except Exception as error:
            return CalibrationSampleReport(
                status=CalibrationPointStatus.FAILED,
                parameter=parameter,
                requested_value=requested_value,
                before_value=before_value,
                applied_value=applied_value,
                applied_readback_error=abs(applied_value - requested_value),
                applied_readback_matches=(
                    abs(applied_value - requested_value) <= self.readback_tolerance
                ),
                applied_metrics=applied_metrics,
                applied_metric_delta=_metric_delta(applied_metrics, baseline_metrics),
                recovery_snapshot=snapshot,
                error=f"Post-restoration measurement failed; Lightroom was restored: {error}",
            )

        applied_error = abs(applied_value - requested_value)
        restoration_error = abs(restored_value - before_value)
        warnings: list[str] = []
        if applied_error > self.readback_tolerance:
            warnings.append("Lightroom readback differs from the requested value.")
        if restoration_error > self.readback_tolerance:
            warnings.append("Restored readback differs from the pre-sample value.")

        return CalibrationSampleReport(
            status=CalibrationPointStatus.COMPLETED,
            parameter=parameter,
            requested_value=requested_value,
            before_value=before_value,
            applied_value=applied_value,
            restored_value=restored_value,
            applied_readback_error=applied_error,
            restoration_readback_error=restoration_error,
            applied_readback_matches=applied_error <= self.readback_tolerance,
            restoration_readback_matches=restoration_error <= self.readback_tolerance,
            applied_metrics=applied_metrics,
            restored_metrics=restored_metrics,
            applied_metric_delta=_metric_delta(applied_metrics, baseline_metrics),
            restoration_metric_drift=_metric_delta(
                restored_metrics,
                baseline_metrics,
                absolute=True,
            ),
            recovery_snapshot=snapshot,
            warnings=tuple(warnings),
        )

    async def _failed_after_restoration(
        self,
        *,
        photo: PhotoRef,
        baseline_metrics: PhotoMetrics,
        parameter: str,
        requested_value: float,
        before_value: float,
        snapshot: DevelopSnapshotRef,
        applied_value: float | None,
        applied_metrics: PhotoMetrics | None,
        error: str,
    ) -> CalibrationSampleReport:
        restored_value: float | None = None
        restored_metrics: PhotoMetrics | None = None
        restoration_error: float | None = None
        restoration_drift: PhotoMetricDelta | None = None
        try:
            restored_metadata = await self.bridge.get_photo_metadata(photo.id)
            restored_value = restored_metadata.develop_settings.get(parameter)
            restoration_error = abs(restored_value - before_value)
            restored_preview = await self.bridge.render_analysis_preview(photo.id)
            restored_metrics = self.analyzer.analyze(restored_preview, restored_metadata)
            restoration_drift = _metric_delta(
                restored_metrics,
                baseline_metrics,
                absolute=True,
            )
        except Exception as restoration_measurement_error:
            error = f"{error}; restoration verification failed: {restoration_measurement_error}"

        applied_error = abs(applied_value - requested_value) if applied_value is not None else None
        return CalibrationSampleReport(
            status=CalibrationPointStatus.FAILED,
            parameter=parameter,
            requested_value=requested_value,
            before_value=before_value,
            applied_value=applied_value,
            restored_value=restored_value,
            applied_readback_error=applied_error,
            restoration_readback_error=restoration_error,
            applied_readback_matches=(
                applied_error <= self.readback_tolerance if applied_error is not None else None
            ),
            restoration_readback_matches=(
                restoration_error <= self.readback_tolerance
                if restoration_error is not None
                else None
            ),
            applied_metrics=applied_metrics,
            restored_metrics=restored_metrics,
            applied_metric_delta=(
                _metric_delta(applied_metrics, baseline_metrics)
                if applied_metrics is not None
                else None
            ),
            restoration_metric_drift=restoration_drift,
            recovery_snapshot=snapshot,
            error=error,
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
            raise CalibrationRecoveryError(
                photo_id=photo_id,
                snapshot_name=snapshot_name,
                restore_error=restore_error,
                measurement_error=measurement_error,
            ) from restore_error


def _median_metrics(measurements: tuple[PhotoMetrics, ...]) -> PhotoMetrics:
    if not measurements:
        msg = "At least one metric measurement is required"
        raise ValueError(msg)
    numeric = {
        field: median(getattr(measurement, field) for measurement in measurements)
        for field in _METRIC_FIELDS
    }
    return measurements[0].model_copy(update=numeric)


def _maximum_drift(
    measurements: tuple[PhotoMetrics, ...],
    center: PhotoMetrics,
) -> PhotoMetricDelta:
    values = {
        field: max(
            abs(getattr(measurement, field) - getattr(center, field))
            for measurement in measurements
        )
        for field in _METRIC_FIELDS
    }
    return PhotoMetricDelta.model_validate(values)
