from __future__ import annotations

from pathlib import Path

import pytest

from stylepilot.configuration import load_scene_prompt, load_settings


def test_process_environment_overrides_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "STYLEPILOT_VLM_API_KEY=dotenv-key\nSTYLEPILOT_VLM_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("STYLEPILOT_VLM_API_KEY", "environment-key")
    monkeypatch.setenv("STYLEPILOT_VLM_MODEL", "environment-model")

    settings = load_settings(env_file)

    assert settings.api_key_value == "environment-key"
    assert settings.vlm_model == "environment-model"


def test_openai_api_key_is_supported_as_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STYLEPILOT_VLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=openai-key\n", encoding="utf-8")

    settings = load_settings(env_file)

    assert settings.api_key_value == "openai-key"


def test_qianwen_api_mode_loads_from_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STYLEPILOT_SEMANTIC_PROVIDER", raising=False)
    monkeypatch.delenv("STYLEPILOT_VLM_API_MODE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "STYLEPILOT_SEMANTIC_PROVIDER=qianwen\nSTYLEPILOT_VLM_API_MODE=chat_completions\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.semantic_provider == "qianwen"
    assert settings.vlm_api_mode == "chat_completions"


def test_relative_private_prompt_path_resolves_from_dotenv_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STYLEPILOT_VLM_PROMPT_FILE", raising=False)
    env_file = tmp_path / "config" / ".env"
    env_file.parent.mkdir()
    env_file.write_text(
        "STYLEPILOT_VLM_PROMPT_FILE=.stylepilot/prompts/scene.json\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.vlm_prompt_file == (env_file.parent / ".stylepilot" / "prompts" / "scene.json")


def test_private_scene_prompt_loads_from_ignored_json(tmp_path: Path) -> None:
    prompt_file = tmp_path / "scene.json"
    prompt_file.write_text(
        '{"prompt_version":"test-v1","system_prompt":"private-system",'
        '"user_prompt":"private-user"}',
        encoding="utf-8",
    )

    prompt = load_scene_prompt(prompt_file)

    assert prompt.prompt_version == "test-v1"
    assert prompt.system_prompt == "private-system"


def test_private_scene_prompt_file_is_required(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="STYLEPILOT_VLM_PROMPT_FILE"):
        load_scene_prompt(tmp_path / "missing.json")
