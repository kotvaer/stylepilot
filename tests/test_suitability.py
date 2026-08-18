from stylepilot.domain.models import (
    PhotoMetrics,
    SceneCompatibility,
    SceneType,
    StyleProfile,
)
from stylepilot.services.suitability import WeightedSuitabilityEngine


def make_metrics(
    *,
    luminance: float,
    chroma: float,
    noise: float,
    lab_a: float = 0,
    lab_b: float = 0,
    scene_type: SceneType = SceneType.UNKNOWN,
    scene_confidence: float = 0,
) -> PhotoMetrics:
    return PhotoMetrics(
        luminance_median=luminance,
        mean_chroma=chroma,
        shadow_clip_ratio=0.0,
        highlight_clip_ratio=0.0,
        noise_risk=noise,
        mean_lab_a=lab_a,
        mean_lab_b=lab_b,
        scene_type=scene_type,
        scene_tags=(scene_type,) if scene_type is not SceneType.UNKNOWN else (),
        scene_confidence=scene_confidence,
    )


def test_close_source_scores_higher_than_dark_noisy_source() -> None:
    style = StyleProfile(
        id="bright",
        name="Bright",
        target_luminance_median=70,
        target_mean_chroma=20,
    )
    engine = WeightedSuitabilityEngine()

    close = engine.evaluate(make_metrics(luminance=65, chroma=21, noise=0.1), style)
    far = engine.evaluate(make_metrics(luminance=15, chroma=5, noise=0.9), style)

    assert close.score > far.score
    assert close.recommended_strength > far.recommended_strength


def test_profile_threshold_controls_eligibility() -> None:
    style = StyleProfile(
        id="strict",
        name="Strict",
        target_luminance_median=90,
        target_mean_chroma=40,
        minimum_suitability_score=95,
    )

    result = WeightedSuitabilityEngine().evaluate(
        make_metrics(luminance=20, chroma=2, noise=0.8),
        style,
    )

    assert result.eligible is False
    assert result.recommended_strength == 0.0


def test_rich_profile_penalizes_source_far_from_lab_color_center() -> None:
    style = StyleProfile(
        id="cool",
        name="Cool",
        target_luminance_median=50,
        target_mean_chroma=20,
        target_mean_lab_a=-8,
        target_mean_lab_b=2,
    )
    engine = WeightedSuitabilityEngine()

    close = engine.evaluate(
        make_metrics(luminance=50, chroma=20, noise=0, lab_a=-8, lab_b=2),
        style,
    )
    far = engine.evaluate(
        make_metrics(luminance=50, chroma=20, noise=0, lab_a=20, lab_b=25),
        style,
    )

    assert close.score > far.score
    assert "Lab color center" in far.reasons[-1]


def test_semantic_gate_rejects_global_match_from_wrong_scene() -> None:
    style = StyleProfile(
        id="portrait",
        name="Portrait",
        target_luminance_median=50,
        target_mean_chroma=20,
        preferred_scene_types=(SceneType.PORTRAIT,),
    )

    result = WeightedSuitabilityEngine().evaluate(
        make_metrics(
            luminance=50,
            chroma=20,
            noise=0,
            scene_type=SceneType.LANDSCAPE,
            scene_confidence=0.98,
        ),
        style,
    )

    assert result.eligible is False
    assert result.recommended_strength == 0
    assert result.scene_compatibility is SceneCompatibility.MISMATCH
    assert "does not match" in result.reasons[-1]


def test_semantic_gate_fails_closed_when_scene_is_unknown() -> None:
    style = StyleProfile(
        id="portrait",
        name="Portrait",
        target_luminance_median=50,
        target_mean_chroma=20,
        preferred_scene_types=(SceneType.PORTRAIT,),
    )

    result = WeightedSuitabilityEngine().evaluate(
        make_metrics(luminance=50, chroma=20, noise=0),
        style,
    )

    assert result.eligible is False
    assert result.scene_compatibility is SceneCompatibility.UNKNOWN
    assert "failed closed" in result.reasons[-1]
