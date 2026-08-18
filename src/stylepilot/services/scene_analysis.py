from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stylepilot.application.ports import PhotoAnalyzer, SceneAnalysisError, SceneAnalyzer
from stylepilot.domain.models import PhotoMetadata, PhotoMetrics, SceneAnalysis, SceneType


@dataclass(frozen=True, slots=True)
class SceneAwareImageAnalyzer:
    """Decorate deterministic image statistics with optional scene semantics."""

    numeric_analyzer: PhotoAnalyzer
    scene_analyzer: SceneAnalyzer

    def analyze(self, preview_path: Path, metadata: PhotoMetadata) -> PhotoMetrics:
        metrics = self.numeric_analyzer.analyze(preview_path, metadata)
        try:
            scene = self.scene_analyzer.analyze(preview_path)
        except SceneAnalysisError as error:
            return PhotoMetrics.model_validate(
                {
                    **metrics.model_dump(),
                    "scene_analysis_error": type(error).__name__,
                }
            )
        tags = scene.scene_tags
        if scene.primary_scene not in tags:
            tags = (scene.primary_scene, *tags)
        return PhotoMetrics.model_validate(
            {
                **metrics.model_dump(),
                "scene_type": scene.primary_scene,
                "scene_tags": tuple(dict.fromkeys(tags)),
                "scene_confidence": scene.confidence,
                "has_face": scene.has_face,
            }
        )


@dataclass(frozen=True, slots=True)
class ConstantSceneAnalyzer:
    """Explicit manual override used for diagnostics and deterministic tests."""

    scene_type: SceneType

    def analyze(self, image_path: Path) -> SceneAnalysis:
        del image_path
        return SceneAnalysis(
            primary_scene=self.scene_type,
            scene_tags=(self.scene_type,),
            confidence=1.0,
            has_person=self.scene_type in {SceneType.PORTRAIT, SceneType.PEOPLE},
            has_face=self.scene_type is SceneType.PORTRAIT,
            dominant_subjects=(),
            evidence=("Scene type supplied by an explicit CLI override.",),
            provider="manual",
            model="manual-override",
            prompt_version="manual-v1",
        )
