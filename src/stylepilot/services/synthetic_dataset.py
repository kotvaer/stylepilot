from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageDraw

from stylepilot.domain.evaluation import (
    EvaluationAssetKind,
    EvaluationDatasetManifest,
    EvaluationDatasetPhoto,
)
from stylepilot.domain.models import SceneType

SYNTHETIC_DATASET_GENERATOR_VERSION = "synthetic-actuator-v1"
_WIDTH = 1024
_HEIGHT = 680
_SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


@dataclass(frozen=True, slots=True)
class _SyntheticPhotoSpec:
    id: str
    filename: str
    capture_series_id: str
    condition_tags: tuple[str, ...]
    render: Callable[[], Image.Image]


@dataclass(frozen=True, slots=True)
class SyntheticEvaluationDatasetBuilder:
    """Create deterministic, owned TIFF targets for Lightroom actuator experiments."""

    def build(self, output_dir: Path) -> EvaluationDatasetManifest:
        root = output_dir.expanduser().resolve()
        if root.exists() and not root.is_dir():
            msg = f"Synthetic dataset output is not a directory: {root}"
            raise ValueError(msg)
        image_dir = root / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        photos = []
        for spec in _photo_specs():
            payload = _encode_tiff(spec.render())
            relative_path = Path("images") / spec.filename
            _write_reproducible(root / relative_path, payload)
            photos.append(
                EvaluationDatasetPhoto(
                    id=spec.id,
                    relative_path=relative_path,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    kind=EvaluationAssetKind.SYNTHETIC,
                    scene_type=SceneType.ABSTRACT,
                    capture_series_id=spec.capture_series_id,
                    condition_tags=spec.condition_tags,
                    width=_WIDTH,
                    height=_HEIGHT,
                )
            )

        manifest = EvaluationDatasetManifest(
            id="synthetic-actuator-v1",
            name="StylePilot synthetic actuator dataset v1",
            description=(
                "Deterministic sRGB TIFF targets spanning tone, chroma, clipping, and "
                "high-frequency detail for Lightroom actuator and renderer calibration."
            ),
            generator_version=SYNTHETIC_DATASET_GENERATOR_VERSION,
            photos=tuple(photos),
        )
        manifest_payload = (
            json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
        ).encode()
        _write_reproducible(root / "manifest.json", manifest_payload)
        return manifest


def _photo_specs() -> tuple[_SyntheticPhotoSpec, ...]:
    return (
        _SyntheticPhotoSpec(
            id="neutral-tone-ramp",
            filename="neutral-tone-ramp.tiff",
            capture_series_id="synthetic-tone",
            condition_tags=("neutral", "full-tone-range", "clipping-sentinels"),
            render=_neutral_tone_ramp,
        ),
        _SyntheticPhotoSpec(
            id="low-key-tone",
            filename="low-key-tone.tiff",
            capture_series_id="synthetic-tone",
            condition_tags=("low-key", "shadow-detail", "dark"),
            render=_low_key,
        ),
        _SyntheticPhotoSpec(
            id="high-key-tone",
            filename="high-key-tone.tiff",
            capture_series_id="synthetic-tone",
            condition_tags=("high-key", "highlight-detail", "bright"),
            render=_high_key,
        ),
        _SyntheticPhotoSpec(
            id="color-patches",
            filename="color-patches.tiff",
            capture_series_id="synthetic-color",
            condition_tags=("color", "chroma", "hue-sweep"),
            render=_color_patches,
        ),
        _SyntheticPhotoSpec(
            id="fine-detail",
            filename="fine-detail.tiff",
            capture_series_id="synthetic-detail",
            condition_tags=("texture", "local-contrast", "high-frequency"),
            render=_fine_detail,
        ),
    )


def _neutral_tone_ramp() -> Image.Image:
    x = np.linspace(0.0, 1.0, _WIDTH, dtype=np.float64)
    ramp = np.broadcast_to(np.rint(x * 255.0).astype(np.uint8), (_HEIGHT, _WIDTH))
    rgb = np.repeat(ramp[:, :, None], 3, axis=2)
    image = Image.fromarray(rgb)
    draw = ImageDraw.Draw(image)
    patch_width = _WIDTH // 10
    for index in range(10):
        value = round(index * 255 / 9)
        left = index * patch_width
        right = _WIDTH if index == 9 else (index + 1) * patch_width
        draw.rectangle((left, _HEIGHT - 110, right, _HEIGHT), fill=(value,) * 3)
    return image


