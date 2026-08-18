from pathlib import Path

from PIL import Image

from stylepilot.domain.models import PhotoMetadata
from stylepilot.services.image_analysis import ImageStatisticsAnalyzer


def save_solid_image(path: Path, value: int) -> None:
    Image.new("RGB", (20, 20), color=(value, value, value)).save(path)


def test_analyzer_reports_dark_and_bright_images_differently(tmp_path: Path) -> None:
    dark = tmp_path / "dark.png"
    bright = tmp_path / "bright.png"
    save_solid_image(dark, 16)
    save_solid_image(bright, 220)
    analyzer = ImageStatisticsAnalyzer()
    metadata = PhotoMetadata(photo_id="photo", iso=100)

    dark_metrics = analyzer.analyze(dark, metadata)
    bright_metrics = analyzer.analyze(bright, metadata)

    assert dark_metrics.luminance_median < bright_metrics.luminance_median
    assert dark_metrics.mean_chroma < 0.1
    assert bright_metrics.mean_chroma < 0.1


def test_high_iso_and_deep_shadows_increase_noise_risk(tmp_path: Path) -> None:
    image = tmp_path / "dark.png"
    save_solid_image(image, 0)
    analyzer = ImageStatisticsAnalyzer()

    low_iso = analyzer.analyze(image, PhotoMetadata(photo_id="low", iso=100))
    high_iso = analyzer.analyze(image, PhotoMetadata(photo_id="high", iso=6400))

    assert high_iso.noise_risk > low_iso.noise_risk


def test_analyzer_reports_lab_axes_and_luminance_contrast(tmp_path: Path) -> None:
    image = tmp_path / "red-and-blue.png"
    pixels = Image.new("RGB", (20, 20), (220, 30, 30))
    pixels.paste((20, 40, 220), (10, 0, 20, 20))
    pixels.save(image)

    metrics = ImageStatisticsAnalyzer().analyze(
        image,
        PhotoMetadata(photo_id="color", iso=100),
    )

    assert metrics.luminance_contrast > 0
    assert metrics.mean_chroma > 50
    assert metrics.mean_lab_a > 20
    assert metrics.mean_lab_b < 20
