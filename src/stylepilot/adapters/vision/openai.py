from __future__ import annotations

import base64
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field

from stylepilot.application.ports import SceneAnalysisError
from stylepilot.domain.models import SceneAnalysis, SceneType


class VisionScenePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_scene: SceneType
    scene_tags: tuple[SceneType, ...] = Field(min_length=1, max_length=5)
    confidence: float = Field(ge=0.0, le=1.0)
    has_person: bool
    has_face: bool
    dominant_subjects: tuple[str, ...] = Field(default=(), max_length=5)
    evidence: tuple[str, ...] = Field(default=(), max_length=3)


@dataclass(frozen=True, slots=True)
class OpenAISceneAnalyzer:
    """Classify a stripped preview through an OpenAI-compatible structured API."""

    api_key: str
    system_prompt: str = field(repr=False)
    user_prompt: str = field(repr=False)
    prompt_version: str
    model: str = "gpt-5.6-luna"
    base_url: str | None = None
    api_mode: Literal["responses", "chat_completions"] = "responses"
    provider_name: str = "openai"
    max_dimension: int = 1024
    client: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.api_key:
            msg = "An API key is required for OpenAI scene analysis."
            raise ValueError(msg)
        if not self.system_prompt or not self.user_prompt or not self.prompt_version:
            msg = "Scene prompt text and version must not be empty"
            raise ValueError(msg)
        if self.max_dimension < 1:
            msg = "max_dimension must be positive"
            raise ValueError(msg)
        if not self.provider_name:
            msg = "provider_name must not be empty"
            raise ValueError(msg)
        if self.client is None:
            client = (
                OpenAI(api_key=self.api_key)
                if self.base_url is None
                else OpenAI(api_key=self.api_key, base_url=self.base_url)
            )
            object.__setattr__(self, "client", client)

    def analyze(self, image_path: Path) -> SceneAnalysis:
        image_url = self._data_url(image_path)
        try:
            payload = (
                self._analyze_responses(image_url)
                if self.api_mode == "responses"
                else self._analyze_chat_completions(image_url)
            )
        except Exception as error:
            msg = "The vision provider could not classify the image."
            raise SceneAnalysisError(msg) from error

        if payload is None:
            msg = "The vision provider returned no structured scene analysis."
            raise SceneAnalysisError(msg)
        tags = tuple(dict.fromkeys((payload.primary_scene, *payload.scene_tags)))
        return SceneAnalysis(
            primary_scene=payload.primary_scene,
            scene_tags=tags,
            confidence=payload.confidence,
            has_person=payload.has_person,
            has_face=payload.has_face,
            dominant_subjects=payload.dominant_subjects,
            evidence=payload.evidence,
            provider=self.provider_name,
            model=self.model,
            prompt_version=self.prompt_version,
        )

    def _analyze_responses(self, image_url: str) -> VisionScenePayload | None:
        response = self.client.responses.parse(
            model=self.model,
            input=[
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": self.user_prompt,
                        },
                        {
                            "type": "input_image",
                            "image_url": image_url,
                            "detail": "low",
                        },
                    ],
                },
            ],
            text_format=VisionScenePayload,
        )
        return response.output_parsed

    def _analyze_chat_completions(self, image_url: str) -> VisionScenePayload | None:
        completion = self.client.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": self.user_prompt,
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url},
                        },
                    ],
                },
            ],
            response_format=VisionScenePayload,
        )
        if not completion.choices:
            return None
        return completion.choices[0].message.parsed

    def _data_url(self, image_path: Path) -> str:
        resolved = image_path.expanduser().resolve()
        if not resolved.is_file():
            msg = f"Scene analysis image does not exist: {resolved}"
            raise SceneAnalysisError(msg)
        try:
            with Image.open(resolved) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail(
                    (self.max_dimension, self.max_dimension),
                    Image.Resampling.LANCZOS,
                )
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=85, optimize=True)
        except (OSError, ValueError) as error:
            msg = "The scene analysis image could not be decoded."
            raise SceneAnalysisError(msg) from error
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