def _low_key() -> Image.Image:
    y, x = np.mgrid[0:_HEIGHT, 0:_WIDTH]
    normalized_x = x / (_WIDTH - 1)
    normalized_y = y / (_HEIGHT - 1)
    radial = np.exp(-(((normalized_x - 0.68) / 0.22) ** 2 + ((normalized_y - 0.45) / 0.3) ** 2))
    texture = 5.0 * np.sin(normalized_x * 24.0 * np.pi) * np.sin(normalized_y * 14.0 * np.pi)
    lightness = np.clip(4.0 + 72.0 * normalized_x**2.4 + 38.0 * radial + texture, 0, 118)
    rgb = np.stack((lightness * 0.92, lightness * 0.98, lightness * 1.08), axis=2)
    return Image.fromarray(np.rint(np.clip(rgb, 0, 255)).astype(np.uint8))


def _high_key() -> Image.Image:
    y, x = np.mgrid[0:_HEIGHT, 0:_WIDTH]
    normalized_x = x / (_WIDTH - 1)
    normalized_y = y / (_HEIGHT - 1)
    radial = np.exp(-(((normalized_x - 0.3) / 0.24) ** 2 + ((normalized_y - 0.55) / 0.32) ** 2))
    texture = 3.0 * np.sin(normalized_x * 18.0 * np.pi) * np.cos(normalized_y * 12.0 * np.pi)
    lightness = np.clip(154.0 + 100.0 * normalized_x**0.45 - 32.0 * radial + texture, 135, 255)
    rgb = np.stack((lightness * 1.02, lightness, lightness * 0.95), axis=2)
    return Image.fromarray(np.rint(np.clip(rgb, 0, 255)).astype(np.uint8))


def _color_patches() -> Image.Image:
    hue = np.broadcast_to(
        np.linspace(0, 255, _WIDTH, endpoint=False, dtype=np.uint8),
        (_HEIGHT, _WIDTH),
    )
    saturation = np.broadcast_to(
        np.linspace(36, 255, _HEIGHT, dtype=np.uint8)[:, None],
        (_HEIGHT, _WIDTH),
    )
    value = np.full((_HEIGHT, _WIDTH), 210, dtype=np.uint8)
    hsv = np.stack((hue, saturation, value), axis=2)
    image = Image.fromarray(hsv, mode="HSV").convert("RGB")
    draw = ImageDraw.Draw(image)
    colors = (
        (128, 128, 128),
        (230, 40, 40),
        (40, 210, 70),
        (35, 90, 230),
        (230, 205, 40),
        (210, 45, 205),
        (35, 205, 215),
        (245, 180, 130),
    )
    patch_width = _WIDTH // len(colors)
    for index, color in enumerate(colors):
        draw.rectangle(
            (index * patch_width, _HEIGHT - 120, (index + 1) * patch_width, _HEIGHT),
            fill=color,
        )
    return image


def _fine_detail() -> Image.Image:
    y, x = np.mgrid[0:_HEIGHT, 0:_WIDTH]
    checker_small = ((x // 4 + y // 4) % 2) * 82
    checker_large = ((x // 32 + y // 32) % 2) * 34
    waves = 30.0 * np.sin(x * 0.37) * np.cos(y * 0.29)
    base = np.clip(68.0 + checker_small + checker_large + waves, 0, 255)
    rgb = np.stack(
        (
            base,
            np.clip(base * 0.9 + 18.0 * np.sin(x * 0.07), 0, 255),
            np.clip(base * 0.82 + 22.0 * np.cos(y * 0.09), 0, 255),
        ),
        axis=2,
    )
    return Image.fromarray(np.rint(rgb).astype(np.uint8))


def _encode_tiff(image: Image.Image) -> bytes:
    buffer = BytesIO()
    # Pillow/libtiff may leave a compression padding byte uninitialized. Raw
    # strips are larger but byte-for-byte deterministic, which makes SHA-256 a
    # meaningful dataset identity across repeated generation runs.
    image.save(
        buffer,
        format="TIFF",
        compression="raw",
        icc_profile=_SRGB_PROFILE,
    )
    return buffer.getvalue()


def _write_reproducible(path: Path, payload: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            msg = f"Refusing to replace a non-matching synthetic dataset asset: {path}"
            raise ValueError(msg)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
