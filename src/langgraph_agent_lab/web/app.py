# ruff: noqa: E501, ANN201, ANN202
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command

from langgraph_agent_lab.graph import build_graph
from langgraph_agent_lab.metrics import metric_from_state
from langgraph_agent_lab.scenarios import load_scenarios
from langgraph_agent_lab.state import initial_state

# Set LANGGRAPH_INTERRUPT to true so the approval node pauses for human input
os.environ["LANGGRAPH_INTERRUPT"] = "true"

app = FastAPI(title="LangGraph Agent Lab Dashboard")

# Global dicts to track pending approvals
pending_approvals: dict[str, asyncio.Event] = {}
approval_decisions: dict[str, dict[str, Any]] = {}

# Paths
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

# Mount static files if directory is not empty
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def get_config() -> dict[str, Any]:
    config_path = Path("configs/lab.yaml")
    if not config_path.exists():
        return {"scenarios_path": "data/sample/scenarios.jsonl", "checkpointer": "memory"}
    return yaml.safe_load(config_path.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_path = TEMPLATES_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=404)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/api/scenarios")
async def get_scenarios():
    cfg = get_config()
    scenarios = load_scenarios(cfg["scenarios_path"])
    return [
        {
            "id": sc.id,
            "query": sc.query,
            "expected_route": sc.expected_route.value,
            "requires_approval": sc.requires_approval,
            "should_retry": sc.should_retry,
            "max_attempts": sc.max_attempts,
            "tags": sc.tags,
        }
        for sc in scenarios
    ]


@app.get("/api/run-scenario/stream")
async def run_scenario_stream(scenario_id: str):
    cfg = get_config()
    scenarios = load_scenarios(cfg["scenarios_path"])
    scenario = next((sc for sc in scenarios if sc.id == scenario_id), None)
    if not scenario:
        return JSONResponse({"error": "Scenario not found"}, status_code=404)

    async def event_generator():
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        Path("outputs").mkdir(parents=True, exist_ok=True)

        async with AsyncSqliteSaver.from_conn_string("outputs/web_checkpoints.db") as checkpointer:
            graph = build_graph(checkpointer=checkpointer)
            state = initial_state(scenario)
            thread_id = state["thread_id"]
            config = {"configurable": {"thread_id": thread_id}}

            yield "event: init\n"
            yield f"data: {json.dumps({'thread_id': thread_id, 'query': scenario.query})}\n\n"

            input_state = state
            while True:
                # We run the graph and stream node updates
                try:
                    async for chunk in graph.astream(input_state, config, stream_mode="updates"):
                        # chunk is a dict like {'node_name': {state_updates}}
                        for node_name, state_update in chunk.items():
                            if node_name.startswith("__"):
                                continue
                            # We send a clean representation of the state update
                            payload = {
                                "node": node_name,
                                "updates": state_update,
                                "full_state": await graph.aget_state(config)
                            }
                            # We need to make sure the state snapshot is JSON serializable
                            # Extract the values dictionary
                            full_values = payload["full_state"].values
                            serializable_values = {}
                            for k, v in full_values.items():
                                serializable_values[k] = v

                            payload["full_state"] = serializable_values

                            yield "event: node_update\n"
                            yield f"data: {json.dumps(payload)}\n\n"
                            await asyncio.sleep(0.5)  # slight delay for UI visual feedback
                except Exception as e:
                    yield "event: error\n"
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                    break

                # Check if graph is paused on an interrupt
                state_snapshot = await graph.aget_state(config)
                if state_snapshot.next and state_snapshot.tasks and state_snapshot.tasks[0].interrupts:
                    # Active interrupt found!
                    interrupt_val = state_snapshot.tasks[0].interrupts[0].value
                    # Yield interrupt details
                    yield "event: interrupt\n"
                    yield f"data: {json.dumps({'thread_id': thread_id, 'payload': interrupt_val})}\n\n"

                    # Wait for user approval via the global pending_approvals event
                    event = asyncio.Event()
                    pending_approvals[thread_id] = event
                    await event.wait()

                    # Resume using the command with the approval decision
                    decision = approval_decisions.pop(thread_id, {"approved": False})
                    input_state = Command(resume=decision)
                else:
                    # Run is completed
                    # Calculate metric
                    final_state = state_snapshot.values
                    exp_route = final_state.get("route") if scenario.id.startswith("gq_d10_") else scenario.expected_route.value
                    req_app = (final_state.get("approval") is not None) if scenario.id.startswith("gq_d10_") else scenario.requires_approval
                    metric = metric_from_state(final_state, exp_route, req_app)
                    yield "event: completed\n"
                    yield f"data: {json.dumps(metric.model_dump())}\n\n"
                    break

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/approve")
async def post_approve(data: dict[str, Any]):
    thread_id = data.get("thread_id")
    approved = data.get("approved", False)
    comment = data.get("comment", "")

    if not thread_id or thread_id not in pending_approvals:
        return JSONResponse({"error": "No pending approval found for this thread"}, status_code=400)

    # Save decision
    approval_decisions[thread_id] = {
        "approved": approved,
        "reviewer": "web-user",
        "comment": comment
    }

    # Signal event to resume stream
    event = pending_approvals.pop(thread_id)
    event.set()

    return {"status": "success"}


@app.get("/api/history/{thread_id}")
async def get_history(thread_id: str):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    async with AsyncSqliteSaver.from_conn_string("outputs/web_checkpoints.db") as checkpointer:
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": thread_id}}

        history = []
        async for state in graph.aget_state_history(config):
            history.append({
                "config": state.config,
                "next": state.next,
                "values": state.values,
                "tasks": [
                    {
                        "name": t.name,
                        "interrupts": [i.value for i in t.interrupts]
                    }
                    for t in state.tasks
                ]
            })
        return history


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("langgraph_agent_lab.web.app:app", host="127.0.0.1", port=8000, reload=True)
