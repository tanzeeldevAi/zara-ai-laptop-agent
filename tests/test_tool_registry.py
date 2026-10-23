"""(a) The tool registry: the original 38 tools + Phase 3 additions.

Phase 3 (Tier-1 capabilities) added 11: persistent memory
(remember_fact / recall_facts / forget_fact), the Playwright browser
(browser_open / browser_snapshot / browser_click / browser_type /
browser_close) and the scheduler (schedule_task / list_schedules /
cancel_schedule) — 49 in total.
"""

from agent.tools import load_all

EXPECTED_COUNT = 49


def test_exact_tool_count():
    schemas, handlers = load_all()
    assert len(schemas) == EXPECTED_COUNT
    assert len(handlers) == EXPECTED_COUNT


def test_tool_names_unique_and_matched():
    schemas, handlers = load_all()
    names = [s["name"] for s in schemas]
    assert len(set(names)) == EXPECTED_COUNT
    assert set(names) == set(handlers.keys())


def test_schemas_well_formed():
    schemas, _ = load_all()
    for schema in schemas:
        assert schema["name"]
        assert schema["description"]
        assert schema["input_schema"]["type"] == "object"
        assert "properties" in schema["input_schema"]


def test_key_tools_present():
    _, handlers = load_all()
    for name in ("screenshot", "run_powershell", "speak", "system_status", "whatsapp_send"):
        assert name in handlers


def test_phase3_tools_present():
    _, handlers = load_all()
    for name in (
        "remember_fact", "recall_facts", "forget_fact",
        "browser_open", "browser_snapshot", "browser_click", "browser_type",
        "browser_close",
        "schedule_task", "list_schedules", "cancel_schedule",
    ):
        assert name in handlers
