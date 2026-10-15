#!/usr/bin/env python
import sys, json
sys.path.insert(0, '.')
from core.db.connection import get_conn, init_pool

init_pool()

with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute('SELECT map_data FROM application_maps ORDER BY crawled_at DESC LIMIT 1')
        row = cur.fetchone()
        if row:
            map_data = row[0]
            if isinstance(map_data, str):
                map_data = json.loads(map_data)
            
            pages = map_data.get('pages', [])
            print(f'Total pages: {len(pages)}')
            
            # Count elements per page
            for i, page in enumerate(pages):
                url = page.get('url', 'unknown')
                els = page.get('elements', [])
                print(f'  Page {i+1}: {len(els)} elements | URL: {url[:70]}...')
            
            # Look specifically for elements with id el-33, el-70, etc
            target_refs = {'el-33', 'el-40', 'el-45', 'el-70', 'el-69', 'el-68', 'el-58'}
            print(f'\nSearching for elements: {target_refs}')
            found = False
            for page_idx, page in enumerate(pages):
                for el in page.get('elements', []):
                    if el.get('ref') in target_refs:
                        print(f'  Found {el.get("ref")} on page {page_idx+1}: name=[{el.get("name")}]')
                        found = True
            if not found:
                print('  *** NONE FOUND IN MAP ***')
            
            # Show ALL elements and their refs  
            print(f'\nALL element refs in the map:')
            all_refs = set()
            for page in pages:
                for el in page.get('elements', []):
                    ref = el.get('ref')
                    if ref:
                        all_refs.add(ref)
            for ref in sorted(all_refs):
                print(f'  {ref}')
