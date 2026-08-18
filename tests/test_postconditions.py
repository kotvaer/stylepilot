from __future__ import annotations

from stylepilot.domain.models import PhotoMetrics, StyleProfile
from stylepilot.services import RenderedResultVerifier


def metrics(
    *,
    luminance: float,
    chroma: float,
    shadow_clip: float = 0.0,
    highlight_clip: float = 0.0,
    contrast: float = 0.0,
    lab_a: float = 0.0,
    lab_b: float = 0.0,
) -> PhotoMetrics:
    return PhotoMetrics(
        luminance_median=luminance,
        mean_chroma=chroma,
        shadow_clip_ratio=shadow_clip,
        highlight_clip_ratio=highlight_clip,
        noise_risk=0.1,
        luminance_contrast=contrast,
        mean_lab_a=lab_a,
        mean_lab_b=lab_b,
    )


def style() -> StyleProfile:
    return StyleProfile(
        id="bright-clean",
        name="Bright Clean",
        target_luminance_median=65,
        target_mean_chroma=22,
        max_shadow_clip_ratio=0.01,
        max_highlight_clip_ratio=0.01,
    )


def test_rendered_result_passes_when_style_distance_improves() -> None:
    report = RenderedResultVerifier().verify(
        metrics(luminance=38, chroma=12),
        metrics(luminance=61, chroma=20),
        style(),
    )

    assert report.passed is True
    assert report.after_style_distance < report.before_style_distance
    assert report.improvement_ratio > 0.5
    assert report.issues == ()
    assert report.warnings == ()


def test_rendered_result_rolls_back_clipping_even_if_style_distance_improves() -> None:
    report = RenderedResultVerifier().verify(
        metrics(luminance=38, chroma=12),
        metrics(luminance=64, chroma=21, highlight_clip=0.02),
        style(),
    )

    assert report.passed is False
    assert report.issues == (
        "Rendered highlight clipping exceeds the style safety threshold (0.0200 > 0.0100).",
    )


def test_rendered_result_rolls_back_material_style_regression() -> None:
    report = RenderedResultVerifier().verify(
        metrics(luminance=60, chroma=20),
        metrics(luminance=20, chroma=5),
        style(),
    )

    assert report.passed is False
    assert "moved farther" in report.issues[0]
    assert report.improvement_ratio < 0


def test_rendered_result_warns_on_safe_but_unmeasurable_change() -> None:
    unchanged = metrics(luminance=50, chroma=16)

    report = RenderedResultVerifier().verify(unchanged, unchanged, style())

    assert report.passed is True
    assert report.improvement_ratio == 0
    assert report.warnings == (
        "The rendered result produced no meaningful measurable style improvement.",
    )


def test_rendered_result_rejects_shadow_clipping() -> None:
    report = RenderedResultVerifier().verify(
        metrics(luminance=38, chroma=12),
        metrics(luminance=60, chroma=20, shadow_clip=0.02),
        style(),
    )

    assert report.passed is False
    assert "shadow clipping" in report.issues[0]


def test_rendered_result_handles_regression_from_exact_target() -> None:
    report = RenderedResultVerifier().verify(
        metrics(luminance=65, chroma=22),
        metrics(luminance=60, chroma=20),
        style(),
    )

    assert report.passed is False
    assert report.before_style_distance == 0
    assert report.improvement_ratio < 0


def test_rich_profile_distance_tracks_lab_color_improvement() -> None:
    rich_style = StyleProfile(
        id="rich",
        name="Rich",
        target_luminance_median=50,
        target_mean_chroma=20,
        target_luminance_contrast=40,
        target_mean_lab_a=-5,
        target_mean_lab_b=4,
    )

    report = RenderedResultVerifier().verify(
        metrics(luminance=50, chroma=20, contrast=40, lab_a=20, lab_b=20),
        metrics(luminance=50, chroma=20, contrast=40, lab_a=-4, lab_b=5),
        rich_style,
    )

    assert report.passed is True
    assert report.after_style_distance < report.before_style_distance
