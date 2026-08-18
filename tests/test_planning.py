from stylepilot.domain.models import DevelopSettings, PhotoMetadata, PhotoMetrics, StyleProfile
from stylepilot.services import RuleBasedEditPlanner


def test_planner_uses_only_controls_with_measured_targets() -> None:
    metadata = PhotoMetadata(
        photo_id="photo-1",
        develop_settings=DevelopSettings(
            values={
                "Contrast2012": 5,
                "Whites2012": 0,
                "Blacks2012": -5,
                "Texture": 20,
            }
        ),
    )
    source = PhotoMetrics(
        luminance_median=40,
        luminance_contrast=25,
        mean_chroma=12,
        shadow_clip_ratio=0.02,
        highlight_clip_ratio=0.03,
        noise_risk=0.1,
    )
    style = StyleProfile(
        id="measured-style",
        name="Measured Style",
        target_luminance_median=55,
        target_luminance_contrast=45,
        target_mean_chroma=18,
        target_shadow_clip_ratio=0.005,
        target_highlight_clip_ratio=0.01,
    )

    plan = RuleBasedEditPlanner().plan(metadata, source, style, strength=1.0)

    assert plan.settings.get("Contrast2012") == 30
    assert plan.settings.get("Whites2012") == -35
    assert plan.settings.get("Blacks2012") == 30
    assert "Texture" not in plan.settings.values
    assert "Clarity2012" not in plan.settings.values
    assert "Dehaze" not in plan.settings.values
    assert len(plan.rationale) == 6


def test_planner_does_not_emit_unneeded_extra_tone_controls() -> None:
    metadata = PhotoMetadata(photo_id="photo-1")
    source = PhotoMetrics(
        luminance_median=50,
        luminance_contrast=35,
        mean_chroma=20,
        shadow_clip_ratio=0,
        highlight_clip_ratio=0,
        noise_risk=0.1,
    )
    style = StyleProfile(
        id="simple-style",
        name="Simple Style",
        target_luminance_median=50,
        target_mean_chroma=20,
    )

    plan = RuleBasedEditPlanner().plan(metadata, source, style, strength=1.0)

    assert set(plan.settings.values) == {
        "Exposure2012",
        "Highlights2012",
        "Shadows2012",
        "Vibrance",
    }
