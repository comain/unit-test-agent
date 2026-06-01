from langgraph.graph import StateGraph, END
from uta.graph.state import AgentState
from uta.graph.nodes import (
    scan_and_select, parse_context, select_next_class,
    generate_and_validate, baseline_compile,
    setup_branch, commit_to_branch, store_and_push,
)

def build_workflow():
    workflow = StateGraph(AgentState)

    workflow.add_node("setup_branch", setup_branch)
    workflow.add_node("baseline_compile", baseline_compile)
    workflow.add_node("scan_and_select", scan_and_select)
    workflow.add_node("parse_context", parse_context)
    workflow.add_node("select_next_class", select_next_class)
    workflow.add_node("generate_and_validate", generate_and_validate)
    workflow.add_node("commit_to_branch", commit_to_branch)
    workflow.add_node("store_and_push", store_and_push)

    workflow.set_entry_point("setup_branch")

    workflow.add_edge("setup_branch", "baseline_compile")

    workflow.add_conditional_edges(
        "baseline_compile",
        lambda state: "error" if state.get("error") else "continue",
        {
            "error": END,
            "continue": "scan_and_select"
        }
    )
    workflow.add_edge("scan_and_select", "parse_context")
    workflow.add_edge("parse_context", "select_next_class")

    workflow.add_conditional_edges(
        "select_next_class",
        lambda state: "end" if state.get("finished") else "generate",
        {
            "end": "store_and_push",
            "generate": "generate_and_validate"
        }
    )

    workflow.add_conditional_edges(
        "generate_and_validate",
        lambda state: "end" if state.get("stopped_early") else "continue",
        {
            "end": END,
            "continue": "commit_to_branch",
        }
    )
    workflow.add_edge("commit_to_branch", "select_next_class")
    workflow.add_edge("store_and_push", END)

    return workflow.compile()
