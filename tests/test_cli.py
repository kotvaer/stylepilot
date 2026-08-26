from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from stylepilot.adapters.vision import OpenAISceneAnalyzer
from stylepilot.cli import (
    _resolve_inspect_style_profile,
    _scene_analyzer_from_args,
    build_parser,
    main,
    run_demo,
)
from stylepilot.configuration import StylePilotSettings
from stylepilot.domain.models import (
    ActuatorCalibrationManifest,
    ActuatorCalibrationReport,
    CalibrationRunStatus,
    WorkflowStatus,
)


@pytest.fixture
def demo_image(tmp_path: Path) -> Path:
    image_path = tmp_path / "demo.jpg"
    Image.new("RGB", (32, 24), (140, 130, 120)).save(image_path)
    return image_path


def test_run_demo_exercises_virtual_copy_branch(demo_image: Path) -> None:
    result = run_demo(demo_image, apply=True, iso=200)

    assert result.status is WorkflowStatus.APPLIED
    assert result.applied_photo is not None
    assert ":virtual:" in result.applied_photo.id
    assert result.plan is not None


def test_run_demo_rejects_missing_image(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Image does not exist"):
        run_demo(tmp_path / "missing.jpg", apply=False, iso=100)


def test_main_prints_machine_readable_result(
    demo_image: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["demo", str(demo_image)])

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == WorkflowStatus.PLANNED
    assert payload["photo"]["filename"] == demo_image.name


def test_rollback_cli_requires_explicit_recovery_token() -> None:
    args = build_parser().parse_args(
        [
            "lightroom",
            "rollback",
            "--photo-id",
            "virtual-1",
            "--snapshot-name",
            "StylePilot Before token",
        ]
    )

    assert args.photo_id == "virtual-1"
    assert args.snapshot_name == "StylePilot Before token"


def test_probe_cli_requires_one_guarded_parameter_point() -> None:
    args = build_parser().parse_args(
        [
            "lightroom",
            "probe",
            "--parameter",
            "Contrast2012",
            "--value",
            "20",
        ]
    )

    assert args.parameter == "Contrast2012"
    assert args.value == 20
    assert args.output_dir == Path(".stylepilot/evaluations/probes")


def test_calibrate_cli_accepts_manifest_and_report_directory() -> None:
    args = build_parser().parse_args(
        [
            "lightroom",
            "calibrate",
            "--manifest",
            "examples/actuator-calibration.json",
        ]
    )

    assert args.manifest == Path("examples/actuator-calibration.json")
    assert args.output_dir == Path(".stylepilot/evaluations/calibrations")


def test_evaluation_cli_exposes_dataset_and_aggregate_defaults() -> None:
    dataset_args = build_parser().parse_args(["evaluation", "create-synthetic-dataset"])
    aggregate_args = build_parser().parse_args(["evaluation", "aggregate", "one.json", "reports"])

    assert dataset_args.output_dir == Path(".stylepilot/evaluations/datasets/synthetic-actuator-v1")
    assert aggregate_args.reports == [Path("one.json"), Path("reports")]
    assert aggregate_args.output_dir == Path(".stylepilot/evaluations/aggregates")


def test_evaluation_cli_generates_dataset_and_json_markdown_aggregate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset_dir = tmp_path / "dataset"
    main(
        [
            "evaluation",
            "create-synthetic-dataset",
            "--output-dir",
            str(dataset_dir),
        ]
    )
    dataset_payload = json.loads(capsys.readouterr().out)
    assert len(dataset_payload["photos"]) == 5
    assert Path(dataset_payload["manifest_path"]).is_file()

    now = datetime(2026, 8, 19, tzinfo=UTC)
    report = ActuatorCalibrationReport(
        run_id="rejected-run",
        created_at=now,
        completed_at=now,
        status=CalibrationRunStatus.REJECTED,
        manifest=ActuatorCalibrationManifest.model_validate(
            {
                "id": "one-point",
                "name": "One point",
                "parameters": [{"parameter": "Contrast2012", "values": [0]}],
            }
        ),
        selected_photos=(),
        planned_sample_count=0,
    )
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    report_path = reports_dir / "report.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    output_dir = tmp_path / "aggregates"

    main(
        [
            "evaluation",
            "aggregate",
            str(reports_dir),
            "--dataset",
            dataset_payload["manifest_path"],
            "--output-dir",
            str(output_dir),
        ]
    )

    aggregate_payload = json.loads(capsys.readouterr().out)
    assert aggregate_payload["source_run_ids"] == ["rejected-run"]
    assert aggregate_payload["dataset_coverage"]["verified_asset_count"] == 5
    assert Path(aggregate_payload["output_path"]).is_file()
    assert Path(aggregate_payload["markdown_output_path"]).is_file()


def test_inspect_cli_exposes_postcondition_safety_thresholds() -> None:
    args = build_parser().parse_args(
        [
            "lightroom",
            "inspect",
            "--max-shadow-clip-ratio",
            "0.005",
            "--max-highlight-clip-ratio",
            "0",
        ]
    )

    assert args.max_shadow_clip_ratio == 0.005
    assert args.max_highlight_clip_ratio == 0


def test_style_build_cli_expands_directory_and_writes_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    references = tmp_path / "references"
    references.mkdir()
    for index, value in enumerate((100, 105, 110, 115, 120), start=1):
        Image.new("RGB", (24, 24), (value, value, value)).save(references / f"{index:02}.jpg")
    (references / "notes.txt").write_text("ignored", encoding="utf-8")
    output = tmp_path / "profiles" / "test-style.json"

    main(
        [
            "style",
            "build",
            "--name",
            "Test Style",
            "--output",
            str(output),
            "--preferred-scene",
            "portrait",
            "--semantic-provider",
            "none",
            str(references),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert payload["profile"]["id"] == "test-style"
    assert payload["profile"]["reference_count"] == 5
    assert payload["profile"]["preferred_scene_types"] == ["portrait"]
    assert payload["output_path"] == str(output.resolve())
    assert saved["profile"]["algorithm_version"] == "global-lab-v1"


def test_inspect_cli_loads_built_profile_and_allows_safety_override(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "profile": {
                    "id": "loaded-style",
                    "name": "Loaded Style",
                    "target_luminance_median": 72,
                    "target_mean_chroma": 12,
                    "target_mean_lab_a": -4,
                    "target_mean_lab_b": 5,
                }
            }
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "lightroom",
            "inspect",
            "--profile",
            str(profile_path),
            "--minimum-score",
            "60",
        ]
    )

    profile = _resolve_inspect_style_profile(args)

    assert profile.id == "loaded-style"
    assert profile.target_mean_lab_a == -4
    assert profile.minimum_suitability_score == 60


def test_semantic_provider_requires_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STYLEPILOT_VLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = build_parser().parse_args(["lightroom", "inspect", "--semantic-provider", "openai"])

    settings = StylePilotSettings(_env_file=None)

    with pytest.raises(ValueError, match="Keys are never accepted as CLI arguments"):
        _scene_analyzer_from_args(args, settings=settings)


def test_semantic_provider_and_model_load_from_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STYLEPILOT_SEMANTIC_PROVIDER", raising=False)
    monkeypatch.delenv("STYLEPILOT_VLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("STYLEPILOT_VLM_MODEL", raising=False)
    prompt_file = tmp_path / "prompt.json"
    prompt_file.write_text(
        '{"prompt_version":"test-v1","system_prompt":"test-system","user_prompt":"test-user"}',
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "STYLEPILOT_SEMANTIC_PROVIDER=qianwen\n"
        "STYLEPILOT_VLM_API_KEY=test-key\n"
        "STYLEPILOT_VLM_MODEL=test-vision-model\n"
        "STYLEPILOT_VLM_API_MODE=chat_completions\n"
        f"STYLEPILOT_VLM_PROMPT_FILE={prompt_file}\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args(["lightroom", "inspect", "--env-file", str(env_file)])

    analyzer = _scene_analyzer_from_args(args)

    assert isinstance(analyzer, OpenAISceneAnalyzer)
    assert analyzer.model == "test-vision-model"
    assert analyzer.api_mode == "chat_completions"
    assert analyzer.provider_name == "qianwen"


def test_cli_model_override_wins_over_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STYLEPILOT_SEMANTIC_PROVIDER", raising=False)
    monkeypatch.delenv("STYLEPILOT_VLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("STYLEPILOT_VLM_MODEL", raising=False)
    prompt_file = tmp_path / "prompt.json"
    prompt_file.write_text(
        '{"prompt_version":"test-v1","system_prompt":"test-system","user_prompt":"test-user"}',
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "STYLEPILOT_SEMANTIC_PROVIDER=openai\n"
        "STYLEPILOT_VLM_API_KEY=test-key\n"
        "STYLEPILOT_VLM_MODEL=dotenv-model\n"
        f"STYLEPILOT_VLM_PROMPT_FILE={prompt_file}\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "lightroom",
            "inspect",
            "--env-file",
            str(env_file),
            "--semantic-model",
            "cli-model",
        ]
    )

    analyzer = _scene_analyzer_from_args(args)

    assert isinstance(analyzer, OpenAISceneAnalyzer)
    assert analyzer.model == "cli-model"
