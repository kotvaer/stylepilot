from __future__ import annotations

from pathlib import Path

from PIL import Image

from stylepilot.application.ports import SceneAnalysisError
from stylepilot.domain.models import PhotoMetadata, SceneType
from stylepilot.services import (
    ConstantSceneAnalyzer,
    ImageStatisticsAnalyzer,
    SceneAwareImageAnalyzer,
)


def test_scene_aware_analyzer_merges_manual_semantics(tmp_path: Path) -> None:
    image = tmp_path / "landscape.jpg"
    Image.new("RGB", (32, 24), (100, 130, 160)).save(image)
    analyzer = SceneAwareImageAnalyzer(
        numeric_analyzer=ImageStatisticsAnalyzer(),
        scene_analyzer=ConstantSceneAnalyzer(SceneType.LANDSCAPE),
    )

    metrics = analyzer.analyze(image, PhotoMetadata(photo_id="photo"))

    assert metrics.scene_type is SceneType.LANDSCAPE
    assert metrics.scene_tags == (SceneType.LANDSCAPE,)
    assert metrics.scene_confidence == 1
    assert metrics.scene_analysis_error is None


def test_scene_aware_analyzer_fails_closed_on_provider_error(tmp_path: Path) -> None:
    class FailingSceneAnalyzer:
        def analyze(self, image_path: Path):
            del image_path
            raise SceneAnalysisError("synthetic failure")

    image = tmp_path / "image.jpg"
    Image.new("RGB", (20, 20), (128, 128, 128)).save(image)
    analyzer = SceneAwareImageAnalyzer(
        numeric_analyzer=ImageStatisticsAnalyzer(),
        scene_analyzer=FailingSceneAnalyzer(),
    )

    metrics = analyzer.analyze(image, PhotoMetadata(photo_id="photo"))

    assert metrics.scene_type is SceneType.UNKNOWN
    assert metrics.scene_confidence == 0
    assert metrics.scene_analysis_error == "SceneAnalysisError"
