import os
import sys

import adbc_driver_gizmosql.dbapi as gizmosql
import pyarrow as pa

from maritime_tip_cue.config import ROOT, env, settings


def connect():
    cfg = settings()["ais_archive"]
    return gizmosql.connect(
        env("GIZMOSQL_URI"),
        username=os.environ.get("GIZMOSQL_USER") or cfg["default_user"],
        password=env("GIZMOSQL_PASSWORD"),
        db_kwargs={"adbc.flight.sql.client_option.tls_root_certs": (ROOT / cfg["tls_root_cert"]).read_text(encoding="ascii")},
    )


def fetch(sql: str) -> pa.Table:
    with connect() as con, con.cursor() as cur:
        cur.execute(sql)
        return cur.fetch_arrow_table()


if __name__ == "__main__":
    print(fetch(sys.argv[1]).to_pandas().to_string(max_rows=40) if len(sys.argv) > 1 else "usage: python -m maritime_tip_cue.ais.archive \"<SQL>\"")
