import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from local_osm.db import (connect, seed_demo, encode_way_node_ids, insert_way_topology_rows,
                          encode_way_geometry, insert_way_geometry_rows,
                          encode_way_geometry_v5, insert_way_geometry_v5_rows,
                          OSMObject, insert_object)
from local_osm.road_index import (
    build_road_index,
    encode_fixed_node_ids,
    fixed_node_ids_view,
    road_index_fast_nodes_ready,
    road_index_ready,
)
from local_osm.planner_stats import build_planner_stats, planner_stats_ready
from local_osm.tag_spatial_index import build_tag_spatial_index, tag_spatial_index_ready
from local_osm.query import (execute_overpass_subset, QueryProfile, UnsupportedQuery, parse_selectors,
                             _bounded_spatial_count)

BBOX = "60.40,22.20,60.50,22.35"


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'test.sqlite3'
        self.con = connect(self.db)
        seed_demo(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def q(self, text):
        return execute_overpass_subset(self.con, text.replace('{{bbox}}', BBOX))

    def test_old_hospital_union_still_supported(self):
        out = self.q('[out:json];(area({{bbox}})[amenity=hospital]; node({{bbox}})[amenity=hospital]; area({{bbox}})[building=hospital]; node({{bbox}})[building=hospital];); out center;')
        self.assertGreaterEqual(len(out), 2)

    def test_0997_nwr_hospital_union(self):
        out = self.q('[out:json];(nwr({{bbox}})[amenity=hospital]; nwr({{bbox}})[building=hospital];); out center;')
        self.assertGreaterEqual(len(out), 2)
        self.assertTrue(any(e.get('tags', {}).get('amenity') == 'hospital' for e in out))

    def test_0997_multiple_filters_and_not_equal(self):
        out = self.q('[out:json];( nwr({{bbox}})[leisure=pitch][sport=soccer][surface!=artificial_turf][indoor!=yes];nwr({{bbox}})[leisure=pitch][surface=grass];nwr({{bbox}})[emergency=mountain_rescue]; );out center;')
        self.assertTrue(any(e.get('tags', {}).get('leisure') == 'pitch' for e in out))

    def test_0997_existence_filter(self):
        out = self.q('[out:json];(nwr({{bbox}})[amenity=parking]; nwr({{bbox}})[highway];);out center;')
        self.assertTrue(any('highway' in e.get('tags', {}) for e in out))

    def test_0997_filter_before_bbox(self):
        out = self.q('[out:json];(nwr[crossing=uncontrolled]({{bbox}});nwr[highway=crossing]({{bbox}}););(._;>;); out center;')
        self.assertTrue(any(e.get('tags', {}).get('crossing') == 'uncontrolled' for e in out))

    def test_0997_single_quoted_regex(self):
        out = self.q("[out:json];nwr({{bbox}})[building~'^(apartments|house)$'];(._;>;);out center;")
        self.assertTrue(any(e.get('tags', {}).get('building') == 'apartments' for e in out))

    def test_recursive_way_output_contains_nodes(self):
        out = self.q('[out:json];nwr({{bbox}})[water=lake];(._;>;);out;')
        way = next(e for e in out if e['type'] == 'way' and e.get('tags', {}).get('water') == 'lake')
        self.assertGreaterEqual(len(way.get('nodes', [])), 2)
        returned_node_ids = {e['id'] for e in out if e['type'] == 'node'}
        self.assertTrue(set(way['nodes']).issubset(returned_node_ids))

    def test_recursive_relation_output_reaches_way_nodes(self):
        out = self.q('[out:json];nwr({{bbox}})[water=lake];(._;>;);out;')
        rel = next(e for e in out if e['type'] == 'relation')
        self.assertTrue(any(m['type'] == 'way' for m in rel.get('members', [])))
        self.assertTrue(any(e['type'] == 'node' and e['id'] == 900000020 for e in out))

    def test_road_intersection(self):
        out = self.q('[out:json];way({{bbox}})[highway~"^(motorway|trunk|primary|secondary|tertiary|(motorway|trunk|primary|secondary)_link)$"]->.major;way({{bbox}})[highway~"^(unclassified|residential|living_street|service)$"]->.minor;node(w.major)(w.minor);out;')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['id'], 900000011)

    def test_road_index_v2_fixed_node_payload_roundtrip(self):
        ids = [1, 900000011, 2**40 + 123]
        payload = encode_fixed_node_ids(ids)
        self.assertEqual(list(fixed_node_ids_view(payload)), ids)

    def test_road_index_v1_remains_runtime_compatible(self):
        # Build a v2 payload, then downgrade metadata to exercise v1 compatibility.
        rows = []
        for way_id in (900000009, 900000010):
            node_ids, coordinates = encode_way_geometry_v5([
                (900000011, 60.4510, 22.2640)
            ])
            rows.append((way_id, node_ids, coordinates))
        insert_way_geometry_v5_rows(self.con, rows)
        self.con.commit()
        build_road_index(self.con, verbose=False)
        self.assertTrue(road_index_fast_nodes_ready(self.con))

        self.con.execute(
            "INSERT INTO metadata(key,value) VALUES('road_index_version','1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'"
        )
        self.con.commit()
        self.assertTrue(road_index_ready(self.con))
        self.assertFalse(road_index_fast_nodes_ready(self.con))

        import local_osm.query as query_module
        query_module._has_schema5_way_geometry.cache_clear()
        query_module._has_schema4_way_geometry.cache_clear()

        statements = []
        self.con.set_trace_callback(statements.append)
        try:
            out = self.q('[out:json];way({{bbox}})[highway~"^(motorway|trunk|primary|secondary|tertiary|(motorway|trunk|primary|secondary)_link)$"]->.major;way({{bbox}})[highway~"^(unclassified|residential|living_street|service)$"]->.minor;node(w.major)(w.minor);out;')
        finally:
            self.con.set_trace_callback(None)

        self.assertEqual([e['id'] for e in out], [900000011])
        self.assertTrue(any('way_geometry_v5' in sql for sql in statements))

    def test_road_intersection_with_packed_topology(self):
        self.con.execute('DELETE FROM way_nodes WHERE way_id IN (900000009,900000010)')
        insert_way_topology_rows(self.con, [
            (900000009, encode_way_node_ids([900000011])),
            (900000010, encode_way_node_ids([900000011])),
        ])
        self.con.commit()
        out = self.q('[out:json];way({{bbox}})[highway~"^(motorway|trunk|primary|secondary|tertiary|(motorway|trunk|primary|secondary)_link)$"]->.major;way({{bbox}})[highway~"^(unclassified|residential|living_street|service)$"]->.minor;node(w.major)(w.minor);out;')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['id'], 900000011)

    def test_road_intersection_with_schema4_embedded_coordinates_only(self):
        self.con.execute('DELETE FROM way_nodes WHERE way_id IN (900000009,900000010)')
        self.con.execute('DELETE FROM way_topology WHERE way_id IN (900000009,900000010)')
        self.con.execute('DELETE FROM node_coords WHERE node_id=900000011')
        row = self.con.execute(
            "SELECT pk FROM objects WHERE osm_type='node' AND osm_id=900000011"
        ).fetchone()
        if row is not None:
            self.con.execute('DELETE FROM object_rtree WHERE pk=?', (int(row[0]),))
            self.con.execute('DELETE FROM objects WHERE pk=?', (int(row[0]),))
        insert_way_geometry_rows(self.con, [
            (900000009, encode_way_geometry([(900000011, 60.4510, 22.2640)])),
            (900000010, encode_way_geometry([(900000011, 60.4510, 22.2640)])),
        ])
        self.con.commit()
        profile = QueryProfile()
        out = execute_overpass_subset(
            self.con,
            '[out:json];way(60.40,22.20,60.50,22.35)[highway~"^(motorway|trunk|primary|secondary|tertiary|(motorway|trunk|primary|secondary)_link)$"]->.major;way(60.40,22.20,60.50,22.35)[highway~"^(unclassified|residential|living_street|service)$"]->.minor;node(w.major)(w.minor);out;',
            profile=profile,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['id'], 900000011)
        self.assertAlmostEqual(out[0]['lat'], 60.4510, places=7)
        self.assertAlmostEqual(out[0]['lon'], 22.2640, places=7)
        self.assertEqual(profile.spatial_seeded_calls, 1)
        self.assertEqual(profile.candidate_calls, 1)
        self.assertGreaterEqual(profile.road_topology_ms, 0.0)

    def test_road_intersection_with_schema5_split_geometry_only(self):
        self.con.execute('DELETE FROM way_nodes WHERE way_id IN (900000009,900000010)')
        self.con.execute('DELETE FROM way_topology WHERE way_id IN (900000009,900000010)')
        self.con.execute('DELETE FROM way_geometry WHERE way_id IN (900000009,900000010)')
        self.con.execute('DELETE FROM node_coords WHERE node_id=900000011')
        row = self.con.execute(
            "SELECT pk FROM objects WHERE osm_type='node' AND osm_id=900000011"
        ).fetchone()
        if row is not None:
            self.con.execute('DELETE FROM object_rtree WHERE pk=?', (int(row[0]),))
            self.con.execute('DELETE FROM objects WHERE pk=?', (int(row[0]),))

        rows = []
        for way_id in (900000009, 900000010):
            node_ids, coordinates = encode_way_geometry_v5([
                (900000011, 60.4510, 22.2640)
            ])
            rows.append((way_id, node_ids, coordinates))
        insert_way_geometry_v5_rows(self.con, rows)
        self.con.commit()

        import local_osm.query as query_module
        query_module._has_schema5_way_geometry.cache_clear()
        query_module._has_schema4_way_geometry.cache_clear()

        profile = QueryProfile()
        out = execute_overpass_subset(
            self.con,
            '[out:json];way(60.40,22.20,60.50,22.35)[highway~"^(motorway|trunk|primary|secondary|tertiary|(motorway|trunk|primary|secondary)_link)$"]->.major;way(60.40,22.20,60.50,22.35)[highway~"^(unclassified|residential|living_street|service)$"]->.minor;node(w.major)(w.minor);out;',
            profile=profile,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['id'], 900000011)
        self.assertAlmostEqual(out[0]['lat'], 60.4510, places=7)
        self.assertAlmostEqual(out[0]['lon'], 22.2640, places=7)
        self.assertEqual(profile.spatial_seeded_calls, 1)

    def test_schema5_road_index_discovers_with_id_streams_before_targeted_coordinates(self):
        # Discovery uses node IDs first and fetches coordinates only for contributing ways.
        self.con.execute('DELETE FROM way_nodes WHERE way_id IN (900000009,900000010)')
        self.con.execute('DELETE FROM way_topology WHERE way_id IN (900000009,900000010)')
        self.con.execute('DELETE FROM way_geometry WHERE way_id IN (900000009,900000010)')
        rows = []
        for way_id in (900000009, 900000010):
            node_ids, coordinates = encode_way_geometry_v5([
                (900000011, 60.4510, 22.2640)
            ])
            rows.append((way_id, node_ids, coordinates))
        insert_way_geometry_v5_rows(self.con, rows)
        self.con.commit()
        build_road_index(self.con, verbose=False)

        import local_osm.query as query_module
        query_module._has_schema5_way_geometry.cache_clear()
        query_module._has_schema4_way_geometry.cache_clear()

        statements = []
        self.con.set_trace_callback(statements.append)
        try:
            profile = QueryProfile()
            out = execute_overpass_subset(
                self.con,
                '[out:json];way(60.40,22.20,60.50,22.35)[highway~"^(motorway|trunk|primary|secondary|tertiary|(motorway|trunk|primary|secondary)_link)$"]->.major;way(60.40,22.20,60.50,22.35)[highway~"^(unclassified|residential|living_street|service)$"]->.minor;node(w.major)(w.minor);out;',
                profile=profile,
            )
        finally:
            self.con.set_trace_callback(None)

        self.assertEqual([e['id'] for e in out], [900000011])
        self.assertAlmostEqual(out[0]['lat'], 60.4510, places=7)
        self.assertAlmostEqual(out[0]['lon'], 22.2640, places=7)
        self.assertEqual(profile.road_index_calls, 1)

        indexed = [
            sql for sql in statements
            if 'FROM road_way_rtree rr' in sql and 'node_ids_fixed' in sql
        ]
        self.assertTrue(indexed)
        self.assertTrue(all('way_geometry_v5' not in sql for sql in indexed))

        targeted = [
            sql for sql in statements
            if 'SELECT way_id,node_ids,coordinates FROM way_geometry_v5 WHERE way_id IN' in sql
        ]
        self.assertTrue(targeted)

    def test_schema5_nonrecursive_way_hydration_reads_id_stream_only(self):
        way_id = 900000019
        self.con.execute('DELETE FROM way_geometry WHERE way_id=?', (way_id,))
        node_ids, coordinates = encode_way_geometry_v5([
            (900000020, 60.4375, 22.2935),
            (900000021, 60.4385, 22.2945),
        ])
        insert_way_geometry_v5_rows(self.con, [(way_id, node_ids, coordinates)])
        self.con.commit()

        statements = []
        self.con.set_trace_callback(statements.append)
        out = self.q('[out:json];way({{bbox}})[water=lake];out center;')
        self.con.set_trace_callback(None)

        way = next(e for e in out if e['type'] == 'way' and e['id'] == way_id)
        self.assertEqual(way['nodes'], [900000020, 900000021])
        id_selects = [
            sql for sql in statements
            if 'SELECT way_id,node_ids FROM way_geometry_v5 WHERE way_id IN' in sql
        ]
        self.assertTrue(id_selects)
        self.assertTrue(all('coordinates' not in sql for sql in id_selects))

    def test_recursive_schema5_way_returns_nodes_without_global_node_coords(self):
        way_id = 900000019
        self.con.execute('DELETE FROM way_nodes WHERE way_id=?', (way_id,))
        self.con.execute('DELETE FROM way_topology WHERE way_id=?', (way_id,))
        self.con.execute('DELETE FROM way_geometry WHERE way_id=?', (way_id,))
        for node_id in (900000020, 900000021):
            self.con.execute('DELETE FROM node_coords WHERE node_id=?', (node_id,))
            row = self.con.execute(
                "SELECT pk FROM objects WHERE osm_type='node' AND osm_id=?", (node_id,)
            ).fetchone()
            if row is not None:
                self.con.execute('DELETE FROM object_rtree WHERE pk=?', (int(row[0]),))
                self.con.execute('DELETE FROM objects WHERE pk=?', (int(row[0]),))

        node_ids, coordinates = encode_way_geometry_v5([
            (900000020, 60.4375, 22.2935),
            (900000021, 60.4385, 22.2945),
        ])
        insert_way_geometry_v5_rows(self.con, [(way_id, node_ids, coordinates)])
        self.con.commit()

        out = self.q('[out:json];nwr({{bbox}})[water=lake];(._;>;);out;')
        nodes = {e['id']: e for e in out if e['type'] == 'node'}
        self.assertIn(900000020, nodes)
        self.assertIn(900000021, nodes)
        self.assertAlmostEqual(nodes[900000020]['lat'], 60.4375, places=7)

    def test_recursive_schema4_way_returns_nodes_without_global_node_coords(self):
        self.con.execute('DELETE FROM way_nodes WHERE way_id=900000019')
        self.con.execute('DELETE FROM way_topology WHERE way_id=900000019')
        for node_id in (900000020, 900000021):
            self.con.execute('DELETE FROM node_coords WHERE node_id=?', (node_id,))
            row = self.con.execute(
                "SELECT pk FROM objects WHERE osm_type='node' AND osm_id=?", (node_id,)
            ).fetchone()
            if row is not None:
                self.con.execute('DELETE FROM object_rtree WHERE pk=?', (int(row[0]),))
                self.con.execute('DELETE FROM objects WHERE pk=?', (int(row[0]),))
        insert_way_geometry_rows(self.con, [(900000019, encode_way_geometry([
            (900000020, 60.4375, 22.2935),
            (900000021, 60.4385, 22.2945),
        ]))])
        self.con.commit()
        out = self.q('[out:json];nwr({{bbox}})[water=lake];(._;>;);out;')
        nodes = {e['id']: e for e in out if e['type'] == 'node'}
        self.assertIn(900000020, nodes)
        self.assertIn(900000021, nodes)
        self.assertAlmostEqual(nodes[900000020]['lat'], 60.4375, places=7)

    def test_hpg_runtime_around_selector_exact_capture(self):
        out = execute_overpass_subset(
            self.con,
            '[out:json];area(around: 1250000, 60.42302046615535,24.742823633265154)[building=apartments];out center;'
        )
        self.assertTrue(any(e.get('tags', {}).get('building') == 'apartments' for e in out))

    def test_around_selector_respects_radius_for_nodes(self):
        out = execute_overpass_subset(
            self.con,
            '[out:json];node(around:1000,60.4518,22.2666)[amenity=hospital];out center;'
        )
        self.assertTrue(any(e.get('tags', {}).get('amenity') == 'hospital' for e in out))
        far = execute_overpass_subset(
            self.con,
            '[out:json];node(around:10,60.0,22.0)[amenity=hospital];out center;'
        )
        self.assertEqual(far, [])

    def test_hpg_runtime_query_country_is_in_exact_capture(self):
        out = execute_overpass_subset(
            self.con,
            '[out:json]; is_in(60.18892376133562, 24.90749935924897)->.a; rel(pivot.a)[boundary=administrative][admin_level=2]; out tags;'
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['type'], 'relation')
        self.assertEqual(out[0]['tags'].get('name'), 'Finland')
        self.assertEqual(out[0]['tags'].get('admin_level'), '2')

    def test_hpg_runtime_query_country_outside_demo_country(self):
        out = execute_overpass_subset(
            self.con,
            '[out:json];is_in(40.0,-75.0)->.a;rel(pivot.a)[boundary=administrative][admin_level=2];out tags;'
        )
        self.assertEqual(out, [])

    def test_hpg_runtime_cliff_sampling_exact_capture_returns_demo_result(self):
        out = execute_overpass_subset(
            self.con,
            '[out:json];node(around: 1500, 60.20386151424147,25.138018381563445)[natural=cliff];out center;'
        )
        self.assertTrue(any(e.get('tags', {}).get('natural') == 'cliff' for e in out))
        self.assertTrue(any(e.get('tags', {}).get('name') == 'Synthetic Helsinki Cliff' for e in out))

    def test_selective_equal_filter_uses_tag_seed_and_no_per_candidate_tag_selects(self):
        statements = []
        self.con.set_trace_callback(statements.append)
        profile = QueryProfile()
        out = execute_overpass_subset(
            self.con,
            '[out:json];node(around:1000,60.4518,22.2666)[amenity=hospital];out center;',
            profile=profile,
        )
        self.con.set_trace_callback(None)
        self.assertTrue(out)
        self.assertEqual(profile.tag_seeded_calls, 1)
        self.assertEqual(profile.spatial_seeded_calls, 0)
        self.assertEqual(profile.candidate_rows, 1)
        # Candidate filtering must not issue per-object tag queries.
        n_plus_one = [
            sql for sql in statements
            if 'SELECT key,value FROM object_tags WHERE object_pk=' in sql
        ]
        self.assertEqual(n_plus_one, [])

    def test_category_regexes_use_tag_value_seed(self):
        profile = QueryProfile()
        out = self.q('[out:json];way({{bbox}})[highway~"^(motorway|trunk|primary|secondary|tertiary|(motorway|trunk|primary|secondary)_link)$"]->.major;way({{bbox}})[highway~"^(unclassified|residential|living_street|service)$"]->.minor;node(w.major)(w.minor);out;')
        execute_overpass_subset(
            self.con,
            '[out:json];way(60.40,22.20,60.50,22.35)[highway~"^(motorway|trunk|primary|secondary|tertiary|(motorway|trunk|primary|secondary)_link)$"]->.major;way(60.40,22.20,60.50,22.35)[highway~"^(unclassified|residential|living_street|service)$"]->.minor;node(w.major)(w.minor);out;',
            profile=profile,
        )
        self.assertEqual(out[0]['id'], 900000011)
        self.assertEqual(profile.tag_seeded_calls, 2)

    def test_recursive_expansion_batches_node_object_lookups(self):
        statements = []
        self.con.set_trace_callback(statements.append)
        out = self.q('[out:json];nwr({{bbox}})[water=lake];(._;>;);out;')
        self.con.set_trace_callback(None)
        self.assertTrue(any(e['type'] == 'node' for e in out))
        # Descendant node lookups should be batched.
        per_node = [
            sql for sql in statements
            if "SELECT * FROM objects WHERE osm_type='node' AND osm_id=" in sql
        ]
        self.assertEqual(per_node, [])

    def test_selective_exists_filter_can_seed_from_key_index(self):
        profile = QueryProfile()
        out = execute_overpass_subset(
            self.con,
            '[out:json];way(60.40,22.20,60.50,22.35)[railway];(._;>;);out;',
            profile=profile,
        )
        self.assertTrue(any(e.get('tags', {}).get('railway') == 'rail' for e in out))
        self.assertEqual(profile.exists_tag_seeded_calls, 1)
        self.assertEqual(profile.spatial_seeded_calls, 0)

    def test_road_profile_does_not_double_count_total_query(self):
        profile = QueryProfile()
        import time
        started = time.perf_counter()
        out = execute_overpass_subset(
            self.con,
            '[out:json];way(60.40,22.20,60.50,22.35)[highway~"^(motorway|trunk|primary|secondary|tertiary|(motorway|trunk|primary|secondary)_link)$"]->.major;way(60.40,22.20,60.50,22.35)[highway~"^(unclassified|residential|living_street|service)$"]->.minor;node(w.major)(w.minor);out;',
            profile=profile,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.assertEqual(out[0]['id'], 900000011)
        self.assertEqual(profile.output_ms, 0.0)
        self.assertGreaterEqual(profile.road_topology_ms, 0.0)
        self.assertLessEqual(profile.timed_ms(), elapsed_ms + 1.0)

    def test_schema4_road_index_preserves_intersection_and_is_used(self):
        self.con.execute('DELETE FROM way_nodes WHERE way_id IN (900000009,900000010)')
        self.con.execute('DELETE FROM way_topology WHERE way_id IN (900000009,900000010)')
        insert_way_geometry_rows(self.con, [
            (900000009, encode_way_geometry([(900000011, 60.4510, 22.2640)])),
            (900000010, encode_way_geometry([(900000011, 60.4510, 22.2640)])),
        ])
        self.con.commit()
        rows, _elapsed = build_road_index(self.con, verbose=False)
        self.assertGreaterEqual(rows, 2)
        self.assertTrue(road_index_ready(self.con))
        profile = QueryProfile()
        out = execute_overpass_subset(
            self.con,
            '[out:json];way(60.40,22.20,60.50,22.35)[highway~"^(motorway|trunk|primary|secondary|tertiary|(motorway|trunk|primary|secondary)_link)$"]->.major;way(60.40,22.20,60.50,22.35)[highway~"^(unclassified|residential|living_street|service)$"]->.minor;node(w.major)(w.minor);out;',
            profile=profile,
        )
        self.assertEqual([e['id'] for e in out], [900000011])
        self.assertEqual(profile.road_index_calls, 1)
        self.assertEqual(profile.candidate_calls, 1)

    def test_typed_spatial_count_forces_rtree_join_order(self):
        # The cardinality probe must keep the RTree as the outer loop.
        _bounded_spatial_count.cache_clear()
        traced = []
        self.con.set_trace_callback(traced.append)
        try:
            count = _bounded_spatial_count(
                self.con, 'way', 60.44, 22.25, 60.46, 22.28, 50000
            )
        finally:
            self.con.set_trace_callback(None)
            _bounded_spatial_count.cache_clear()

        self.assertGreaterEqual(count, 0)
        probe_sql = next(
            sql for sql in traced
            if 'SELECT COUNT(*) FROM (SELECT 1 FROM object_rtree r' in sql
        )
        self.assertIn('object_rtree r CROSS JOIN objects o ON o.pk=r.pk', probe_sql)

    def test_adaptive_planner_prefers_spatial_for_common_local_value(self):
        for i in range(10):
            insert_object(self.con, OSMObject(
                'way', 910000000 + i, 65.0 + i * 0.01, 27.0,
                65.0 + i * 0.01, 65.0 + i * 0.01, 27.0, 27.0,
                {'building': 'detached'}, True,
            ))
        insert_object(self.con, OSMObject(
            'way', 919999999, 60.451, 22.264, 60.451, 60.451, 22.264, 22.264,
            {'building': 'detached'}, True,
        ))
        self.con.commit()
        profile = QueryProfile()
        with patch('local_osm.query.ADAPTIVE_SMALL_TAG_LIMIT', 1):
            out = execute_overpass_subset(
                self.con,
                '[out:json];way(60.44,22.25,60.46,22.28)[building=detached];out center;',
                profile=profile,
            )
        self.assertTrue(any(e['id'] == 919999999 for e in out))
        self.assertEqual(profile.adaptive_spatial_first_calls, 1)
        self.assertEqual(profile.spatial_seeded_calls, 1)

    def test_selective_tag_spatial_index_preserves_results_and_is_used(self):
        # A common tag in a small envelope should use the selective spatial index.
        for i in range(40):
            insert_object(self.con, OSMObject(
                'way', 930000000 + i, 65.0 + i * 0.001, 27.0,
                65.0 + i * 0.001, 65.0 + i * 0.001, 27.0, 27.0,
                {'building': 'detached'}, True,
            ))
        insert_object(self.con, OSMObject(
            'way', 939999999, 60.451, 22.264, 60.451, 60.451, 22.264, 22.264,
            {'building': 'detached'}, True,
        ))
        self.con.commit()

        query = '[out:json];way(60.44,22.25,60.46,22.28)[building=detached];out center;'
        with patch('local_osm.query.ADAPTIVE_SMALL_TAG_LIMIT', 1):
            before = execute_overpass_subset(self.con, query)

        total, counts, _elapsed = build_tag_spatial_index(self.con, verbose=False)
        self.assertGreater(total, 0)
        self.assertGreater(counts['building_detached'], 0)
        self.assertTrue(tag_spatial_index_ready(self.con))

        import local_osm.query as query_module
        query_module._tag_spatial_bundle_ready.cache_clear()
        statements = []
        self.con.set_trace_callback(statements.append)
        profile = QueryProfile()
        with patch('local_osm.query.ADAPTIVE_SMALL_TAG_LIMIT', 1):
            after = execute_overpass_subset(self.con, query, profile=profile)
        self.con.set_trace_callback(None)

        self.assertEqual(before, after)
        self.assertEqual([e['id'] for e in after], [939999999])
        self.assertEqual(profile.tag_spatial_index_calls, 1)
        self.assertEqual(profile.spatial_seeded_calls, 1)
        feature_selects = [sql for sql in statements if 'rne_ts_building_detached' in sql]
        self.assertTrue(feature_selects)
        self.assertTrue(all('object_tags f0' not in sql for sql in feature_selects))

    def test_country_profile_uses_non_overlapping_is_in_stages(self):
        profile = QueryProfile()
        out = execute_overpass_subset(
            self.con,
            '[out:json];is_in(60.18892376133562,24.90749935924897)->.a;rel(pivot.a)[boundary=administrative][admin_level=2];out tags;',
            profile=profile,
        )
        self.assertEqual(len(out), 1)
        self.assertGreaterEqual(profile.is_in_sql_ms, 0.0)
        self.assertGreaterEqual(profile.is_in_hydrate_ms, 0.0)
        self.assertGreaterEqual(profile.is_in_geometry_ms, 0.0)
        self.assertEqual(profile.candidate_sql_ms, 0.0)

    def test_planner_stats_commits_before_chained_tag_spatial_builder(self):
        # Planner statistics must leave a clean transaction boundary for the next builder.
        keys, values, _elapsed = build_planner_stats(self.con, verbose=False)
        self.assertGreaterEqual(keys, 2)
        self.assertGreater(values, 0)
        self.assertFalse(self.con.in_transaction)

        total, _counts, _elapsed = build_tag_spatial_index(self.con, verbose=False)
        self.assertGreaterEqual(total, 0)
        self.assertFalse(self.con.in_transaction)

    def test_persisted_planner_stats_preserve_candidate_results(self):
        query = '[out:json];node(around:1000,60.4518,22.2666)[amenity=hospital];out center;'
        before = execute_overpass_subset(self.con, query)
        keys, values, _elapsed = build_planner_stats(self.con, verbose=False)
        self.con.commit()
        self.assertTrue(planner_stats_ready(self.con))
        self.assertGreaterEqual(keys, 2)
        self.assertGreater(values, 0)
        import local_osm.query as query_module
        query_module._bounded_exact_tag_count.cache_clear()
        query_module._bounded_values_tag_count.cache_clear()
        after = execute_overpass_subset(self.con, query)
        self.assertEqual(before, after)

    def test_tag_first_candidate_path_skips_redundant_rtree_lookup(self):
        statements = []
        self.con.set_trace_callback(statements.append)
        profile = QueryProfile()
        out = execute_overpass_subset(
            self.con,
            '[out:json];node(around:1000,60.4518,22.2666)[amenity=hospital];out center;',
            profile=profile,
        )
        self.con.set_trace_callback(None)
        self.assertTrue(out)
        self.assertEqual(profile.tag_seeded_calls, 1)
        candidate_selects = [sql for sql in statements if 'object_tags seed' in sql]
        self.assertTrue(candidate_selects)
        self.assertTrue(all('object_rtree' not in sql for sql in candidate_selects))
        self.assertTrue(all('o.min_lon' in sql for sql in candidate_selects))

    def test_nonrecursive_area_output_skips_way_geometry_hydration(self):
        row = self.con.execute(
            "SELECT osm_id FROM objects WHERE osm_type='way' AND area_eligible=1 LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        way_id = int(row[0])
        self.con.execute(
            "INSERT OR REPLACE INTO way_geometry(way_id,node_data) VALUES(?,?)",
            (way_id, b"\x80"),
        )
        self.con.commit()
        out = self.q('[out:json];area({{bbox}})[building=apartments];out center;')
        self.assertTrue(any(e['type'] == 'area' for e in out))

    def test_recursive_schema4_geometry_is_loaded_once_per_seed_way(self):
        self.con.execute('DELETE FROM way_nodes WHERE way_id=900000019')
        self.con.execute('DELETE FROM way_topology WHERE way_id=900000019')
        self.con.execute('DELETE FROM way_geometry WHERE way_id=900000019')
        insert_way_geometry_rows(self.con, [(900000019, encode_way_geometry([
            (900000020, 60.4375, 22.2935),
            (900000021, 60.4385, 22.2945),
        ]))])
        self.con.commit()
        statements = []
        self.con.set_trace_callback(statements.append)
        out = self.q('[out:json];way({{bbox}})[water=lake];(._;>;);out;')
        self.con.set_trace_callback(None)
        self.assertTrue(any(e['type'] == 'way' and e['id'] == 900000019 for e in out))
        geometry_selects = [
            sql for sql in statements
            if 'SELECT way_id,node_data FROM way_geometry WHERE way_id IN' in sql
            and '900000019' in sql
        ]
        self.assertEqual(len(geometry_selects), 1)

    def test_placeholder_rejected_if_not_substituted(self):
        with self.assertRaises(UnsupportedQuery):
            execute_overpass_subset(self.con, '[out:json];node({{bbox}})[natural=peak];out center;')


if __name__ == '__main__':
    unittest.main()


class HydrationCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'hydration.sqlite3'
        self.con = connect(self.db)
        seed_demo(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_packed_tags_preserve_elements_and_are_used(self):
        from local_osm.hydration_cache import build_packed_tags, packed_tags_ready
        import local_osm.query as query_module
        query = '[out:json];nwr(60.40,22.20,60.50,22.35)[amenity=hospital];out center;'
        baseline = execute_overpass_subset(self.con, query)
        build_packed_tags(self.con, verbose=False)
        query_module._hydration_cache_flags.cache_clear()
        self.assertTrue(packed_tags_ready(self.con))
        profile = QueryProfile()
        accelerated = execute_overpass_subset(self.con, query, profile=profile)
        self.assertEqual(accelerated, baseline)
        self.assertGreaterEqual(profile.packed_tags_calls, 1)
        self.assertGreaterEqual(profile.hydrate_tags_sql_ms, 0.0)
        self.assertGreaterEqual(profile.hydrate_tags_decode_ms, 0.0)

    def test_packed_way_ids_preserve_nonrecursive_way_nodes_and_are_used(self):
        from local_osm.hydration_cache import build_packed_way_ids, packed_way_ids_ready
        import local_osm.query as query_module
        query = '[out:json];way(60.40,22.20,60.50,22.35)[water=lake];out center;'
        baseline = execute_overpass_subset(self.con, query)
        build_packed_way_ids(self.con, verbose=False)
        query_module._hydration_cache_flags.cache_clear()
        self.assertTrue(packed_way_ids_ready(self.con))
        profile = QueryProfile()
        accelerated = execute_overpass_subset(self.con, query, profile=profile)
        self.assertEqual(accelerated, baseline)
        self.assertGreaterEqual(profile.packed_way_ids_calls, 1)
        self.assertGreaterEqual(profile.hydrate_way_sql_ms, 0.0)
        self.assertGreaterEqual(profile.hydrate_way_decode_ms, 0.0)
