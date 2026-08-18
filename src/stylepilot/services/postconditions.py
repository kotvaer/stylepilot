from __future__ import annotations

import math
from dataclasses import dataclass

from stylepilot.domain.models import (
    PhotoMetrics,
    RenderVerificationReport,
    StyleProfile,
)


@dataclass(frozen=True, slots=True)
class RenderedResultVerifier:
    """Validate Lightroom's rendered output using objective safety metrics."""

    maximum_distance_regression: float = 0.03
    meaningful_improvement_ratio: float = 0.02

    def verify(
        self,
        before: PhotoMetrics,
        after: PhotoMetrics,
        style: StyleProfile,
    ) -> RenderVerificationReport:
        before_distance = self._style_distance(before, style)
        after_distance = self._style_distance(after, style)
        improvement_ratio = self._improvement_ratio(before_distance, after_distance)

        issues: list[str] = []
        if after.shadow_clip_ratio > style.max_shadow_clip_ratio:
            issues.append(
                "Rendered shadow clipping exceeds the style safety threshold "
                f"({after.shadow_clip_ratio:.4f} > {style.max_shadow_clip_ratio:.4f})."
            )
        if after.highlight_clip_ratio > style.max_highlight_clip_ratio:
            issues.append(
                "Rendered highlight clipping exceeds the style safety threshold "
                f"({after.highlight_clip_ratio:.4f} > {style.max_highlight_clip_ratio:.4f})."
            )
        if after_distance > before_distance + self.maximum_distance_regression:
            issues.append(
                "The rendered result moved farther from the target style "
                f"({before_distance:.4f} -> {after_distance:.4f})."
            )

        warnings: list[str] = []
        if not issues and improvement_ratio < self.meaningful_improvement_ratio:
            warnings.append(
                "The rendered result produced no meaningful measurable style improvement."
            )

        return RenderVerificationReport(
            passed=not issues,
            before_style_distance=round(before_distance, 6),
            after_style_distance=round(after_distance, 6),
            improvement_ratio=round(improvement_ratio, 6),
            issues=tuple(issues),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _style_distance(metrics: PhotoMetrics, style: StyleProfile) -> float:
        luminance_error = (metrics.luminance_median - style.target_luminance_median) / 100.0
        chroma_error = (metrics.mean_chroma - style.target_mean_chroma) / 60.0
        components = [luminance_error, chroma_error]
        if style.target_luminance_contrast is not None:
            components.append(
                (metrics.luminance_contrast - style.target_luminance_contrast) / 100.0
            )
        if style.target_mean_lab_a is not None and style.target_mean_lab_b is not None:
            components.extend(
                (
                    (metrics.mean_lab_a - style.target_mean_lab_a) / 50.0,
                    (metrics.mean_lab_b - style.target_mean_lab_b) / 50.0,
                )
            )
        return math.sqrt(sum(component**2 for component in components))

    @staticmethod
    def _improvement_ratio(before_distance: float, after_distance: float) -> float:
        if before_distance <= 1e-9:
            return 0.0 if after_distance <= 1e-9 else -after_distance
        return (before_distance - after_distance) / before_distance
