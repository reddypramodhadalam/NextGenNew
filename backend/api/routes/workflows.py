"""Optional wizard tracking; no changes to existing untracked API requests."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.routing import APIRoute

from service.workflow_control import (
    FlowCancelled, create_flow, find_flow, request_cancel, tracked_request,
)


def _flow(request, flow_id):
    try:
        return find_flow(flow_id, request.headers.get("X-Workflow-Key"))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class WorkflowRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            flow_id = request.headers.get("X-Workflow-Id")
            if not flow_id:
                return await original(request)
            flow = _flow(request, flow_id)
            try:
                return await tracked_request(flow, original, request)
            except FlowCancelled:
                raise HTTPException(status_code=409, detail="Workflow cancelled; no new work can start.")

        return handler


router = APIRouter()


@router.post("/test-suites/flows")
def create():
    try:
        return create_flow()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/test-suites/flows/{flow_id}")
def status(flow_id: str, request: Request):
    return _flow(request, flow_id).snapshot()


@router.post("/test-suites/flows/{flow_id}/cancel")
def cancel(flow_id: str, request: Request):
    return request_cancel(_flow(request, flow_id))