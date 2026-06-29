# Day 08 Lab Report

## 1. Team / student

- Name: Hoàng Văn Anh
- Repo/commit: Day 08 LangGraph Lab Implementation
- Date: 2026-06-29

## 2. Architecture

The LangGraph support-ticket agent is built as a stateful `StateGraph` consisting of 11 nodes and 4 conditional routing layers.

### Nodes:

- `intake`: Normalizes query input.
- `classify`: LLM intent classifier with structured output.
- `tool`: Executes lookup tools and simulates transient failures.
- `evaluate`: LLM-as-judge or heuristic evaluation of tool execution.
- `answer`: LLM-grounded response generation.
- `clarify`: Generates specific clarification questions for vague queries.
- `risky_action`: Prepares sensitive actions for review.
- `approval`: Handles human-in-the-loop decisions (interrupts).
- `retry`: Increments retry attempt counters.
- `dead_letter`: Handles terminal execution failures.
- `finalize`: Normalizes final events.

### Routing & Edges:

- **`classify`** conditional edge: simple -> answer, tool -> tool, missing_info -> clarify, risky -> risky_action, error -> retry.
- **`evaluate`** conditional edge: success -> answer, needs_retry -> retry.
- **`retry`** conditional edge: attempt < max_attempts -> tool, attempt >= max_attempts -> dead_letter.
- **`approval`** conditional edge: approved -> tool, rejected -> clarify.

## 3. State schema

| Field             | Reducer   | Why                                            |
| ----------------- | --------- | ---------------------------------------------- |
| messages          | append    | audit conversation/events                      |
| tool_results      | append    | history of tool outputs                        |
| errors            | append    | history of transient or permanent errors       |
| events            | append    | trace/audit of executed graph nodes            |
| route             | overwrite | stores the current active route name           |
| risk_level        | overwrite | stores risk level evaluation ('high'/'low')    |
| attempt           | overwrite | track retry count                              |
| max_attempts      | overwrite | stores maximum allowed retries                 |
| final_answer      | overwrite | final customer-facing response                 |
| evaluation_result | overwrite | feedback loop state ('success'/'needs_retry')  |
| pending_question  | overwrite | clarification questions                        |
| proposed_action   | overwrite | details of the risky action requiring approval |
| approval          | overwrite | human-in-the-loop approval decision            |

## 4. Scenario results

### Key Metrics Summary:

- **Total Scenarios:** 7
- **Success Rate:** 100.00%
- **Average Nodes Visited:** 6.43
- **Total Retries:** 3
- **Total Interrupts:** 2

### Detailed Scenario Results:

| Scenario        | Expected route | Actual route | Success | Retries | Interrupts |
| --------------- | -------------- | ------------ | ------: | ------: | ---------: |
| S01_simple      | simple         | simple       |     Yes |       0 |          0 |
| S02_tool        | tool           | tool         |     Yes |       0 |          0 |
| S03_missing     | missing_info   | missing_info |     Yes |       0 |          0 |
| S04_risky       | risky          | risky        |     Yes |       0 |          1 |
| S05_error       | error          | error        |     Yes |       2 |          0 |
| S06_delete      | risky          | risky        |     Yes |       0 |          1 |
| S07_dead_letter | error          | error        |     Yes |       1 |          0 |

## 5. Failure analysis

1. **Retry or tool failure:** We tested transient tool failures using the `error` scenario. When the mock tool returned an error prefix, the evaluation node successfully routed to the retry node, which tracked attempt counts. Once the max retry limit was reached, the loop safely exited to the dead-letter node to avoid infinite loops.
2. **Risky action without approval:** We implemented human-in-the-loop gating for the `risky` route. If the system was configured with `LANGGRAPH_INTERRUPT=true`, it invoked LangGraph `interrupt()`, halting the execution until an operator approved or rejected the action. If approved, the agent proceeded to run the tool; if rejected, it requested clarification.

## 6. Persistence / recovery evidence

We implemented a checkpointer backend using `SqliteSaver` in `persistence.py` with `WAL` journaling mode enabled for durability and concurrency. A unique `thread_id` was configured for each invoke, allowing state history tracking and crash recovery.

## 7. Extension work

We completed the following extension work:

1. **SQLite Checkpointer Backend:** Implemented persistent storage in sqlite3, supporting WAL and thread isolation.
2. **LLM-as-Judge Evaluation:** Taught the evaluate node to run a structured Pydantic judge check, classifying tool output quality dynamically.
3. **Interactive HITL Interrupts:** Handled state pausing using `interrupt()` for human approval.

## 8. Improvement plan

If we had one more day, we would build a Streamlit UI for approval and real-time visualization of state history and checkpointer traces (e.g., using LangSmith or Graphviz diagram export).