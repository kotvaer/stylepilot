from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from stylepilot.adapters.vision.openai import OpenAISceneAnalyzer, VisionScenePayload
from stylepilot.application.ports import SceneAnalysisError
from stylepilot.domain.models import SceneType


class FakeResponses:
    def __init__(self, payload: VisionScenePayload | None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.request: dict[str, Any] | None = None

    def parse(self, **request: Any) -> SimpleNamespace:
        self.request = request
        if self.error is not None:
            raise self.error
        return SimpleNamespace(output_parsed=self.payload)


class FakeChatCompletions:
    def __init__(self, payload: VisionScenePayload | None) -> None:
        self.payload = payload
        self.request: dict[str, Any] | None = None

    def parse(self, **request: Any) -> SimpleNamespace:
        self.request = request
        message = SimpleNamespace(parsed=self.payload)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_openai_scene_analyzer_sends_stripped_downsized_data_url(tmp_path: Path) -> None:
    source = tmp_path / "portrait.png"
    Image.new("RGB", (200, 100), (180, 140, 120)).save(source)
    responses = FakeResponses(
        VisionScenePayload(
            primary_scene=SceneType.PORTRAIT,
            scene_tags=(SceneType.PORTRAIT, SceneType.PEOPLE),
            confidence=0.96,
            has_person=True,
            has_face=True,
            dominant_subjects=("one person",),
            evidence=("A face and upper body dominate the frame.",),
        )
    )
    client = SimpleNamespace(responses=responses)
    analyzer = OpenAISceneAnalyzer(
        api_key="test-key",
        system_prompt="test-system-placeholder",
        user_prompt="test-user-placeholder",
        prompt_version="test-v1",
        model="test-vision-model",
        max_dimension=64,
        client=client,
    )

    result = analyzer.analyze(source)

    assert result.primary_scene is SceneType.PORTRAIT
    assert result.provider == "openai"
    assert result.model == "test-vision-model"
    assert responses.request is not None
    assert responses.request["text_format"] is VisionScenePayload
    assert "test-system-placeholder" not in repr(analyzer)
    assert responses.request["input"][0]["content"] == "test-system-placeholder"
    user_content = responses.request["input"][1]["content"]
    assert user_content[0]["text"] == "test-user-placeholder"
    image_url = user_content[1]["image_url"]
    assert image_url.startswith("data:image/jpeg;base64,")
    encoded = image_url.removeprefix("data:image/jpeg;base64,")
    with Image.open(BytesIO(base64.b64decode(encoded))) as uploaded:
        assert max(uploaded.size) <= 64
        assert uploaded.getexif() == {}


def test_qianwen_scene_analyzer_uses_chat_completions_schema_parse(tmp_path: Path) -> None:
    source = tmp_path / "street.jpg"
    Image.new("RGB", (80, 60), (100, 120, 130)).save(source)
    completions = FakeChatCompletions(
        VisionScenePayload(
            primary_scene=SceneType.STREET,
            scene_tags=(SceneType.STREET, SceneType.CITYSCAPE),
            confidence=0.91,
            has_person=False,
            has_face=False,
            dominant_subjects=("road", "buildings"),
            evidence=("A road runs between urban buildings.",),
        )
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    analyzer = OpenAISceneAnalyzer(
        api_key="test-key",
        system_prompt="test-system-placeholder",
        user_prompt="test-user-placeholder",
        prompt_version="test-v1",
        model="qwen3.7-plus",
        api_mode="chat_completions",
        provider_name="qianwen",
        client=client,
    )

    result = analyzer.analyze(source)

    assert result.provider == "qianwen"
    assert result.primary_scene is SceneType.STREET
    assert completions.request is not None
    assert completions.request["response_format"] is VisionScenePayload
    user_content = completions.request["messages"][1]["content"]
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.parametrize(
    ("payload", "error"),
    [(None, None), (None, RuntimeError("provider unavailable"))],
)
def test_openai_scene_analyzer_normalizes_invalid_provider_results(
    tmp_path: Path,
    payload: VisionScenePayload | None,
    error: Exception | None,
) -> None:
    image = tmp_path / "image.jpg"
    Image.new("RGB", (20, 20), (128, 128, 128)).save(image)
    analyzer = OpenAISceneAnalyzer(
        api_key="test-key",
        system_prompt="test-system-placeholder",
        user_prompt="test-user-placeholder",
        prompt_version="test-v1",
        client=SimpleNamespace(responses=FakeResponses(payload, error)),
    )

    with pytest.raises(SceneAnalysisError):
        analyzer.analyze(image)
