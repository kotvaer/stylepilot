from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from stylepilot.application.ports import SceneAnalyzer
from stylepilot.domain.models import (
    PhotoMetadata,
    PhotoMetrics,
    ReferenceImageAssessment,
    SceneAnalysis,
    SceneType,
    StyleFeatureSpread,
    StyleProfile,
    StyleProfileBuildResult,
)
from stylepilot.services.image_analysis import ImageStatisticsAnalyzer

_FEATURE_NAMES = (
    "luminance median",
    "luminance contrast",
    "mean chroma",
    "Lab a axis",
    "Lab b axis",
    "shadow clipping share",
    "highlight clipping share",
)
_FEATURE_SCALE_FLOORS = np.array(
    (2.0, 2.0, 1.0, 1.0, 1.0, 0.02, 0.02),
    dtype=np.float64,
)
_CONSISTENCY_WARNING_SPREADS = np.array(
    (15.0, 20.0, 8.0, 8.0, 8.0, 0.05, 0.05),
    dtype=np.float64,
)


@dataclass(frozen=True, slots=True)
class MultiReferenceStyleProfiler:
    """Build a robust style center from a small set of rendered references.

    References remain part of the result even when they are flagged as possible
    outliers. The caller must make the curation decision explicitly and rebuild.
    """

    analyzer: ImageStatisticsAnalyzer = field(default_factory=ImageStatisticsAnalyzer)
    minimum_references: int = 5
    maximum_references: int = 20
    outlier_z_threshold: float = 3.5
    algorithm_version: str = "global-lab-v1"
    scene_analyzer: SceneAnalyzer | None = None

    def build(
        self,
        *,
        profile_id: str,
        name: str,
        reference_paths: tuple[Path, ...],
        preferred_scene_types: tuple[SceneType, ...] = (),
    ) -> StyleProfileBuildResult:
        paths = self._validate_paths(reference_paths)
        analyzed = tuple(self._analyze(path, index) for index, path in enumerate(paths))
        metrics = tuple(item[0] for item in analyzed)
        scenes = tuple(item[1] for item in analyzed)
        matrix = np.stack([self._feature_vector(value) for value in metrics])
        center = np.median(matrix, axis=0)
        spread = 1.4826 * np.median(np.abs(matrix - center), axis=0)
        scoring_scale = np.maximum(spread, _FEATURE_SCALE_FLOORS)
        z_scores = np.abs((matrix - center) / scoring_scale)
        robust_scores = np.max(z_scores, axis=1)
        candidate_indices = self._candidate_indices(robust_scores)

        assessments = tuple(
            self._assessment(
                path,
                value,
                score,
                z_score,
                is_outlier=index in candidate_indices,
                scene_analysis=scenes[index],
            )
            for index, (path, value, score, z_score) in enumerate(
                zip(paths, metrics, robust_scores, z_scores, strict=True)
            )
        )
        resolved_scene_types, semantic_warnings = self._resolve_scene_types(
            preferred_scene_types,
            scenes,
        )
        warnings = (*self._warnings(assessments, spread), *semantic_warnings)
        semantic_scenes = tuple(scene for scene in scenes if scene is not None)
        profile = StyleProfile(
            id=profile_id,
            name=name,
            target_luminance_median=float(center[0]),
            target_luminance_contrast=float(center[1]),
            target_mean_chroma=float(center[2]),
            target_mean_lab_a=float(center[3]),
            target_mean_lab_b=float(center[4]),
            target_shadow_clip_ratio=float(center[5]),
            target_highlight_clip_ratio=float(center[6]),
            feature_spread=StyleFeatureSpread(
                luminance_median=float(spread[0]),
                luminance_contrast=float(spread[1]),
                mean_chroma=float(spread[2]),
                mean_lab_a=float(spread[3]),
                mean_lab_b=float(spread[4]),
                shadow_clip_ratio=float(spread[5]),
                highlight_clip_ratio=float(spread[6]),
            ),
            reference_count=len(paths),
            algorithm_version=(
                f"{self.algorithm_version}+scene-v1" if semantic_scenes else self.algorithm_version
            ),
            semantic_reference_count=len(semantic_scenes),
            semantic_provider=self._single_value(
                tuple(scene.provider for scene in semantic_scenes)
            ),
            semantic_model=self._single_value(tuple(scene.model for scene in semantic_scenes)),
            semantic_prompt_version=self._single_value(
                tuple(scene.prompt_version for scene in semantic_scenes)
            ),
            preferred_scene_types=resolved_scene_types,
        )
        return StyleProfileBuildResult(
            profile=profile,
            references=assessments,
            warnings=warnings,
        )

    def _validate_paths(self, reference_paths: tuple[Path, ...]) -> tuple[Path, ...]:
        count = len(reference_paths)
        if not self.minimum_references <= count <= self.maximum_references:
            msg = (
                f"A style profile requires {self.minimum_references} to "
                f"{self.maximum_references} reference images, got {count}."
            )
            raise ValueError(msg)

        paths = tuple(path.expanduser().resolve() for path in reference_paths)
        if len(set(paths)) != len(paths):
            msg = "Reference image paths must be unique."
            raise ValueError(msg)
        missing = tuple(path for path in paths if not path.is_file())
        if missing:
            msg = f"Reference image does not exist: {missing[0]}"
            raise FileNotFoundError(msg)
        return paths

    def _analyze(
        self,
        path: Path,
        index: int,
    ) -> tuple[PhotoMetrics, SceneAnalysis | None]:
        metrics = self.analyzer.analyze(
            path,
            PhotoMetadata(photo_id=f"reference-{index + 1}"),
        )
        if self.scene_analyzer is None:
            return metrics, None
        scene = self.scene_analyzer.analyze(path)
        tags = tuple(dict.fromkeys((scene.primary_scene, *scene.scene_tags)))
        return (
            PhotoMetrics.model_validate(
                {
                    **metrics.model_dump(),
                    "scene_type": scene.primary_scene,
                    "scene_tags": tags,
                    "scene_confidence": scene.confidence,
                    "has_face": scene.has_face,
                }
            ),
            scene,
        )

    @staticmethod
    def _feature_vector(metrics: PhotoMetrics) -> np.ndarray:
        return np.array(
            (
                metrics.luminance_median,
                metrics.luminance_contrast,
                metrics.mean_chroma,
                metrics.mean_lab_a,
                metrics.mean_lab_b,
                metrics.shadow_clip_ratio,
                metrics.highlight_clip_ratio,
            ),
            dtype=np.float64,
        )

    def _assessment(
        self,
        path: Path,
        metrics: PhotoMetrics,
        robust_score: np.float64,
        z_scores: np.ndarray,
        *,
        is_outlier: bool,
        scene_analysis: SceneAnalysis | None,
    ) -> ReferenceImageAssessment:
        reasons: tuple[str, ...] = ()
        if is_outlier:
            feature_index = int(np.argmax(z_scores))
            reasons = (
                f"{_FEATURE_NAMES[feature_index]} is far from the robust style center.",
                "Review this image before excluding it; scene content can also cause the gap.",
            )
        return ReferenceImageAssessment(
            path=path,
            metrics=metrics,
            robust_z_score=float(robust_score),
            is_outlier_candidate=is_outlier,
            reasons=reasons,
            scene_analysis=scene_analysis,
        )

    def _candidate_indices(self, robust_scores: np.ndarray) -> frozenset[int]:
        qualifying = np.flatnonzero(robust_scores >= self.outlier_z_threshold)
        maximum_candidates = max(1, len(robust_scores) // 5)
        ranked = sorted(
            (int(index) for index in qualifying),
            key=lambda index: (-robust_scores[index], index),
        )
        return frozenset(ranked[:maximum_candidates])

    @staticmethod
    def _resolve_scene_types(
        preferred_scene_types: tuple[SceneType, ...],
        scenes: tuple[SceneAnalysis | None, ...],
    ) -> tuple[tuple[SceneType, ...], tuple[str, ...]]:
        explicit = tuple(dict.fromkeys(preferred_scene_types))
        if SceneType.UNKNOWN in explicit:
            msg = "unknown cannot be a preferred style scene type."
            raise ValueError(msg)
        if explicit:
            return explicit, ()

        confident = tuple(
            scene.primary_scene
            for scene in scenes
            if scene is not None
            and scene.confidence >= 0.6
            and scene.primary_scene is not SceneType.UNKNOWN
        )
        if not confident:
            if any(scene is not None for scene in scenes):
                return (), (
                    "The semantic provider found no confident shared scene type; "
                    "set preferred scenes explicitly or curate the references.",
                )
            return (), ()

        counts = Counter(confident)
        minimum_count = max(2, math.ceil(len(scenes) * 0.4))
        resolved = tuple(
            scene_type for scene_type, count in counts.most_common() if count >= minimum_count
        )
        if resolved:
            return resolved, ()
        return (), (
            "The reference scenes are heterogeneous; split the set into scene-specific "
            "styles or set preferred scenes explicitly.",
        )

    @staticmethod
    def _single_value(values: tuple[str, ...]) -> str | None:
        unique = tuple(dict.fromkeys(values))
        return unique[0] if len(unique) == 1 else None

    def _warnings(
        self,
        assessments: tuple[ReferenceImageAssessment, ...],
        spread: np.ndarray,
    ) -> tuple[str, ...]:
        warnings: list[str] = []
        candidates = tuple(item for item in assessments if item.is_outlier_candidate)
        if candidates:
            warnings.append(
                f"{len(candidates)} reference image(s) are possible outliers; "
                "review them and rebuild explicitly."
            )
        inconsistent_features = [
            name
            for name, value, threshold in zip(
                _FEATURE_NAMES,
                spread,
                _CONSISTENCY_WARNING_SPREADS,
                strict=True,
            )
            if value > threshold
        ]
        if inconsistent_features:
            warnings.append(
                "The reference set has high dispersion in: "
                + ", ".join(inconsistent_features)
                + ". Consider splitting it into scene-specific sub-styles."
            )
        return tuple(warnings)
