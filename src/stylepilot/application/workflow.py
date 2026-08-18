from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, START, StateGraph

from stylepilot.application.ports import (
    EditPlanner,
    LightroomBridge,
    PhotoAnalyzer,
    PostconditionVerifier,
    SuitabilityEngine,
)
from stylepilot.application.state import WorkflowState, WorkflowUpdate
from stylepilot.domain.models import WorkflowRequest, WorkflowResult, WorkflowStatus


@dataclass(frozen=True, slots=True)
class WorkflowDependencies:
    bridge: LightroomBridge
    analyzer: PhotoAnalyzer
    suitability: SuitabilityEngine
    planner: EditPlanner
    verifier: PostconditionVerifier


class StylePilotWorkflow:
    """First executable StylePilot workflow slice.

    The graph intentionally mixes deterministic nodes with guarded Lightroom
    actions. VLM planning and render-result refinement will be added without
    changing the bridge or domain boundaries.
    """

    def __init__(self, dependencies: WorkflowDependencies) -> None:
        self._dependencies = dependencies
        self._graph: Any = self._build_graph()  # LangGraph's compiled type is intentionally opaque.

    def run(self, request: WorkflowRequest) -> WorkflowResult:
        """Run the workflow from synchronous callers such as the local demo."""

        return asyncio.run(self.arun(request))

    async def arun(self, request: WorkflowRequest) -> WorkflowResult:
        """Run the workflow without blocking an existing event loop."""

        state = WorkflowState.model_validate(await self._graph.ainvoke({"request": request}))
        if state.status is None:
            msg = "Workflow completed without a terminal status."
            raise RuntimeError(msg)
        return WorkflowResult(
            status=state.status,
            message=state.message,
            photo=state.photo,
            metrics=state.metrics,
            suitability=state.suitability,
            plan=state.plan,
            applied_photo=state.applied_photo,
            recovery_snapshot=state.recovery_snapshot,
            rendered_metrics=state.rendered_metrics,
            verification=state.verification,
        )

    def _build_graph(self) -> Any:
        graph = StateGraph(WorkflowState)
        graph.add_node("load_selection", self._load_selection)
        graph.add_node("load_source", self._load_source)
        graph.add_node("analyze", self._analyze)
        graph.add_node("evaluate_suitability", self._evaluate_suitability)
        graph.add_node("plan", self._plan)
        graph.add_node("request_approval", self._request_approval)
        graph.add_node("apply", self._apply)
        graph.add_node("verify_applied_result", self._verify_applied_result)

        graph.add_edge(START, "load_selection")
        graph.add_conditional_edges(
            "load_selection",
            self._route_after_selection,
            {"continue": "load_source", "stop": END},
        )
        graph.add_edge("load_source", "analyze")
        graph.add_edge("analyze", "evaluate_suitability")
        graph.add_conditional_edges(
            "evaluate_suitability",
            self._route_after_suitability,
            {"plan": "plan", "stop": END},
        )
        graph.add_conditional_edges(
            "plan",
            self._route_after_plan,
            {"apply": "apply", "approval": "request_approval", "stop": END},
        )
        graph.add_conditional_edges(
            "request_approval",
            self._route_after_approval,
            {"apply": "apply", "stop": END},
        )
        graph.add_edge("apply", "verify_applied_result")
        graph.add_edge("verify_applied_result", END)
        return graph.compile()

    async def _load_selection(self, state: WorkflowState) -> WorkflowUpdate:
        del state
        selected = await self._dependencies.bridge.get_selected_photos()
        if not selected:
            return {
                "status": WorkflowStatus.NO_SELECTION,
                "message": "No photo is selected in Lightroom Classic.",
            }
        photo = selected[0]
        return {"photo": photo, "message": f"Selected {photo.filename}."}

    def _route_after_selection(self, state: WorkflowState) -> str:
        return "continue" if state.photo is not None else "stop"

    async def _load_source(self, state: WorkflowState) -> WorkflowUpdate:
        photo = state.photo
        if photo is None:
            msg = "A photo is required before loading its source data."
            raise RuntimeError(msg)
        return {
            "metadata": await self._dependencies.bridge.get_photo_metadata(photo.id),
            "preview_path": await self._dependencies.bridge.render_analysis_preview(photo.id),
        }

    def _analyze(self, state: WorkflowState) -> WorkflowUpdate:
        if state.preview_path is None or state.metadata is None:
            msg = "Photo metadata and an analysis preview are required before analysis."
            raise RuntimeError(msg)
        metrics = self._dependencies.analyzer.analyze(
            state.preview_path,
            state.metadata,
        )
        return {"metrics": metrics}

    def _evaluate_suitability(self, state: WorkflowState) -> WorkflowUpdate:
        if state.metrics is None:
            msg = "Photo metrics are required before suitability evaluation."
            raise RuntimeError(msg)
        report = self._dependencies.suitability.evaluate(
            state.metrics,
            state.request.style_profile,
        )
        if report.eligible:
            return {"suitability": report, "message": "The source photo is suitable for planning."}
        return {
            "suitability": report,
            "status": WorkflowStatus.UNSUITABLE,
            "message": "The source photo is not suitable for this style at a safe strength.",
        }

    def _route_after_suitability(self, state: WorkflowState) -> str:
        return "plan" if state.suitability is not None and state.suitability.eligible else "stop"

    def _plan(self, state: WorkflowState) -> WorkflowUpdate:
        if state.metadata is None or state.metrics is None or state.suitability is None:
            msg = "Metadata, metrics, and suitability are required before edit planning."
            raise RuntimeError(msg)
        request = state.request
        strength = min(request.requested_strength, state.suitability.recommended_strength)
        plan = self._dependencies.planner.plan(
            state.metadata,
            state.metrics,
            request.style_profile,
            strength,
        )
        message = "A guarded Lightroom Develop plan is ready."
        if request.apply and not request.write_approved:
            message = "The plan is ready but requires explicit write approval."
        return {
            "plan": plan,
            "status": WorkflowStatus.PLANNED,
            "message": message,
        }

    def _route_after_plan(self, state: WorkflowState) -> str:
        request = state.request
        if not request.apply:
            return "stop"
        return "apply" if request.write_approved else "approval"

    async def _request_approval(self, state: WorkflowState) -> WorkflowUpdate:
        if state.photo is None or state.suitability is None or state.plan is None:
            msg = "Photo, suitability, and plan are required before requesting approval."
            raise RuntimeError(msg)
        try:
            approved = await self._dependencies.bridge.request_write_approval(
                state.photo,
                state.request.style_profile,
                state.suitability,
                state.plan,
            )
        except Exception as error:
            return {
                "status": WorkflowStatus.PLANNED,
                "message": f"Write approval was not granted: {error}",
            }
        if not approved:
            return {
                "status": WorkflowStatus.PLANNED,
                "message": "The photographer rejected the proposed virtual-copy edit.",
            }
        return {
            "write_approved": True,
            "message": "The photographer approved the request-bound edit.",
        }

    def _route_after_approval(self, state: WorkflowState) -> str:
        return "apply" if state.write_approved else "stop"

    async def _apply(self, state: WorkflowState) -> WorkflowUpdate:
        if state.plan is None or state.photo is None:
            msg = "An edit plan and source photo are required before applying settings."
            raise RuntimeError(msg)
        plan = state.plan
        virtual_copy = await self._dependencies.bridge.create_virtual_copy(
            state.photo.id,
            "StylePilot Preview",
        )
        recovery_snapshot = await self._dependencies.bridge.apply_develop_settings(
            virtual_copy.id,
            plan.settings,
            "StylePilot Preview",
        )
        return {
            "applied_photo": virtual_copy,
            "recovery_snapshot": recovery_snapshot,
            "message": "The plan was applied and is awaiting rendered-result verification.",
        }

    async def _verify_applied_result(self, state: WorkflowState) -> WorkflowUpdate:
        applied_photo = state.applied_photo
        recovery_snapshot = state.recovery_snapshot
        source_metrics = state.metrics
        if applied_photo is None or recovery_snapshot is None or source_metrics is None:
            msg = (
                "Applied photo, recovery snapshot, and source metrics are required "
                "for verification."
            )
            raise RuntimeError(msg)

        try:
            metadata = await self._dependencies.bridge.get_photo_metadata(applied_photo.id)
            preview = await self._dependencies.bridge.render_analysis_preview(applied_photo.id)
            rendered_metrics = self._dependencies.analyzer.analyze(preview, metadata)
        except Exception as error:
            await self._dependencies.bridge.restore_develop_snapshot(
                applied_photo.id,
                recovery_snapshot.name,
            )
            return {
                "status": WorkflowStatus.ROLLED_BACK,
                "message": (
                    "Rendered-result verification could not complete; the recovery "
                    f"snapshot was restored. Cause: {error}"
                ),
            }

        report = self._dependencies.verifier.verify(
            source_metrics,
            rendered_metrics,
            state.request.style_profile,
        )
        if report.passed:
            return {
                "rendered_metrics": rendered_metrics,
                "verification": report,
                "status": WorkflowStatus.APPLIED,
                "message": "The Lightroom-rendered virtual copy passed postcondition checks.",
            }

        await self._dependencies.bridge.restore_develop_snapshot(
            applied_photo.id,
            recovery_snapshot.name,
        )
        return {
            "rendered_metrics": rendered_metrics,
            "verification": report,
            "status": WorkflowStatus.ROLLED_BACK,
            "message": "The rendered result failed safety checks and was rolled back.",
        }
