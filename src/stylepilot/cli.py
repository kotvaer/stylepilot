from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from PIL import Image

from stylepilot.adapters.lightroom import (
    InMemoryLightroomBridge,
    LightroomBridgeError,
    McpLightroomBridge,
    McpServerConfig,
    StdioMcpToolClient,
)
from stylepilot.adapters.vision import OpenAISceneAnalyzer
from stylepilot.application.calibration import (
    CalibrationRecoveryError,
    LightroomActuatorCalibrator,
)
from stylepilot.application.ports import PhotoAnalyzer, SceneAnalyzer
from stylepilot.application.probing import LightroomParameterProbe, ParameterProbeRecoveryError
from stylepilot.application.workflow import StylePilotWorkflow, WorkflowDependencies
from stylepilot.configuration import StylePilotSettings, load_scene_prompt, load_settings
from stylepilot.domain.models import (
    DEVELOP_PARAMETER_RANGES,
    ActuatorCalibrationManifest,
    DevelopSettings,
    ParameterProbeRequest,
    PhotoMetadata,
    PhotoRef,
    SceneType,
    StyleProfile,
    StyleProfileBuildResult,
    WorkflowRequest,
    WorkflowResult,
)
from stylepilot.services import (
    ConstantSceneAnalyzer,
    ImageStatisticsAnalyzer,
    MultiReferenceStyleProfiler,
    RenderedResultVerifier,
    RuleBasedEditPlanner,
    SceneAwareImageAnalyzer,
    WeightedSuitabilityEngine,
)

_REFERENCE_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"})
_SCENE_CHOICES = tuple(scene.value for scene in SceneType if scene is not SceneType.UNKNOWN)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stylepilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="Run the workflow against a local rendered image")
    demo.add_argument("image", type=Path)
    demo.add_argument("--apply", action="store_true", help="Exercise the virtual-copy branch")
    demo.add_argument("--iso", type=int, default=100)

    style = subparsers.add_parser("style", help="Build and inspect multi-image style profiles")
    style_commands = style.add_subparsers(dest="style_command", required=True)
    style_build = style_commands.add_parser(
        "build",
        help="Build a robust style profile from 5 to 20 rendered reference images",
    )
    style_build.add_argument("references", nargs="+", type=Path)
    style_build.add_argument("--name", required=True)
    style_build.add_argument("--id", dest="style_id")
    style_build.add_argument("--output", type=Path)
    style_build.add_argument(
        "--preferred-scene",
        action="append",
        choices=_SCENE_CHOICES,
        default=[],
        help="Set an explicit compatible scene; may be repeated",
    )
    _add_semantic_provider_arguments(style_build)

    lightroom = subparsers.add_parser("lightroom", help="Connect to Lightroom Classic over MCP")
    lightroom_commands = lightroom.add_subparsers(dest="lightroom_command", required=True)
    lightroom_commands.add_parser("doctor", help="Check MCP and Lightroom plugin connectivity")
    inspect = lightroom_commands.add_parser(
        "inspect",
        help="Analyze the primary selected Lightroom photo without modifying it",
    )
    inspect.add_argument("--profile", type=Path)
    inspect.add_argument("--style-name")
    inspect.add_argument("--target-luminance", type=float)
    inspect.add_argument("--target-chroma", type=float)
    inspect.add_argument("--minimum-score", type=float)
    inspect.add_argument("--max-shadow-clip-ratio", type=float)
    inspect.add_argument("--max-highlight-clip-ratio", type=float)
    inspect.add_argument("--preview-root", type=Path, default=Path(".stylepilot/previews"))
    inspect.add_argument("--scene-override", choices=_SCENE_CHOICES)
    _add_semantic_provider_arguments(inspect)
    inspect.add_argument(
        "--apply-to-virtual-copy",
        action="store_true",
        help="Create a guarded virtual copy and apply the plan; never writes the source photo",
    )
    rollback = lightroom_commands.add_parser(
        "rollback",
        help="Restore a StylePilot recovery snapshot on a Lightroom virtual copy",
    )
    rollback.add_argument("--photo-id", required=True)
    rollback.add_argument("--snapshot-name", required=True)
    probe = lightroom_commands.add_parser(
        "probe",
        help="Measure one guarded Develop parameter on a temporary virtual copy",
    )
    probe.add_argument(
        "--parameter",
        required=True,
        choices=tuple(DEVELOP_PARAMETER_RANGES),
    )
    probe.add_argument("--value", required=True, type=float)
    probe.add_argument("--preview-root", type=Path, default=Path(".stylepilot/previews"))
    probe.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".stylepilot/evaluations/probes"),
        help="Directory for versioned JSON probe reports",
    )
    calibrate = lightroom_commands.add_parser(
        "calibrate",
        help="Run an approved multi-photo Lightroom actuator calibration manifest",
    )
    calibrate.add_argument("--manifest", required=True, type=Path)
    calibrate.add_argument("--preview-root", type=Path, default=Path(".stylepilot/previews"))
    calibrate.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".stylepilot/evaluations/calibrations"),
        help="Directory for versioned JSON calibration reports",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "demo":
            result = run_demo(args.image, apply=args.apply, iso=args.iso)
            payload = result.model_dump(mode="json")
        elif args.command == "style":
            result = run_style_build(
                name=args.name,
                style_id=args.style_id,
                references=args.references,
                scene_analyzer=_scene_analyzer_from_args(args),
                preferred_scene_types=tuple(SceneType(value) for value in args.preferred_scene),
            )
            payload = result.model_dump(mode="json")
            if args.output is not None:
                output_path = args.output.expanduser().resolve()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                payload["output_path"] = str(output_path)
        else:
            payload = asyncio.run(run_lightroom_command(args))
    except (FileNotFoundError, ValueError) as error:
        raise SystemExit(str(error)) from error
    except LightroomBridgeError as error:
        raise SystemExit(f"Lightroom integration error: {error}") from error
    except ParameterProbeRecoveryError as error:
        raise SystemExit(f"Lightroom probe recovery error: {error}") from error
    except CalibrationRecoveryError as error:
        raise SystemExit(f"Lightroom calibration recovery error: {error}") from error
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def run_style_build(
    *,
    name: str,
    style_id: str | None,
    references: Sequence[Path],
    scene_analyzer: SceneAnalyzer | None = None,
    preferred_scene_types: tuple[SceneType, ...] = (),
) -> StyleProfileBuildResult:
    reference_paths = _expand_reference_paths(references)
    resolved_id = style_id or _style_id_from_name(name)
    return MultiReferenceStyleProfiler(scene_analyzer=scene_analyzer).build(
        profile_id=resolved_id,
        name=name,
        reference_paths=reference_paths,
        preferred_scene_types=preferred_scene_types,
    )


