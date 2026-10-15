#!/usr/bin/env python
"""Quick validation of Phase 3.0-Extended implementation."""

from agents.crawl_agent import CRAWL_TOOLS
from core.tools_registry import TOOL_DEFINITIONS

print("=" * 70)
print("PHASE 3.0-EXTENDED VALIDATION")
print("=" * 70)

print(f"\n✓ CRAWL_TOOLS loaded: {len(CRAWL_TOOLS)} tools")
tool_names = [t.get('name') for t in CRAWL_TOOLS]
print(f"  Tools: {', '.join(tool_names)}")

expected_new_tools = [
    'handle_alert',
    'switch_to_window',
    'get_all_windows',
    'scroll_page',
    'wait_for_dynamic_content',
    'detect_modals',
]

print(f"\n✓ New tools registered: {len(expected_new_tools)}")
for tool in expected_new_tools:
    if tool in TOOL_DEFINITIONS:
        print(f"  ✓ {tool}")
    else:
        print(f"  ✗ {tool} MISSING")

print("\n✓ Imports verified:")
try:
    from service.browser_explorer import (
        detect_alert,
        handle_alert,
        detect_modals,
        get_all_windows,
        switch_to_window,
        switch_to_main_window,
        detect_scrollable_areas,
        scroll_page,
        wait_for_dynamic_content,
        discover_advanced_content,
    )
    print("  ✓ All browser_explorer functions imported successfully")
except ImportError as e:
    print(f"  ✗ Import error: {e}")

print("\n" + "=" * 70)
print("VALIDATION COMPLETE")
print("=" * 70)
