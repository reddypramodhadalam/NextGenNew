#!/usr/bin/env python
import sys, json
sys.path.insert(0, '.')
from core.db.connection import get_conn, init_pool
from service.map_formatter import identifiers_from_map

init_pool()

with get_conn() as conn:
    with conn.cursor() as cur:
        # Get the latest map
        cur.execute('''
            SELECT map_data FROM application_maps 
            ORDER BY crawled_at DESC LIMIT 1
        ''')
        row = cur.fetchone()
        if row:
            map_data = row[0]
            if isinstance(map_data, str):
                map_data = json.loads(map_data)
            
            # Show elements from first page
            if 'pages' in map_data and map_data['pages']:
                page = map_data['pages'][0]
                print(f"First page has {len(page.get('elements', []))} elements")
                print('\nFirst 10 elements:')
                for i, el in enumerate(page.get('elements', [])[:10]):
                    print(f"  {i+1}. ref={el.get('ref')}, role={el.get('role')}, name={el.get('name')}")
            
            # Get identifiers
            ids = identifiers_from_map(map_data)
            print(f'\nTotal identifiers: {len(ids)}')
            print('\nFirst 20 identifiers:')
            for i, idx in enumerate(sorted(ids)[:20]):
                print(f"  '{idx}'")
