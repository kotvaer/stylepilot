from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from skimage.color import rgb2lab

from stylepilot.domain.models import PhotoMetadata, PhotoMetrics


@dataclass(frozen=True, slots=True)
class ImageStatisticsAnalyzer:
    """Small, deterministic baseline for Lightroom-rendered sRGB previews."""

    max_dimension: int = 1024

    def __post_init__(self) -> None:
        if self.max_dimension < 1:
            msg = "max_dimension must be positive"
            raise ValueError(msg)

    def analyze(self, preview_path: Path, metadata: PhotoMetadata) -> PhotoMetrics:
        with Image.open(preview_path) as image:
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            normalized.thumbnail(
                (self.max_dimension, self.max_dimension),
                Image.Resampling.LANCZOS,
            )
            rgb = np.asarray(normalized, dtype=np.float32) / 255.0

        lab = rgb2lab(rgb)
        lightness = lab[..., 0]
        lab_a = lab[..., 1]
        lab_b = lab[..., 2]
        chroma = np.hypot(lab_a, lab_b)
        shadow_share = float(np.mean(lightness <= 5.0))

        return PhotoMetrics(
            luminance_median=float(np.median(lightness)),
            luminance_contrast=float(np.percentile(lightness, 90) - np.percentile(lightness, 10)),
            mean_chroma=float(np.mean(chroma)),
            mean_lab_a=float(np.mean(lab_a)),
            mean_lab_b=float(np.mean(lab_b)),
            shadow_clip_ratio=float(np.mean(np.max(rgb, axis=-1) <= (1.0 / 255.0))),
            highlight_clip_ratio=float(np.mean(np.min(rgb, axis=-1) >= (254.0 / 255.0))),
            noise_risk=self._estimate_noise_risk(metadata.iso, shadow_share),
        )

    @staticmethod
    def _estimate_noise_risk(iso: int | None, shadow_share: float) -> float:
        iso_component = 0.0 if iso is None else min(1.0, max(0.0, (iso - 100) / 6300))
        return min(1.0, (0.65 * iso_component) + (0.35 * shadow_share))
