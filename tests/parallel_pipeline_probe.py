from __future__ import annotations
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
import tempfile
from pathlib import Path
from local_osm.db import connect, get_way_node_ids
from local_osm.importer import ImportStats, _TopologyWriter, _prepare_way_batch


def main():
    records=[]
    for i in range(20):
        oid=1000+i
        records.append((oid, ((oid*10,60+i*0.001,24.0),(oid*10+1,60+i*0.001,24.1)), (("highway","service"),), False, True))
    batches=[records[:10], records[10:]]
    with tempfile.TemporaryDirectory() as td:
        con=connect(Path(td)/'p.sqlite3')
        stats=ImportStats()
        writer=_TopologyWriter(con, stats, queue_depth=2)
        writer.start()
        with ProcessPoolExecutor(max_workers=2, mp_context=mp.get_context('spawn')) as ex:
            futs=[ex.submit(_prepare_way_batch,b) for b in batches]
            for fut in as_completed(futs):
                writer.submit(fut.result())
        writer.finish()
        assert con.execute("SELECT COUNT(*) FROM objects WHERE osm_type='way'").fetchone()[0] == 20
        assert con.execute("SELECT COUNT(*) FROM way_geometry_v5").fetchone()[0] == 20
        assert get_way_node_ids(con, 1000) == [10000,10001]
        assert con.execute("SELECT COUNT(*) FROM node_coords").fetchone()[0] == 0
        assert stats.retained_ways == 20
        con.close()
    print('parallel pipeline probe ok')

if __name__=='__main__':
    main()
