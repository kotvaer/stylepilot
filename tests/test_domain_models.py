import pytest
from pydantic import ValidationError

from stylepilot.domain.models import DevelopSettings, StyleFeatureSpread


def test_develop_settings_reject_unknown_parameter() -> None:
    with pytest.raises(ValidationError, match="Unsupported Lightroom Develop parameter"):
        DevelopSettings(values={"ImaginarySlider": 10.0})


def test_develop_settings_reject_out_of_range_value() -> None:
    with pytest.raises(ValidationError, match="Exposure2012 must be between"):
        DevelopSettings(values={"Exposure2012": 6.0})


def test_develop_settings_accept_safe_values() -> None:
    settings = DevelopSettings(values={"Exposure2012": 0.5, "Vibrance": -12})

    assert settings.get("Exposure2012") == 0.5
    assert settings.get("Contrast2012") == 0.0


@pytest.mark.parametrize("value", ["0.5", True, float("inf"), float("nan")])
def test_develop_settings_reject_non_finite_or_coerced_values(value: object) -> None:
    with pytest.raises(ValidationError, match="must be a finite number"):
        DevelopSettings(values={"Exposure2012": value})  # type: ignore[dict-item]


def test_style_feature_spread_rejects_negative_dispersion() -> None:
    with pytest.raises(ValidationError):
        StyleFeatureSpread(
            luminance_median=-1,
            luminance_contrast=1,
            mean_chroma=1,
            mean_lab_a=1,
            mean_lab_b=1,
            shadow_clip_ratio=1,
            highlight_clip_ratio=1,
        )
