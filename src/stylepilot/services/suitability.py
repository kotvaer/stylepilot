from __future__ import annotations

import math

from stylepilot.domain.models import (
    PhotoMetrics,
    SceneCompatibility,
    SceneType,
    StyleProfile,
    SuitabilityReport,
)


class WeightedSuitabilityEngine:
    """Explainable baseline suitability score.

    This is intentionally a transparent baseline. It will later be calibrated
    on Lightroom experiments and user acceptance data rather than replaced by
    an opaque aesthetic score.
    """

    def evaluate(self, metrics: PhotoMetrics, style: StyleProfile) -> SuitabilityReport:
        luminance_distance = abs(metrics.luminance_median - style.target_luminance_median) / 100.0
        chroma_distance = min(
            1.0,
            abs(metrics.mean_chroma - style.target_mean_chroma) / 60.0,
        )
        contrast_distance = self._optional_contrast_distance(metrics, style)
        color_axis_distance = self._optional_color_axis_distance(metrics, style)
        shadow_excess = max(0.0, metrics.shadow_clip_ratio - style.max_shadow_clip_ratio)
        highlight_excess = max(
            0.0,
            metrics.highlight_clip_ratio - style.max_highlight_clip_ratio,
        )
        upward_shift = max(0.0, style.target_luminance_median - metrics.luminance_median)
        lift_noise_risk = metrics.noise_risk * min(1.0, upward_shift / 35.0)

        scene_compatibility = self._scene_compatibility(metrics, style)
        scene_penalty = {
            SceneCompatibility.NOT_REQUIRED: 0.0,
            SceneCompatibility.MATCH: 0.0,
            SceneCompatibility.UNKNOWN: 0.20,
            SceneCompatibility.MISMATCH: 0.55,
        }[scene_compatibility]

        loss = (
            (0.35 * luminance_distance)
            + (0.20 * chroma_distance)
            + (0.05 * contrast_distance)
            + (0.10 * color_axis_distance)
            + (2.5 * shadow_excess)
            + (2.5 * highlight_excess)
            + (0.25 * lift_noise_risk)
            + scene_penalty
        )
        score = max(0.0, min(100.0, 100.0 * (1.0 - loss)))
        semantic_gate_passed = scene_compatibility in {
            SceneCompatibility.NOT_REQUIRED,
            SceneCompatibility.MATCH,
        }
        eligible = score >= style.minimum_suitability_score and semantic_gate_passed
        recommended_strength = 0.0 if not eligible else min(1.0, max(0.25, score / 85.0))

        reasons = self._reasons(
            metrics,
            style,
            luminance_distance,
            color_axis_distance,
            lift_noise_risk,
            scene_compatibility,
        )
        return SuitabilityReport(
            score=round(score, 2),
            eligible=eligible,
            recommended_strength=round(recommended_strength, 2),
            reasons=tuple(reasons),
            scene_compatibility=scene_compatibility,
        )

    @staticmethod
    def _reasons(
        metrics: PhotoMetrics,
        style: StyleProfile,
        luminance_distance: float,
        color_axis_distance: float,
        lift_noise_risk: float,
        scene_compatibility: SceneCompatibility,
    ) -> list[str]:
        reasons: list[str] = []
        if luminance_distance > 0.25:
            reasons.append("The source brightness is far from the target style.")
        else:
            reasons.append("The source brightness is within a recoverable range.")
        if lift_noise_risk > 0.25:
            reasons.append("Lifting the source toward the target may amplify shadow noise.")
        if color_axis_distance > 0.35:
            reasons.append("The source palette is far from the style's Lab color center.")
        if metrics.shadow_clip_ratio > style.max_shadow_clip_ratio:
            reasons.append("The source exceeds the style's shadow clipping allowance.")
        if metrics.highlight_clip_ratio > style.max_highlight_clip_ratio:
            reasons.append("The source exceeds the style's highlight clipping allowance.")
        if scene_compatibility is SceneCompatibility.MISMATCH:
            preferred = ", ".join(scene.value for scene in style.preferred_scene_types)
            reasons.append(
                f"The source scene ({metrics.scene_type.value}) does not match the "
                f"style's preferred scenes ({preferred})."
            )
        elif scene_compatibility is SceneCompatibility.UNKNOWN:
            reason = "Scene compatibility could not be verified, so semantic safety failed closed."
            if metrics.scene_analysis_error is not None:
                reason += f" Provider status: {metrics.scene_analysis_error}."
            reasons.append(reason)
        if len(reasons) == 1:
            reasons.append("No major technical compatibility risk was detected.")
        return reasons

    @staticmethod
    def _scene_compatibility(
        metrics: PhotoMetrics,
        style: StyleProfile,
    ) -> SceneCompatibility:
        if not style.preferred_scene_types:
            return SceneCompatibility.NOT_REQUIRED
        if metrics.scene_type is SceneType.UNKNOWN or metrics.scene_confidence < 0.6:
            return SceneCompatibility.UNKNOWN
        source_scenes = {metrics.scene_type, *metrics.scene_tags}
        if source_scenes.intersection(style.preferred_scene_types):
            return SceneCompatibility.MATCH
        return SceneCompatibility.MISMATCH

    @staticmethod
    def _optional_contrast_distance(metrics: PhotoMetrics, style: StyleProfile) -> float:
        if style.target_luminance_contrast is None:
            return 0.0
        return min(
            1.0,
            abs(metrics.luminance_contrast - style.target_luminance_contrast) / 100.0,
        )

    @staticmethod
    def _optional_color_axis_distance(metrics: PhotoMetrics, style: StyleProfile) -> float:
        if style.target_mean_lab_a is None or style.target_mean_lab_b is None:
            return 0.0
        return min(
            1.0,
            math.hypot(
                metrics.mean_lab_a - style.target_mean_lab_a,
                metrics.mean_lab_b - style.target_mean_lab_b,
            )
            / 50.0,
        )