def _expand_reference_paths(references: Sequence[Path]) -> tuple[Path, ...]:
    expanded: list[Path] = []
    for reference in references:
        resolved = reference.expanduser().resolve()
        if resolved.is_dir():
            expanded.extend(
                path
                for path in sorted(resolved.iterdir())
                if path.is_file() and path.suffix.lower() in _REFERENCE_IMAGE_SUFFIXES
            )
        else:
            expanded.append(resolved)
    if not expanded:
        msg = "No supported reference images were found."
        raise ValueError(msg)
    return tuple(expanded)


def _style_id_from_name(name: str) -> str:
    style_id = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    if not style_id:
        msg = "Provide --id when the style name has no ASCII letters or numbers."
        raise ValueError(msg)
    return style_id


async def run_lightroom_command(args: argparse.Namespace) -> dict[str, object]:
    config = McpServerConfig()
    async with StdioMcpToolClient(config) as client:
        bridge = McpLightroomBridge(
            client=client, preview_root=getattr(args, "preview_root", Path())
        )
        if args.lightroom_command == "doctor":
            return asdict(await bridge.diagnose())
        if args.lightroom_command == "rollback":
            await bridge.restore_recovery_snapshot(args.photo_id, args.snapshot_name)
            return {
                "status": "restored",
                "photo_id": args.photo_id,
                "snapshot_name": args.snapshot_name,
            }
        if args.lightroom_command == "probe":
            report = await LightroomParameterProbe(
                bridge=bridge,
                analyzer=ImageStatisticsAnalyzer(),
            ).run(
                ParameterProbeRequest(
                    parameter=args.parameter,
                    target_value=args.value,
                )
            )
            payload = report.model_dump(mode="json")
            output_dir = args.output_dir.expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{report.run_id}.json"
            output_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            payload["output_path"] = str(output_path)
            return payload
        if args.lightroom_command == "calibrate":
            manifest_path = args.manifest.expanduser().resolve()
            if not manifest_path.is_file():
                msg = f"Calibration manifest does not exist: {manifest_path}"
                raise FileNotFoundError(msg)
            manifest = ActuatorCalibrationManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            report = await LightroomActuatorCalibrator(
                bridge=bridge,
                analyzer=ImageStatisticsAnalyzer(),
            ).run(manifest)
            payload = report.model_dump(mode="json")
            output_dir = args.output_dir.expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{report.run_id}.json"
            output_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            payload["output_path"] = str(output_path)
            return payload

        workflow = StylePilotWorkflow(
            WorkflowDependencies(
                bridge=bridge,
                analyzer=_photo_analyzer_from_args(args),
                suitability=WeightedSuitabilityEngine(),
                planner=RuleBasedEditPlanner(),
                verifier=RenderedResultVerifier(),
            )
        )
        style_profile = _resolve_inspect_style_profile(args)
        result = await workflow.arun(
            WorkflowRequest(
                style_profile=style_profile,
                apply=args.apply_to_virtual_copy,
                write_approved=False,
            )
        )
        return result.model_dump(mode="json")


