# ruff: noqa: E501
"""Node functions for the LangGraph workflow.

Each function receives AgentState and returns a partial state update dict.
Do NOT mutate input state — return new values only.

LLM REQUIREMENT:
- classify_node MUST use a real LLM call (structured output for intent classification)
- answer_node MUST use a real LLM call (grounded response generation)
- evaluate_node SHOULD use LLM-as-judge (bonus points; heuristic acceptable for base score)
"""

from __future__ import annotations

import os
from typing import Literal

from langgraph.types import interrupt
from pydantic import BaseModel, Field

from .llm import get_llm
from .state import AgentState, make_event


# Define Pydantic model for structured classification
class Classification(BaseModel):
    route: Literal["simple", "tool", "missing_info", "risky", "error"] = Field(
        description="The routed intent classification: 'risky' for payments/refunds/deletions/email sending/cancellations; 'tool' for lookups/order status; 'missing_info' for vague/incomplete queries; 'error' for system errors/timeouts/failures; 'simple' for basic QA/greetings."
    )
    explanation: str = Field(description="Brief explanation of why this route was selected.")


# ─── EXAMPLE: working node (provided for reference) ──────────────────
def intake_node(state: AgentState) -> dict:
    """Normalize raw query. This node is provided as a working example."""
    query = state.get("query", "").strip()
    return {
        "query": query,
        "messages": [f"intake:{query[:40]}"],
        "events": [make_event("intake", "completed", "query normalized")],
    }


# ─── TODO(student): implement ALL nodes below ────────────────────────


def classify_node(state: AgentState) -> dict:
    """Classify the query into a route using an LLM.

    *** MUST use a real LLM call — keyword-only heuristics will lose points. ***

    Use .with_structured_output() or equivalent to get reliable enum classification.
    The LLM should classify into one of: simple, tool, missing_info, risky, error.
    """
    query = state.get("query", "")
    llm = get_llm()
    structured_llm = llm.with_structured_output(Classification)
    prompt = f"""You are a support-ticket classification assistant.
Your task is to classify the user query into the correct route.
Priority order (highest to lowest): risky > tool > missing_info > error > simple.

If a query fits multiple categories, choose the one with the HIGHEST priority.
Categories:
1. 'risky': Actions with side effects, such as refunds, account deletions, cancellations, sending emails, or payments.
2. 'tool': Information lookup/search, such as order lookup, checking status, looking up customer details.
3. 'missing_info': Vague, incomplete, or ambiguous query lacking context (e.g. "Can you fix it?", "help me please" without stating what to fix/help with).
4. 'error': System crashes, timeouts, HTTP errors, service down, exceptions.
5. 'simple': General questions, greetings, resetting passwords, or simple QA answered without tools.

Query: {query}
"""
    res = structured_llm.invoke(prompt)
    route = res.route
    risk_level = "high" if route == "risky" else "low"
    return {
        "route": route,
        "risk_level": risk_level,
        "events": [make_event("classify", "completed", f"Route: {route}, Risk: {risk_level}")],
    }


def tool_node(state: AgentState) -> dict:
    """Execute a mock tool call.

    Simulate transient failures for error-route scenarios to test retry loops.

    Requirements:
    - Read current attempt count from state
    - If route is "error" and attempt < 2: return error result (string containing "ERROR")
    - Otherwise: return a mock success result string
    - Append result to tool_results list
    """
    attempt = state.get("attempt", 0)
    route = state.get("route", "")
    
    if route == "error" and attempt < 2:
        result = "ERROR: Timeout failure while processing tool request"
    else:
        result = "Mock Tool Result: Operation succeeded. Detailed info: status=completed, order_id=12345."
        
    return {
        "tool_results": [result],
        "events": [make_event("tool", "completed", f"Executed tool: {result[:50]}...")],
    }


def evaluate_node(state: AgentState) -> dict:
    """Evaluate tool results — the retry-loop gate.

    Check whether the latest tool result is satisfactory or needs retry.

    SHOULD use LLM-as-judge for bonus points. Heuristic (e.g., check for "ERROR" substring)
    is acceptable for base score.
    """
    results = state.get("tool_results", [])
    latest_result = results[-1] if results else ""
    
    # We implement heuristic first as a quick fallback and check
    if "ERROR" in latest_result:
        evaluation_result = "needs_retry"
    else:
        # LLM-as-judge bonus
        try:
            llm = get_llm()
            class JudgeResult(BaseModel):
                satisfactory: bool = Field(description="True if the tool result is successful and satisfactory, False if it is a failure or error")
            
            structured_judge = llm.with_structured_output(JudgeResult)
            prompt = f"""Evaluate this tool result for a customer support ticket.
Is the execution successful and satisfactory, or does it represent an error or failure that needs retry?
Tool Result: {latest_result}

Respond with JSON format matching the schema."""
            judge = structured_judge.invoke(prompt)
            evaluation_result = "success" if judge.satisfactory else "needs_retry"
        except Exception:
            evaluation_result = "success"
            
    return {
        "evaluation_result": evaluation_result,
        "events": [make_event("evaluate", "completed", f"Result: {evaluation_result}")],
    }


