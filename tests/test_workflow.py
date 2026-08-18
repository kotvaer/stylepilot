from pathlib import Path

from PIL import Image

from stylepilot.adapters.lightroom import InMemoryLightroomBridge
from stylepilot.application.ports import PhotoAnalyzer, PostconditionVerifier
from stylepilot.application.workflow import StylePilotWorkflow, WorkflowDependencies
from stylepilot.domain.models import (
    DevelopSettings,
    PhotoMetadata,
    PhotoMetrics,
    PhotoRef,
    RenderVerificationReport,
    SceneType,
    StyleProfile,
    WorkflowRequest,
    WorkflowStatus,
)
from stylepilot.services import (
    ConstantSceneAnalyzer,
    ImageStatisticsAnalyzer,
    RenderedResultVerifier,
    RuleBasedEditPlanner,
    SceneAwareImageAnalyzer,
    WeightedSuitabilityEngine,
)


def build_workflow(
    tmp_path: Path,
    *,
    selected: bool = True,
    verifier: PostconditionVerifier | None = None,
    analyzer: PhotoAnalyzer | None = None,
) -> tuple[StylePilotWorkflow, InMemoryLightroomBridge]:
    preview = tmp_path / "preview.png"
    Image.new("RGB", (32, 32), color=(140, 130, 120)).save(preview)
    photo = PhotoRef(id="photo-1", path=preview, filename="preview.png")
    bridge = InMemoryLightroomBridge(
        selected_photos=(photo,) if selected else (),
        metadata_by_id={
            photo.id: PhotoMetadata(
                photo_id=photo.id,
                iso=200,
                develop_settings=DevelopSettings(values={"Exposure2012": 0.1}),
            )
        },
        preview_by_id={photo.id: preview},
    )
    workflow = StylePilotWorkflow(
        WorkflowDependencies(
            bridge=bridge,
            analyzer=analyzer or ImageStatisticsAnalyzer(),
            suitability=WeightedSuitabilityEngine(),
            planner=RuleBasedEditPlanner(),
            verifier=verifier or RenderedResultVerifier(),
        )
    )
    return workflow, bridge


def style(*, minimum_score: float = 30) -> StyleProfile:
    return StyleProfile(
        id="bright-clean",
        name="Bright Clean",
        target_luminance_median=65,
        target_mean_chroma=20,
        minimum_suitability_score=minimum_score,
    )


def test_workflow_stops_cleanly_without_selection(tmp_path: Path) -> None:
    workflow, _bridge = build_workflow(tmp_path, selected=False)

    result = workflow.run(WorkflowRequest(style_profile=style()))

    assert result.status is WorkflowStatus.NO_SELECTION
    assert result.photo is None


def test_workflow_can_plan_without_touching_lightroom(tmp_path: Path) -> None:
    workflow, bridge = build_workflow(tmp_path)

    result = workflow.run(WorkflowRequest(style_profile=style(), apply=False))

    assert result.status is WorkflowStatus.PLANNED
    assert result.plan is not None
    assert bridge.virtual_copies == []
    assert bridge.applied_settings == {}


def test_workflow_applies_only_to_virtual_copy(tmp_path: Path) -> None:
    workflow, bridge = build_workflow(tmp_path)

    result = workflow.run(WorkflowRequest(style_profile=style(), apply=True, write_approved=True))

    assert result.status is WorkflowStatus.APPLIED
    assert result.applied_photo is not None
    assert result.applied_photo.id != "photo-1"
    assert result.recovery_snapshot is not None
    assert result.recovery_snapshot.photo_id == result.applied_photo.id
    assert result.rendered_metrics is not None
    assert result.verification is not None
    assert result.verification.passed is True
    assert "photo-1" not in bridge.applied_settings
    assert result.applied_photo.id in bridge.applied_settings


def test_workflow_requires_explicit_write_approval(tmp_path: Path) -> None:
    workflow, bridge = build_workflow(tmp_path)
    bridge.approval_granted = False

    result = workflow.run(WorkflowRequest(style_profile=style(), apply=True))

    assert result.status is WorkflowStatus.PLANNED
    assert "photographer rejected" in result.message
    assert bridge.approval_requests == 1
    assert bridge.virtual_copies == []
    assert bridge.applied_settings == {}


def test_workflow_applies_after_request_bound_approval(tmp_path: Path) -> None:
    workflow, bridge = build_workflow(tmp_path)

    result = workflow.run(WorkflowRequest(style_profile=style(), apply=True))

    assert result.status is WorkflowStatus.APPLIED
    assert bridge.approval_requests == 1
    assert result.applied_photo is not None


def test_failed_rendered_postcondition_restores_snapshot(tmp_path: Path) -> None:
    class RejectingVerifier:
        def verify(
            self,
            before: PhotoMetrics,
            after: PhotoMetrics,
            style: StyleProfile,
        ) -> RenderVerificationReport:
            del before, after, style
            return RenderVerificationReport(
                passed=False,
                before_style_distance=0.2,
                after_style_distance=0.3,
                improvement_ratio=-0.5,
                issues=("Synthetic rendered safety failure.",),
                warnings=(),
            )

    workflow, bridge = build_workflow(tmp_path, verifier=RejectingVerifier())

    result = workflow.run(WorkflowRequest(style_profile=style(), apply=True, write_approved=True))

    assert result.status is WorkflowStatus.ROLLED_BACK
    assert result.applied_photo is not None
    assert result.recovery_snapshot is not None
    assert result.verification is not None
    assert result.verification.passed is False
    assert bridge.applied_settings == {}
    assert bridge.restored_snapshots == [result.recovery_snapshot.name]


def test_workflow_rejects_unsafe_style_before_planning(tmp_path: Path) -> None:
    workflow, bridge = build_workflow(tmp_path)

    result = workflow.run(
        WorkflowRequest(
            style_profile=style(minimum_score=100),
            apply=True,
            write_approved=True,
        )
    )

    assert result.status is WorkflowStatus.UNSUITABLE
    assert result.plan is None
    assert bridge.virtual_copies == []


def test_workflow_rejects_landscape_for_portrait_style_before_planning(
    tmp_path: Path,
) -> None:
    analyzer = SceneAwareImageAnalyzer(
        numeric_analyzer=ImageStatisticsAnalyzer(),
        scene_analyzer=ConstantSceneAnalyzer(SceneType.LANDSCAPE),
    )
    workflow, bridge = build_workflow(tmp_path, analyzer=analyzer)
    portrait_style = style().model_copy(update={"preferred_scene_types": (SceneType.PORTRAIT,)})

    result = workflow.run(
        WorkflowRequest(style_profile=portrait_style, apply=True, write_approved=True)
    )

    assert result.status is WorkflowStatus.UNSUITABLE
    assert result.plan is None
    assert result.suitability is not None
    assert result.suitability.scene_compatibility == "mismatch"
    assert bridge.virtual_copies == []
