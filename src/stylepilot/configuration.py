from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScenePromptConfig(BaseModel):
    """Private prompt payload loaded from an ignored local JSON file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_version: str = Field(min_length=1, max_length=100)
    system_prompt: str = Field(min_length=1)
    user_prompt: str = Field(min_length=1)


class StylePilotSettings(BaseSettings):
    """Runtime configuration loaded from the environment and a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    semantic_provider: Literal["none", "openai", "qianwen"] = Field(
        default="none",
        validation_alias="STYLEPILOT_SEMANTIC_PROVIDER",
    )
    vlm_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("STYLEPILOT_VLM_API_KEY", "OPENAI_API_KEY"),
    )
    vlm_model: str = Field(
        default="gpt-5.6-luna",
        validation_alias="STYLEPILOT_VLM_MODEL",
    )
    vlm_base_url: str | None = Field(
        default=None,
        validation_alias="STYLEPILOT_VLM_BASE_URL",
    )
    vlm_api_mode: Literal["responses", "chat_completions"] = Field(
        default="responses",
        validation_alias="STYLEPILOT_VLM_API_MODE",
    )
    vlm_prompt_file: Path = Field(
        default=Path(".stylepilot/prompts/scene-analysis.json"),
        validation_alias="STYLEPILOT_VLM_PROMPT_FILE",
    )

    @property
    def api_key_value(self) -> str | None:
        if self.vlm_api_key is None:
            return None
        return self.vlm_api_key.get_secret_value() or None


def load_settings(env_file: Path = Path(".env")) -> StylePilotSettings:
    """Load a chosen dotenv file without overriding real process environment variables."""

    return StylePilotSettings(_env_file=env_file)


def load_scene_prompt(path: Path) -> ScenePromptConfig:
    """Load private prompt text without embedding it in code or repository history."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        msg = (
            f"Private scene prompt file does not exist: {resolved}. "
            "Set STYLEPILOT_VLM_PROMPT_FILE to an ignored local JSON file."
        )
        raise FileNotFoundError(msg)
    return ScenePromptConfig.model_validate_json(resolved.read_text(encoding="utf-8"))
