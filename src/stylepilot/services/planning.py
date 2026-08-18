from __future__ import annotations

from stylepilot.domain.models import (
    DevelopSettings,
    EditPlan,
    PhotoMetadata,
    PhotoMetrics,
    StyleProfile,
)


class RuleBasedEditPlanner:
    """Explainable baseline planner used before the learned Lightroom surrogate."""

    def plan(
        self,
        metadata: PhotoMetadata,
        metrics: PhotoMetrics,
        style: StyleProfile,
        strength: float,
    ) -> EditPlan:
        current = metadata.develop_settings
        luminance_delta = style.target_luminance_median - metrics.luminance_median
        exposure_delta = self._clamp(luminance_delta / 20.0, -2.5, 2.5) * strength
        chroma_delta = style.target_mean_chroma - metrics.mean_chroma
        vibrance_delta = self._clamp(chroma_delta * 1.5, -30.0, 30.0) * strength

        highlights_delta = -min(35.0, metrics.highlight_clip_ratio * 350.0) * strength
        shadows_delta = min(30.0, metrics.shadow_clip_ratio * 300.0) * strength

        values = {
            "Exposure2012": self._clamp(
                current.get("Exposure2012") + exposure_delta,
                -5.0,
                5.0,
            ),
            "Highlights2012": self._clamp(
                current.get("Highlights2012") + highlights_delta,
                -100.0,
                100.0,
            ),
            "Shadows2012": self._clamp(
                current.get("Shadows2012") + shadows_delta,
                -100.0,
                100.0,
            ),
            "Vibrance": self._clamp(
                current.get("Vibrance") + vibrance_delta,
                -100.0,
                100.0,
            ),
        }
        rationale = [
            "Move the rendered median lightness toward the style target.",
            "Protect clipped highlights and recover clipped shadows within safe limits.",
            "Move chroma with Vibrance before using stronger channel-specific edits.",
        ]

        if style.target_luminance_contrast is not None:
            contrast_delta = (
                self._clamp(
                    (style.target_luminance_contrast - metrics.luminance_contrast) * 1.25,
                    -30.0,
                    30.0,
                )
                * strength
            )
            values["Contrast2012"] = self._clamp(
                current.get("Contrast2012") + contrast_delta,
                -100.0,
                100.0,
            )
            rationale.append("Move global contrast toward the measured style distribution.")

        highlight_target = (
            style.target_highlight_clip_ratio
            if style.target_highlight_clip_ratio is not None
            else style.max_highlight_clip_ratio
        )
        highlight_excess = max(0.0, metrics.highlight_clip_ratio - highlight_target)
        if highlight_excess > 0:
            whites_delta = -min(35.0, highlight_excess * 2500.0) * strength
            values["Whites2012"] = self._clamp(
                current.get("Whites2012") + whites_delta,
                -100.0,
                100.0,
            )
            rationale.append("Lower Whites when rendered highlight clipping exceeds the target.")

        shadow_target = (
            style.target_shadow_clip_ratio
            if style.target_shadow_clip_ratio is not None
            else style.max_shadow_clip_ratio
        )
        shadow_excess = max(0.0, metrics.shadow_clip_ratio - shadow_target)
        if shadow_excess > 0:
            blacks_delta = min(35.0, shadow_excess * 2500.0) * strength
            values["Blacks2012"] = self._clamp(
                current.get("Blacks2012") + blacks_delta,
                -100.0,
                100.0,
            )
            rationale.append("Raise Blacks when rendered shadow clipping exceeds the target.")

        settings = DevelopSettings(values=values)
        return EditPlan(
            source_photo_id=metadata.photo_id,
            style_profile_id=style.id,
            strength=strength,
            settings=settings,
            rationale=tuple(rationale),
        )

    @staticmethod
    def _clamp(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))