def _resolve_inspect_style_profile(args: argparse.Namespace) -> StyleProfile:
    if args.profile is None:
        return StyleProfile(
            id="cli-style",
            name=args.style_name or "Bright Clean",
            target_luminance_median=(
                65.0 if args.target_luminance is None else args.target_luminance
            ),
            target_mean_chroma=22.0 if args.target_chroma is None else args.target_chroma,
            minimum_suitability_score=(45.0 if args.minimum_score is None else args.minimum_score),
            max_shadow_clip_ratio=(
                0.01 if args.max_shadow_clip_ratio is None else args.max_shadow_clip_ratio
            ),
            max_highlight_clip_ratio=(
                0.01 if args.max_highlight_clip_ratio is None else args.max_highlight_clip_ratio
            ),
        )

    profile_path = args.profile.expanduser().resolve()
    if not profile_path.is_file():
        msg = f"Style profile does not exist: {profile_path}"
        raise FileNotFoundError(msg)
    raw_payload = json.loads(profile_path.read_text(encoding="utf-8"))
    profile_payload = raw_payload.get("profile", raw_payload)
    profile = StyleProfile.model_validate(profile_payload)

    updates: dict[str, object] = {}
    for field_name, value in (
        ("name", args.style_name),
        ("target_luminance_median", args.target_luminance),
        ("target_mean_chroma", args.target_chroma),
        ("minimum_suitability_score", args.minimum_score),
        ("max_shadow_clip_ratio", args.max_shadow_clip_ratio),
        ("max_highlight_clip_ratio", args.max_highlight_clip_ratio),
    ):
        if value is not None:
            updates[field_name] = value
    return StyleProfile.model_validate({**profile.model_dump(), **updates})


def _add_semantic_provider_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--semantic-provider",
        choices=("none", "openai", "qianwen"),
        help="Override STYLEPILOT_SEMANTIC_PROVIDER from .env (default: none)",
    )
    parser.add_argument("--semantic-model")
    parser.add_argument("--semantic-base-url")
    parser.add_argument(
        "--semantic-api-mode",
        choices=("responses", "chat_completions"),
        help="Override STYLEPILOT_VLM_API_MODE from .env",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Local dotenv configuration file (default: .env)",
    )


def _scene_analyzer_from_args(
    args: argparse.Namespace,
    *,
    settings: StylePilotSettings | None = None,
) -> SceneAnalyzer | None:
    scene_override = getattr(args, "scene_override", None)
    if scene_override is not None:
        return ConstantSceneAnalyzer(SceneType(scene_override))

    runtime = settings or load_settings(args.env_file)
    provider = args.semantic_provider or runtime.semantic_provider
    if provider == "none":
        return None

    api_key = runtime.api_key_value
    if not api_key:
        msg = (
            "Semantic analysis requires STYLEPILOT_VLM_API_KEY or OPENAI_API_KEY; "
            "add it to .env or the process environment. Keys are never accepted "
            "as CLI arguments."
        )
        raise ValueError(msg)
    model = args.semantic_model or runtime.vlm_model
    base_url = args.semantic_base_url or runtime.vlm_base_url
    api_mode = args.semantic_api_mode or runtime.vlm_api_mode
    prompt = load_scene_prompt(runtime.vlm_prompt_file)
    return OpenAISceneAnalyzer(
        api_key=api_key,
        system_prompt=prompt.system_prompt,
        user_prompt=prompt.user_prompt,
        prompt_version=prompt.prompt_version,
        model=model,
        base_url=base_url,
        api_mode=api_mode,
        provider_name=provider,
    )


def _photo_analyzer_from_args(args: argparse.Namespace) -> PhotoAnalyzer:
    numeric = ImageStatisticsAnalyzer()
    scene = _scene_analyzer_from_args(args)
    if scene is None:
        return numeric
    return SceneAwareImageAnalyzer(numeric_analyzer=numeric, scene_analyzer=scene)


def run_demo(image_path: Path, *, apply: bool, iso: int) -> WorkflowResult:
    resolved = image_path.expanduser().resolve()
    if not resolved.is_file():
        msg = f"Image does not exist: {resolved}"
        raise FileNotFoundError(msg)

    with Image.open(resolved) as image:
        width, height = image.size

    photo = PhotoRef(id="demo-photo", path=resolved, filename=resolved.name)
    metadata = PhotoMetadata(
        photo_id=photo.id,
        iso=iso,
        width=width,
        height=height,
        develop_settings=DevelopSettings(),
    )
    bridge = InMemoryLightroomBridge(
        selected_photos=(photo,),
        metadata_by_id={photo.id: metadata},
        preview_by_id={photo.id: resolved},
    )
    workflow = StylePilotWorkflow(
        WorkflowDependencies(
            bridge=bridge,
            analyzer=ImageStatisticsAnalyzer(),
            suitability=WeightedSuitabilityEngine(),
            planner=RuleBasedEditPlanner(),
            verifier=RenderedResultVerifier(),
        )
    )
    return workflow.run(
        WorkflowRequest(
            style_profile=StyleProfile(
                id="bright-clean",
                name="Bright Clean",
                target_luminance_median=65.0,
                target_mean_chroma=22.0,
            ),
            apply=apply,
            write_approved=apply,
        )
    )
