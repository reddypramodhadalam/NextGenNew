#!/usr/bin/env python
import sys
sys.path.insert(0, '.')
from core.db.connection import get_conn, init_pool
import json

# Initialize the pool
init_pool()

# Find the latest proposals with matched: false
with get_conn() as conn:
    with conn.cursor() as cur:
        # Get proposals with unmatched steps
        cur.execute('''
            SELECT id, title, tags, steps
            FROM test_case_proposals 
            ORDER BY created_at DESC
            LIMIT 20
        ''')
        for row in cur.fetchall():
            pid, title, tags, steps = row
            print(f'\n=== Proposal {pid}: {title[:60]}')
            
            tags_obj = tags if isinstance(tags, dict) else json.loads(tags or '{}')
            if 'unmatched_steps' in tags_obj and tags_obj['unmatched_steps']:
                print(f'  Unmatched steps summary: {tags_obj["unmatched_steps"]}')
            
            # Get the steps
            if steps:
                steps_obj = steps if isinstance(steps, list) else json.loads(steps or '[]')
                unmatched_found = False
                for i, step in enumerate(steps_obj):
                    if isinstance(step, dict):
                        matched = step.get('matched', True)
                        if not matched:
                            unmatched_found = True
                            print(f'    Step {i+1}: {step.get("step")[:50]}...')
                            print(f'      element_hint: [{step.get("element_hint")}]')
                if not unmatched_found:
                    print('    (No unmatched steps in detail)')
