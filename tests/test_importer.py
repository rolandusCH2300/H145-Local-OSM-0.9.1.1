import json
import tempfile
import unittest
from pathlib import Path

from local_osm.db import (OSMObject, connect, insert_object, set_area_geometry,
                          encode_way_node_ids, decode_way_node_ids,
                          encode_way_geometry, decode_way_geometry, decode_way_geometry_node_ids,
                          encode_way_geometry_v5, decode_way_geometry_v5,
                          insert_way_topology_rows, insert_way_geometry_rows, insert_way_geometry_v5_rows,
                          insert_relation_member_rows, get_area_geometry,
                          get_way_node_ids, get_way_nodes)
from local_osm.import_profile import DISCOVERY_KEYS, is_country_boundary, is_relevant, is_relevant_items
from local_osm.importer import (ImportObjectBatch, ImportStats, _TopologyWriter, _bounds,
                                _create_bulk_load_indexes, _drop_bulk_load_indexes,
                                _prepare_way_batch, _recover_selected_country_geometry_fallbacks,
                                _resolve_worker_count)
from local_osm.query import execute_overpass_subset
from local_osm.tier_c import finalize_tier_c_tags, prepare_tier_c_schema


class ImportProfileTests(unittest.TestCase):
    def test_rne_relevance_profile(self):
        self.assertTrue(is_relevant({'highway': 'residential'}))
        self.assertTrue(is_relevant({'railway': 'rail'}))
        self.assertTrue(is_relevant({'natural': 'cliff'}))
        self.assertTrue(is_relevant({'building': 'apartments'}))
        self.assertTrue(is_relevant({'amenity': 'hospital'}))
        self.assertFalse(is_relevant({'craft': 'carpenter'}))
        self.assertFalse(is_relevant({'building': 'garage'}))

    def test_country_boundary_is_always_retained(self):
        tags = {'type': 'boundary', 'boundary': 'administrative', 'admin_level': '2', 'name': 'Finland'}
        self.assertTrue(is_country_boundary(tags))
        self.assertTrue(is_relevant(tags))

    def test_fast_taglist_relevance_matches_mapping_semantics(self):
        self.assertTrue(is_relevant_items([('highway', 'service')]))
        self.assertTrue(is_relevant_items([('natural', 'cliff')]))
        self.assertTrue(is_relevant_items([('boundary', 'administrative'), ('admin_level', '2')]))
        self.assertFalse(is_relevant_items([('building', 'garage'), ('name', 'Garage')]))
        self.assertIn('highway', DISCOVERY_KEYS)
        self.assertIn('boundary', DISCOVERY_KEYS)

    def test_bounds(self):
        center_lat, center_lon, min_lat, max_lat, min_lon, max_lon = _bounds([
            (60.0, 24.0), (62.0, 28.0), (61.0, 25.0)
        ])
        self.assertEqual((center_lat, center_lon), (61.0, 26.0))
        self.assertEqual((min_lat, max_lat, min_lon, max_lon), (60.0, 62.0, 24.0, 28.0))


class SelectedCountryGeometryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / 'selected-country.sqlite3')

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _add_way(self, way_id, nodes):
        lats = [lat for _nid, lat, _lon in nodes]
        lons = [lon for _nid, _lat, lon in nodes]
        insert_object(self.con, OSMObject(
            'way', way_id,
            (min(lats) + max(lats)) / 2.0,
            (min(lons) + max(lons)) / 2.0,
            min(lats), max(lats), min(lons), max(lons), {}, False,
        ))
        node_ids, coordinates = encode_way_geometry_v5(nodes)
        insert_way_geometry_v5_rows(self.con, [(way_id, node_ids, coordinates)])

    def _add_country_square(self, relation_id, iso2, first_way, first_node, lat0, lon0):
        n1, n2, n3, n4 = range(first_node, first_node + 4)
        ways = list(range(first_way, first_way + 4))
        self._add_way(ways[0], [(n1, lat0, lon0), (n2, lat0, lon0 + 1.0)])
        self._add_way(ways[1], [(n2, lat0, lon0 + 1.0), (n3, lat0 + 1.0, lon0 + 1.0)])
        self._add_way(ways[2], [(n3, lat0 + 1.0, lon0 + 1.0), (n4, lat0 + 1.0, lon0)])
        self._add_way(ways[3], [(n4, lat0 + 1.0, lon0), (n1, lat0, lon0)])
        insert_object(self.con, OSMObject(
            'relation', relation_id,
            lat0 + 0.5, lon0 + 0.5,
            lat0, lat0 + 1.0, lon0, lon0 + 1.0,
            {
                'type': 'boundary',
                'boundary': 'administrative',
                'admin_level': '2',
                'ISO3166-1:alpha2': iso2,
                'name': f'Country {iso2}',
            },
            True,
        ))
        insert_relation_member_rows(
            self.con,
            relation_id,
            [('way', way_id, 'outer') for way_id in ways],
        )
        self.con.commit()

    def test_selected_country_fallback_recovers_and_counts_only_selected_missing_geometry(self):
        self._add_country_square(1000, 'NO', 100, 1, 0.0, 0.0)
        self._add_country_square(1001, 'SE', 200, 10, 10.0, 10.0)
        stats = ImportStats(country_geometries=0)

        results = _recover_selected_country_geometry_fallbacks(
            self.con,
            stats,
            {'NO'},
        )

        self.assertEqual([(r.relation_id, r.status) for r in results], [(1000, 'recovered')])
        self.assertEqual(stats.country_geometries, 1)
        no_pk = self.con.execute(
            "SELECT pk FROM objects WHERE osm_type='relation' AND osm_id=1000"
        ).fetchone()[0]
        se_pk = self.con.execute(
            "SELECT pk FROM objects WHERE osm_type='relation' AND osm_id=1001"
        ).fetchone()[0]
        self.assertIsNotNone(get_area_geometry(self.con, no_pk))
        self.assertIsNone(get_area_geometry(self.con, se_pk))


class OptimizedSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / 'schema.sqlite3')

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_way_nodes_has_no_global_reverse_index(self):
        names = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertNotIn('idx_way_nodes_node', names)

    def test_composite_topology_tables_are_without_rowid(self):
        rows = {r[0]: r[1] for r in self.con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN ('way_nodes','way_geometry','way_geometry_v5','relation_members','object_tags')"
        )}
        self.assertIn('WITHOUT ROWID', rows['way_nodes'].upper())
        self.assertIn('WITHOUT ROWID', rows['way_geometry'].upper())
        self.assertIn('WITHOUT ROWID', rows['way_geometry_v5'].upper())
        self.assertIn('WITHOUT ROWID', rows['relation_members'].upper())
        self.assertIn('WITHOUT ROWID', rows['object_tags'].upper())


class BulkLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / 'bulk.sqlite3')
        self.con.execute('PRAGMA foreign_keys=OFF')
        prepare_tier_c_schema(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_batch_writer_preserves_object_rtree_and_tags(self):
        batch = ImportObjectBatch(self.con, object_limit=2, tag_limit=10)
        batch.add(OSMObject(
            'way', 100, 60.0, 24.0, 59.9, 60.1, 23.9, 24.1,
            {'highway':'primary','name':'A'}, False
        ))
        batch.add(OSMObject(
            'way', 101, 61.0, 25.0, 60.9, 61.1, 24.9, 25.1,
            {'natural':'cliff'}, True
        ))
        batch.flush()
        self.con.commit()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM objects').fetchone()[0], 2)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM object_rtree').fetchone()[0], 2)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM object_tags').fetchone()[0], 2)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM packed_object_tags').fetchone()[0], 0)
        finalize_tier_c_tags(self.con)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM packed_object_tags').fetchone()[0], 2)
        self.assertEqual(
            self.con.execute("SELECT value FROM object_tags t JOIN objects o ON o.pk=t.object_pk WHERE o.osm_id=100 AND t.key='highway'").fetchone()[0],
            'primary'
        )

    def test_deferred_indexes_are_recreated(self):
        _drop_bulk_load_indexes(self.con)
        names = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertNotIn('idx_tags_key_value', names)
        self.assertNotIn('idx_tags_key', names)
        self.assertNotIn('idx_relation_members_member', names)
        _create_bulk_load_indexes(self.con)
        names = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn('idx_objects_osm_id', names)
        self.assertIn('idx_tags_key_value', names)
        self.assertIn('idx_tags_key', names)
        self.assertIn('idx_relation_members_member', names)


class PackedTopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / 'packed.sqlite3')

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_varint_roundtrip_preserves_order_and_large_ids(self):
        ids = [1, 2, 1, 9_876_543_210, 9_876_543_200, 42, 43]
        blob = encode_way_node_ids(ids)
        self.assertEqual(decode_way_node_ids(blob), ids)
        self.assertLess(len(blob), len(ids) * 8)

    def test_get_way_node_ids_prefers_packed_and_falls_back_to_legacy(self):
        insert_way_topology_rows(self.con, [(100, encode_way_node_ids([7, 8, 9]))])
        self.con.executemany(
            'INSERT INTO way_nodes(way_id,node_id,seq) VALUES(?,?,?)',
            [(101, 11, 0), (101, 12, 1)],
        )
        self.con.commit()
        self.assertEqual(get_way_node_ids(self.con, 100), [7, 8, 9])
        self.assertEqual(get_way_node_ids(self.con, 101), [11, 12])


class PackedGeometryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / 'geometry.sqlite3')

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_geometry_roundtrip_preserves_ids_coordinates_and_missing_values(self):
        nodes = [
            (9_876_543_210, 60.1234567, 24.7654321),
            (9_876_543_200, 60.1234570, 24.7654310),
            (42, None, None),
            (43, -33.1234567, 151.1234567),
        ]
        blob = encode_way_geometry(nodes)
        self.assertEqual(decode_way_geometry(blob), nodes)

    def test_geometry_id_only_decoder_matches_full_decoder(self):
        blob = encode_way_geometry([
            (100, 60.0, 24.0),
            (101, 60.0000001, 24.0000002),
            (99, None, None),
            (5_000_000_000, 61.5, 25.5),
        ])
        self.assertEqual(
            decode_way_geometry_node_ids(blob),
            [row[0] for row in decode_way_geometry(blob)],
        )

    def test_get_way_nodes_prefers_schema4_geometry(self):
        nodes = [(7, 60.0, 24.0), (8, 60.0000001, 24.0000002)]
        insert_way_geometry_rows(self.con, [(100, encode_way_geometry(nodes))])
        self.con.commit()
        self.assertEqual(get_way_node_ids(self.con, 100), [7, 8])
        self.assertEqual(get_way_nodes(self.con, 100), nodes)

    def test_schema5_split_geometry_roundtrip_and_preference(self):
        nodes = [
            (7, 60.0, 24.0),
            (8, 60.0000001, 24.0000002),
            (9, None, None),
            (10, 61.0, 25.0),
        ]
        node_ids, coordinates = encode_way_geometry_v5(nodes)
        self.assertEqual(decode_way_geometry_v5(node_ids, coordinates), nodes)
        insert_way_geometry_v5_rows(self.con, [(101, node_ids, coordinates)])
        # Deliberately add conflicting schema-4 topology for the same way; v5
        # must win when both are present.
        insert_way_geometry_rows(
            self.con,
            [(101, encode_way_geometry([(999, 1.0, 1.0)]))],
        )
        self.con.commit()
        self.assertEqual(get_way_node_ids(self.con, 101), [7, 8, 9, 10])
        self.assertEqual(get_way_nodes(self.con, 101), nodes)


class MultiprocessingPreparationTests(unittest.TestCase):
    def test_prepare_way_batch_preserves_topology_bounds_tags_and_counts(self):
        records = [
            (
                100,
                ((10, 60.0, 24.0), (11, 61.0, 25.0), (10, 60.0, 24.0)),
                (("highway", "primary"), ("name", "A")),
                True,
                True,
            ),
            (
                101,
                ((20, 62.0, 26.0), (21, 62.5, 26.5)),
                (("railway", "rail"),),
                False,
                False,
            ),
        ]
        prepared, coords, missing, retained, deps, node_refs, worker_s = _prepare_way_batch(records)
        self.assertEqual(missing, 0)
        self.assertEqual(retained, 1)
        self.assertEqual(deps, 1)
        self.assertEqual(node_refs, 5)
        self.assertGreaterEqual(worker_s, 0.0)
        self.assertEqual(len(prepared), 2)
        self.assertEqual(
            [nid for nid, _lat, _lon in decode_way_geometry_v5(prepared[0][9], prepared[0][10])],
            [10, 11, 10],
        )
        decoded = decode_way_geometry_v5(prepared[0][9], prepared[0][10])
        self.assertEqual(decoded[0], (10, 60.0, 24.0))
        self.assertEqual(decoded[1], (11, 61.0, 25.0))
        self.assertEqual(prepared[0][1:7], (60.5, 24.5, 60.0, 61.0, 24.0, 25.0))
        self.assertEqual(dict(prepared[0][8]), {"highway": "primary", "name": "A"})
        self.assertEqual(coords, [])

    def test_prepare_way_batch_reports_missing_geometry(self):
        records = [(200, ((1, None, None), (2, None, None)), (("highway", "service"),), False, True)]
        prepared, coords, missing, retained, deps, node_refs, _worker_s = _prepare_way_batch(records)
        self.assertEqual(prepared, [])
        self.assertEqual(coords, [])
        self.assertEqual(missing, 1)
        self.assertEqual(retained, 0)
        self.assertEqual(deps, 0)
        self.assertEqual(node_refs, 0)

    def test_worker_count_parser(self):
        self.assertEqual(_resolve_worker_count("0"), 0)
        self.assertEqual(_resolve_worker_count("serial"), 0)
        self.assertEqual(_resolve_worker_count("3"), 3)
        self.assertGreaterEqual(_resolve_worker_count("auto"), 1)


class ConcurrentTopologyWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / 'writer.sqlite3')
        self.con.execute('PRAGMA foreign_keys=OFF')
        prepare_tier_c_schema(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_writer_thread_batches_sqlite_writes(self):
        stats = ImportStats()
        writer = _TopologyWriter(self.con, stats, queue_depth=2)
        writer.start()
        rows = [
            (
                300,
                60.0,
                24.0,
                59.9,
                60.1,
                23.9,
                24.1,
                True,
                (("highway", "primary"),),
                encode_way_geometry_v5([(1, 60.0, 24.0), (2, 60.1, 24.1)])[0],
                encode_way_geometry_v5([(1, 60.0, 24.0), (2, 60.1, 24.1)])[1],
                True,
                2,
            ),
        ]
        writer.submit((rows, [], 0, 1, 0, 2, 0.01))
        writer.finish()
        self.con.commit()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM objects WHERE osm_type='way'").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM way_geometry_v5").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM packed_object_tags").fetchone()[0], 0)
        finalize_tier_c_tags(self.con)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM packed_object_tags").fetchone()[0], 1)
        self.assertEqual(stats.retained_ways, 1)
        self.assertEqual(stats.dependency_node_refs, 2)


if __name__ == '__main__':
    unittest.main()