def answer_node(state: AgentState) -> dict:
    """Generate a final response using an LLM.

    *** MUST use a real LLM call — hardcoded strings will lose points. ***

    The LLM should generate a helpful response grounded in available context:
    - tool_results (if any)
    - approval decision (if risky route)
    - original query
    """
    query = state.get("query", "")
    tool_results = state.get("tool_results", [])
    approval = state.get("approval")
    
    prompt = f"""You are a helpful customer support agent.
Answer the user's query grounded in the provided context (tool results and approval details, if any).
Do not hallucinate or make up details.

Query: {query}
Tool Results: {tool_results}
Approval Details: {approval}

Helpful Response:"""
    
    llm = get_llm()
    response = llm.invoke(prompt)
    answer = response.content
    
    return {
        "final_answer": answer,
        "events": [make_event("answer", "completed", "response generated")],
    }


def ask_clarification_node(state: AgentState) -> dict:
    """Ask for missing information instead of hallucinating.

    Generate a specific clarification question based on the vague/incomplete query.
    """
    query = state.get("query", "")
    prompt = f"""The user query is too vague, ambiguous, or incomplete for us to resolve.
Please ask the user a polite, specific clarification question to get the missing information.

Query: {query}

Clarification Question:"""
    
    llm = get_llm()
    response = llm.invoke(prompt)
    question = response.content
    
    return {
        "pending_question": question,
        "final_answer": question,
        "events": [make_event("clarify", "completed", f"Clarification: {question[:40]}...")],
    }


def risky_action_node(state: AgentState) -> dict:
    """Prepare a risky action for human approval.

    Describe the proposed action and why it requires approval.
    """
    query = state.get("query", "")
    action = f"Proposed action for query '{query}': Perform requested operation with side effects (requires human verification)."
    return {
        "proposed_action": action,
        "events": [make_event("risky_action", "completed", "prepared action description")],
    }


def approval_node(state: AgentState) -> dict:
    """Human-in-the-loop approval step.

    Default behavior: mock approval (approved=True) so tests and CI run offline.
    Extension: if env LANGGRAPH_INTERRUPT=true, use langgraph.types.interrupt() for real HITL.
    """
    if os.getenv("LANGGRAPH_INTERRUPT") == "true":
        decision = interrupt({
            "proposed_action": state.get("proposed_action"),
            "query": state.get("query")
        })
        if isinstance(decision, dict):
            approved = decision.get("approved", False)
            reviewer = decision.get("reviewer", "human-reviewer")
            comment = decision.get("comment", "")
        else:
            approved = getattr(decision, "approved", False)
            reviewer = getattr(decision, "reviewer", "human-reviewer")
            comment = getattr(decision, "comment", "")
    else:
        approved = True
        reviewer = "mock-reviewer"
        comment = "auto-approved"
        
    return {
        "approval": {
            "approved": approved,
            "reviewer": reviewer,
            "comment": comment
        },
        "events": [make_event("approval", "completed", f"Decision: {approved}")],
    }


def retry_or_fallback_node(state: AgentState) -> dict:
    """Record a retry attempt.

    Increment the attempt counter and log the transient failure.
    """
    attempt = state.get("attempt", 0) + 1
    error_msg = f"Attempt {attempt} failed due to system/tool error"
    return {
        "attempt": attempt,
        "errors": [error_msg],
        "events": [make_event("retry", "completed", f"Attempt {attempt}")],
    }


def dead_letter_node(state: AgentState) -> dict:
    """Handle unresolvable failures after max retries exceeded.

    This is the third layer: retry → fallback → dead letter.
    Log the failure and set a final_answer explaining that the request could not be completed.
    """
    return {
        "final_answer": "We apologize, but we were unable to process your request after multiple attempts due to persistent errors.",
        "events": [make_event("dead_letter", "completed", "escalated to dead letter queue")],
    }


def finalize_node(state: AgentState) -> dict:
    """Emit a final audit event. All routes must pass through here before END.
    """
    return {
        "events": [make_event("finalize", "completed", "workflow finished")],
    }
