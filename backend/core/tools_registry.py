"""
core/tools_registry.py

One definition per tool, shared by every agent that needs it.

A tool definition is the structured description given to the LLM so it
knows how to call that tool. The actual implementation (Selenium calls,
etc.) lives in core/tools.py. These are kept separate so definitions
can be imported without importing Selenium.

Agents import the subset they need by name:
    from core.tools_registry import TOOL_DEFINITIONS
    explorer_tools = [TOOL_DEFINITIONS[t] for t in ["get_snapshot", "click", "type_text", "done"]]
"""

TOOL_DEFINITIONS = {
    "get_snapshot": {
        "name": "get_snapshot",
        "description": (
            "Read every interactive element currently visible on screen — "
            "buttons, text boxes, links, menus. Returns a list of elements, "
            "each with a ref (used to act on it), its role, and its visible name. "
            "Call this whenever you need to see what's on screen before acting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    "click": {
        "name": "click",
        "description": (
            "Click one element from the most recent get_snapshot result. "
            "Use the ref value exactly as returned — it is only valid until "
            "the next get_snapshot call. If the screen may have changed, "
            "call get_snapshot again before clicking."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The ref value from the most recent get_snapshot result.",
                }
            },
            "required": ["ref"],
        },
    },

    "type_text": {
        "name": "type_text",
        "description": (
            "Clear a text input and type the given text into it. "
            "Only use on elements whose role is 'input' or 'textarea'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The ref value from the most recent get_snapshot result.",
                },
                "text": {
                    "type": "string",
                    "description": "The exact text to type.",
                },
            },
            "required": ["ref", "text"],
        },
    },

    "done": {
        "name": "done",
        "description": (
            "Call this once you have confirmed or disproven the test case's "
            "expected outcome. Set passed=true if the application behaved "
            "correctly, false if it did not. Include a plain-English explanation "
            "in notes — this becomes the test case's result description."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "passed": {
                    "type": "boolean",
                    "description": "True if the expected outcome was confirmed, false if not.",
                },
                "notes": {
                    "type": "string",
                    "description": "What you found and why you reached this verdict.",
                },
            },
            "required": ["passed"],
        },
    },

    "propose_replacement": {
        "name": "propose_replacement",
        "description": (
            "Report the element that now serves the same purpose as the one "
            "that broke. Called by the Healer Agent — never by Explorer or Author."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "The element's role (e.g. 'button')."},
                "name": {"type": "string", "description": "The element's accessible name or label."},
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "How confident you are this is the correct replacement.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Why you believe this element replaces the broken one.",
                },
            },
            "required": ["role", "name", "confidence", "reasoning"],
        },
    },

    "propose_test_cases": {
        "name": "propose_test_cases",
        "description": (
            "Report the structured list of test cases identified from surveying the screen. "
            "Called by the Author Agent — never by Explorer or Healer. "
            "Each test case must include a title, steps, category, and priority. "
            "Steps must each include step text, expected result, element_hint, matched, "
            "data_input, and wait_condition."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "test_cases": {
                    "type": "array",
                    "description": "List of structured test cases.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": (
                                    "Short, specific title under 70 chars. "
                                    "No 'Verify that' prefix."
                                ),
                            },
                            "scenario": {
                                "type": "string",
                                "description": "2-4 word feature group name.",
                            },
                            "category": {
                                "type": "string",
                                "description": "One of: positive, negative, edge, boundary, security.",
                            },
                            "priority": {
                                "type": "string",
                                "description": "One of: P0, P1, P2, P3.",
                            },
                            "description": {
                                "type": "string",
                                "description": "One sentence describing what is being verified.",
                            },
                            "requirement_id": {
                                "type": "string",
                                "description": "Requirement ID from the source document, or null.",
                            },
                            "preconditions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "What must be true before step 1: auth state, "
                                    "starting screen, required data."
                                ),
                            },
                            "steps": {
                                "type": "array",
                                "description": "Ordered list of test steps.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "step": {
                                            "type": "string",
                                            "description": "Imperative tester action.",
                                        },
                                        "expected": {
                                            "type": "string",
                                            "description": "What must be true after this step.",
                                        },
                                        "element_hint": {
                                            "type": "string",
                                            "description": (
                                                "Exact label/id/aria-label of the element "
                                                "acted on, from the snapshot. null for "
                                                "navigate/wait steps."
                                            ),
                                        },
                                        "matched": {
                                            "type": "boolean",
                                            "description": (
                                                "true if element was seen in a snapshot, "
                                                "false if inferred."
                                            ),
                                        },
                                        "data_input": {
                                            "type": "string",
                                            "description": (
                                                "Business key for the value to type, e.g. "
                                                "'username', 'password'. null if step types nothing."
                                            ),
                                        },
                                        "wait_condition": {
                                            "type": "string",
                                            "description": (
                                                "What to wait for after this action: "
                                                "'url_change', 'element_visible', or 'none'."
                                            ),
                                        },
                                    },
                                    "required": ["step", "expected"],
                                },
                            },
                        },
                        "required": ["title", "steps"],
                    },
                }
            },
            "required": ["test_cases"],
        },
    },

    # ── Phase 1 tools: navigation, frames, dropdowns, waits, alerts ──────────

    "navigate_to": {
        "name": "navigate_to",
        "description": (
            "Navigate the browser to an absolute URL and wait for the page to finish "
            "loading. Call get_snapshot after navigating to see the new page's elements."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The absolute URL to navigate to (e.g. 'https://app.example.com/complaints').",
                }
            },
            "required": ["url"],
        },
    },

    "get_current_url": {
        "name": "get_current_url",
        "description": (
            "Return the browser's current URL. Use after a submit or navigation step "
            "to verify the page changed to the expected URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    "switch_to_frame": {
        "name": "switch_to_frame",
        "description": (
            "Switch into an iframe so that get_snapshot, click, and type_text operate "
            "inside it. Pass 'main' as frame_ref to return to the top-level document. "
            "Always call get_snapshot after switching frames."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "frame_ref": {
                    "type": "string",
                    "description": (
                        "The ref of an iframe element from the most recent get_snapshot, "
                        "or the string 'main' to switch back to the main document."
                    ),
                }
            },
            "required": ["frame_ref"],
        },
    },

    "select_option": {
        "name": "select_option",
        "description": (
            "Select one option from a native <select> dropdown by its visible text. "
            "Do NOT use click() for dropdowns — use this tool instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The ref of the <select> element from get_snapshot.",
                },
                "value": {
                    "type": "string",
                    "description": "The visible text of the option to select (e.g. 'High', 'Active').",
                },
            },
            "required": ["ref", "value"],
        },
    },

    "scroll_into_view": {
        "name": "scroll_into_view",
        "description": (
            "Scroll the viewport until the element is centred on screen. "
            "Call this before clicking or typing into elements that may be below the fold."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The ref of the element to scroll to.",
                }
            },
            "required": ["ref"],
        },
    },

    "handle_alert": {
        "name": "handle_alert",
        "description": (
            "Accept or dismiss a browser alert, confirm, or prompt dialog. "
            "Must be called immediately when a dialog appears — any other action "
            "while a dialog is open will fail with UnexpectedAlertPresentException."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["accept", "dismiss"],
                    "description": "'accept' clicks OK; 'dismiss' clicks Cancel.",
                }
            },
            "required": ["action"],
        },
    },

    "wait_for_element": {
        "name": "wait_for_element",
        "description": (
            "Wait for a CSS selector to become visible on screen. "
            "Use after slow actions (form submit, page load, async fetch) to confirm "
            "the expected result appeared before continuing. Do not use time.sleep. "
            "poll_interval controls how frequently the DOM is re-checked (default 0.5s)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector to wait for (e.g. '.success-message', '#order-id').",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds to wait. Default 10.",
                },
                "poll_interval": {
                    "type": "number",
                    "description": (
                        "How often (seconds) to re-check the DOM. Default 0.5. "
                        "Lower = faster detection, higher = less CPU on slow pages."
                    ),
                },
            },
            "required": ["selector"],
        },
    },

    # ── Phase 2 tools: mouse gestures ────────────────────────────────────────

    "hover": {
        "name": "hover",
        "description": (
            "Move the mouse over an element without clicking. "
            "Use to trigger :hover CSS styles, tooltip overlays, or dropdown menus "
            "that only appear on hover. Call get_snapshot after to see revealed elements."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Element ref from get_snapshot."},
            },
            "required": ["ref"],
        },
    },

    "double_click": {
        "name": "double_click",
        "description": (
            "Double-click an element. Use for file open, inline cell editing, "
            "tree node expansion, or any control that requires two rapid clicks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Element ref from get_snapshot."},
            },
            "required": ["ref"],
        },
    },

    "right_click": {
        "name": "right_click",
        "description": (
            "Right-click an element to open its context menu. "
            "Call get_snapshot after to see the menu items, then click the desired option."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Element ref from get_snapshot."},
            },
            "required": ["ref"],
        },
    },

    "drag_and_drop": {
        "name": "drag_and_drop",
        "description": (
            "Drag one element and drop it onto another. "
            "Use for reordering list items, kanban card moves, and drag-to-upload zones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_ref": {"type": "string", "description": "Ref of the element to drag."},
                "target_ref": {"type": "string", "description": "Ref of the element to drop onto."},
            },
            "required": ["source_ref", "target_ref"],
        },
    },

    # ── Phase 2 tools: keyboard ───────────────────────────────────────────────

    "press_key": {
        "name": "press_key",
        "description": (
            "Press a keyboard key on the currently focused element or page body. "
            "Accepted names: enter, tab, escape, space, backspace, delete, home, end, "
            "pageup, pagedown, arrowup, arrowdown, arrowleft, arrowright, f1-f12. "
            "Single printable characters are also accepted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key name (case-insensitive) or a single character.",
                },
            },
            "required": ["key"],
        },
    },

    "key_combo": {
        "name": "key_combo",
        "description": (
            "Send a keyboard shortcut to an element (e.g. Ctrl+S, Ctrl+A, Shift+Tab). "
            "Modifier keys: ctrl, shift, alt, meta. "
            "Use ref='body' to target the whole page. "
            "Example: key_combo(ref='el-3', keys=['ctrl', 's'])"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Element ref, or 'body' to target the page.",
                },
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keys to chord: modifiers first, then the main key. E.g. ['ctrl','s'].",
                },
            },
            "required": ["ref", "keys"],
        },
    },

    # ── Phase 2 tools: form helpers ───────────────────────────────────────────

    "check": {
        "name": "check",
        "description": (
            "Ensure a checkbox or toggle is checked (ticked on). "
            "Safe no-op if already checked — will not double-toggle."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Ref of the checkbox element from get_snapshot."},
            },
            "required": ["ref"],
        },
    },

    "uncheck": {
        "name": "uncheck",
        "description": (
            "Ensure a checkbox or toggle is unchecked (ticked off). "
            "Safe no-op if already unchecked — will not double-toggle."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Ref of the checkbox element from get_snapshot."},
            },
            "required": ["ref"],
        },
    },

    "upload_file": {
        "name": "upload_file",
        "description": (
            "Upload a file by sending its absolute path to a file input element. "
            "No file-picker dialog interaction needed. "
            "The path must be on the machine running the browser."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Ref of the <input type='file'> element."},
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file on disk (e.g. 'C:/reports/brd.pdf').",
                },
            },
            "required": ["ref", "file_path"],
        },
    },

    "clear_input": {
        "name": "clear_input",
        "description": (
            "Clear a text input field using Ctrl+A then Delete. "
            "React-safe — triggers onChange correctly unlike element.clear(). "
            "Use before type_text when you need to replace existing content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Ref of the input or textarea element."},
            },
            "required": ["ref"],
        },
    },

    # ── Phase 2 tools: element inspection ────────────────────────────────────

    "is_visible": {
        "name": "is_visible",
        "description": (
            "Check whether an element is currently visible on screen. "
            "Returns {visible: bool}. Use to branch on whether an error message, "
            "spinner, or panel is shown without raising an exception."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Element ref from get_snapshot."},
            },
            "required": ["ref"],
        },
    },

    "get_element_text": {
        "name": "get_element_text",
        "description": (
            "Return the current visible text or value of an element. "
            "Use for reading dynamic values — prices, statuses, error messages, "
            "order IDs — that may have changed since the last get_snapshot."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Element ref from get_snapshot."},
            },
            "required": ["ref"],
        },
    },

    "get_attribute": {
        "name": "get_attribute",
        "description": (
            "Return the value of any HTML attribute on an element. "
            "Use for: aria-checked (checkbox state), href (link target), "
            "disabled (button state), value (input content), data-* attributes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Element ref from get_snapshot."},
                "attribute": {
                    "type": "string",
                    "description": "HTML attribute name (e.g. 'aria-checked', 'href', 'value', 'disabled').",
                },
            },
            "required": ["ref", "attribute"],
        },
    },

    # ── Phase 2 tools: window and tab management ──────────────────────────────

    "list_tabs": {
        "name": "list_tabs",
        "description": (
            "Return all open browser tabs with their index, title, and handle. "
            "Use the index with switch_tab() to move between tabs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    "new_tab": {
        "name": "new_tab",
        "description": (
            "Open a new browser tab and optionally navigate to a URL. "
            "The new tab becomes active. Call get_snapshot to interact with it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to open in the new tab. Leave empty for a blank tab.",
                },
            },
            "required": [],
        },
    },

    "switch_tab": {
        "name": "switch_tab",
        "description": (
            "Switch focus to a browser tab by its zero-based index (from list_tabs). "
            "Call get_snapshot after switching to see the new tab's elements."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "description": "Zero-based tab index (0 = first/original tab).",
                },
            },
            "required": ["index"],
        },
    },

    "close_tab": {
        "name": "close_tab",
        "description": (
            "Close the currently active browser tab. "
            "Focus moves to the last remaining tab. "
            "Returns an error if this is the last tab."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    # ── Phase 2 tools: browser control ───────────────────────────────────────

    "resize_window": {
        "name": "resize_window",
        "description": (
            "Resize the browser viewport to test responsive layouts. "
            "Common sizes: 1440x900 (desktop), 768x1024 (tablet), 375x812 (mobile)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "width":  {"type": "integer", "description": "Viewport width in pixels."},
                "height": {"type": "integer", "description": "Viewport height in pixels."},
            },
            "required": ["width", "height"],
        },
    },

    "evaluate_js": {
        "name": "evaluate_js",
        "description": (
            "Execute arbitrary JavaScript in the browser and return the result. "
            "Use 'return' to send a value back. Result must be JSON-serialisable. "
            "Use for: reading localStorage, checking React state, measuring computed style."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "JavaScript to execute (e.g. 'return document.title').",
                },
            },
            "required": ["script"],
        },
    },

    "get_console_messages": {
        "name": "get_console_messages",
        "description": (
            "Return browser JavaScript console messages (errors, warnings, logs). "
            "Returns empty list with a warning if browser logging was not enabled at startup."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    "get_network_requests": {
        "name": "get_network_requests",
        "description": (
            "Return resource timing data for network requests made by the page "
            "(XHR, fetch, scripts, images). Uses the browser Performance API — "
            "no proxy needed. Does not include request/response headers or bodies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    # ── Phase 3: Advanced browser exploration ──────────────────────────────

    "handle_alert": {
        "name": "handle_alert",
        "description": (
            "Handle a browser alert/confirm/prompt dialog. "
            "These are JavaScript dialogs (not HTML modals) that block the page. "
            "Use 'accept' to click OK, 'dismiss' to click Cancel, or 'get_text' to read the message. "
            "After handling, call get_snapshot() to continue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["accept", "dismiss", "get_text"],
                    "description": "Action to perform: accept (OK), dismiss (Cancel), or get_text (read).",
                },
            },
            "required": ["action"],
        },
    },

    "switch_to_window": {
        "name": "switch_to_window",
        "description": (
            "Switch focus to a different browser window or tab. "
            "Use this when clicking something opens a new tab/window. "
            "In Selenium, tabs and windows are handled uniformly as 'windows' with handles. "
            "After switching, call get_snapshot() to see the new window's content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": (
                        "The window handle to switch to. Get available handles from a previous "
                        "get_snapshot call or from checking open windows. Use 'main' to switch back "
                        "to the first/original window."
                    ),
                },
            },
            "required": ["handle"],
        },
    },

    "get_all_windows": {
        "name": "get_all_windows",
        "description": (
            "Get a list of all open browser windows and tabs. "
            "Returns handles, titles, and URLs for each. "
            "Use with switch_to_window() to navigate between them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    "scroll_page": {
        "name": "scroll_page",
        "description": (
            "Scroll the page to reveal lazy-loaded or cut-off content. "
            "Call get_snapshot() after scrolling to see newly-loaded elements. "
            "Limit scrolling to avoid infinite loops (max 5-10 scrolls per page)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["down", "up"],
                    "description": "Scroll direction: 'down' (default) or 'up'.",
                },
                "amount": {
                    "type": "integer",
                    "description": "Number of scroll steps (default 3, typically 1-5 steps per call).",
                },
            },
            "required": [],
        },
    },

    "wait_for_dynamic_content": {
        "name": "wait_for_dynamic_content",
        "description": (
            "Wait for dynamic/AJAX content to finish loading. "
            "Detects spinners, loaders, and progress indicators. "
            "Use when a page is loading asynchronously. "
            "After waiting, call get_snapshot() to see the loaded content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds to wait (default 10).",
                },
            },
            "required": [],
        },
    },

    "detect_modals": {
        "name": "detect_modals",
        "description": (
            "Detect and list all visible modal/popup dialogs on the page. "
            "Modals are indicated by role='dialog', aria-modal='true', or modal/dialog classes. "
            "Unlike JavaScript alerts, these are HTML elements. "
            "You can interact with them normally (click buttons, fill inputs)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },

    # ── Crawl-specific tool ────────────────────────────────────────────────

    "fill_form_for_exploration": {
        "name": "fill_form_for_exploration",
        "description": (
            "Fill all visible form inputs with minimal synthetic values based only on each "
            "field's HTML type attribute. No real, BRD-sourced, or domain-specific data is used. "
            "Use this INSTEAD of type_text when you need to submit a form to discover what the "
            "application does next (dialogs, redirects, validation errors). "
            "Skips password, file, hidden, submit, button, reset, and image inputs automatically. "
            "Returns a list of fields filled and fields skipped with reasons."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "form_selector": {
                    "type": "string",
                    "description": (
                        "Optional CSS selector to scope the fill to a specific form element "
                        "(e.g. 'form', 'form#escalation-form'). "
                        "If omitted, all visible fillable inputs on the page are targeted."
                    ),
                },
            },
            "required": [],
        },
    },
}
