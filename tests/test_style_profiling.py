from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from stylepilot.domain.models import SceneType
from stylepilot.services import ConstantSceneAnalyzer, MultiReferenceStyleProfiler


def save_reference(path: Path, color: tuple[int, int, int]) -> Path:
    Image.new("RGB", (32, 24), color).save(path)
    return path


def test_profiler_builds_robust_center_and_flags_color_outlier(tmp_path: Path) -> None:
    references = tuple(
        save_reference(tmp_path / f"neutral-{index}.png", (value, value, value))
        for index, value in enumerate((116, 118, 120, 122, 124), start=1)
    )
    outlier = save_reference(tmp_path / "red.png", (240, 20, 20))

    result = MultiReferenceStyleProfiler().build(
        profile_id="quiet-neutral",
        name="Quiet Neutral",
        reference_paths=(*references, outlier),
    )

    assert result.profile.reference_count == 6
    assert result.profile.algorithm_version == "global-lab-v1"
    assert result.profile.target_mean_chroma < 1
    assert result.profile.target_luminance_contrast == pytest.approx(0, abs=0.01)
    assert result.profile.target_shadow_clip_ratio == 0
    assert result.profile.target_highlight_clip_ratio == 0
    assert result.profile.feature_spread is not None
    assert result.references[-1].path == outlier.resolve()
    assert result.references[-1].is_outlier_candidate is True
    assert sum(item.is_outlier_candidate for item in result.references) == 1
    assert "possible outliers" in result.warnings[0]


def test_profiler_requires_five_unique_existing_references(tmp_path: Path) -> None:
    reference = save_reference(tmp_path / "reference.png", (128, 128, 128))
    profiler = MultiReferenceStyleProfiler()

    with pytest.raises(ValueError, match="requires 5 to 20"):
        profiler.build(
            profile_id="too-small",
            name="Too Small",
            reference_paths=(reference,) * 4,
        )

    with pytest.raises(ValueError, match="must be unique"):
        profiler.build(
            profile_id="duplicates",
            name="Duplicates",
            reference_paths=(reference,) * 5,
        )


def test_profiler_reports_missing_reference(tmp_path: Path) -> None:
    paths = tuple(
        save_reference(tmp_path / f"reference-{index}.png", (128, 128, 128)) for index in range(4)
    )

    with pytest.raises(FileNotFoundError, match="Reference image does not exist"):
        MultiReferenceStyleProfiler().build(
            profile_id="missing",
            name="Missing",
            reference_paths=(*paths, tmp_path / "missing.png"),
        )


def test_profiler_derives_shared_scene_from_semantic_references(tmp_path: Path) -> None:
    references = tuple(
        save_reference(tmp_path / f"portrait-{index}.jpg", (120 + index, 110, 100))
        for index in range(5)
    )
    profiler = MultiReferenceStyleProfiler(scene_analyzer=ConstantSceneAnalyzer(SceneType.PORTRAIT))

    result = profiler.build(
        profile_id="portrait-style",
        name="Portrait Style",
        reference_paths=references,
    )

    assert result.profile.preferred_scene_types == (SceneType.PORTRAIT,)
    assert result.profile.semantic_reference_count == 5
    assert result.profile.semantic_provider == "manual"
    assert result.profile.algorithm_version == "global-lab-v1+scene-v1"
    assert all(reference.scene_analysis is not None for reference in result.references)


def test_profiler_accepts_explicit_preferred_scenes_without_provider(tmp_path: Path) -> None:
    references = tuple(
        save_reference(tmp_path / f"landscape-{index}.jpg", (100, 120 + index, 140))
        for index in range(5)
    )

    result = MultiReferenceStyleProfiler().build(
        profile_id="landscape-style",
        name="Landscape Style",
        reference_paths=references,
        preferred_scene_types=(SceneType.LANDSCAPE,),
    )

    assert result.profile.preferred_scene_types == (SceneType.LANDSCAPE,)
    assert result.profile.semantic_reference_count == 0
