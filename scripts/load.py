"""Generate load against a lab database so consecutive snapshots differ.

  uv run scripts/load.py postgres --seconds 20            # normal mix
  uv run scripts/load.py postgres --seconds 20 --regress  # adds an unindexed scan + idle-in-transaction holder
  uv run scripts/load.py oracle --seconds 20 --awr-snapshot   # also takes an AWR snapshot afterwards

Statements use bind variables so the engines aggregate them (one sql_id / queryid / digest each).
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import threading
import time

PG = os.environ.get("PG_APP_DSN", "postgresql://postgres:postgres@localhost:5432/app")   # app owner, not the collector role
MY = os.environ.get("MYSQL_APP_DSN", "mysql://root:root@localhost:3306/app")
ORA = os.environ.get("ORACLE_APP_DSN", "app/app@localhost:1521/FREEPDB1")

# {c} customer id, {i} order id, {a} amount, {now} current timestamp expression
NORMAL = [
    ("select * from orders where customer_id = {c}", 5),
    ("select status, count(*) from orders where customer_id = {c} group by status", 2),
    ("update orders set amount = amount + 1 where id = {i}", 2),
    ("insert into orders (customer_id, status, amount, created_at, note) values ({c}, 'new', 9.99, {now}, 'load')", 1),
]
REGRESS = [("select count(*) from orders where note like '%7a%' and amount > {a}", 3)]
STYLE = {"postgres": "%({0})s", "mysql": "%({0})s", "oracle": ":{0}"}
NOW = {"postgres": "now()", "mysql": "now()", "oracle": "systimestamp"}


def prepare(engine, q):
    q = q.replace("{now}", NOW[engine])
    if STYLE[engine].startswith("%"):  # psycopg and pymysql read a bare % (the LIKE '%7a%') as a placeholder
        q = q.replace("%", "%%")
    return q.format(**{k: STYLE[engine].format(k) for k in ("c", "i", "a")})


def connect(engine):
    if engine == "postgres":
        import psycopg
        c = psycopg.connect(PG, autocommit=True)
        return c, c.cursor()
    if engine == "mysql":
        import pymysql
        from urllib.parse import urlparse
        u = urlparse(MY)
        c = pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username, password=u.password, database=u.path[1:], autocommit=True)
        return c, c.cursor()
    import oracledb
    c = oracledb.connect(dsn=ORA)
    c.autocommit = True
    c.module = "lab-load"  # not "dbtriage*": the collector filters its own module out
    return c, c.cursor()


ERRORS: list[str] = []  # one per load thread that died, so main() can fail instead of reporting a load that never ran


def worker(engine, stop, queries):
    c = None
    try:
        c, cur = connect(engine)  # inside the try: an unreachable database is a failed run too
        while time.time() < stop:
            q, w = random.choice(queries)
            if random.random() > w / 5:
                continue
            params = {"c": random.randint(1, 5000), "i": random.randint(1, 100000), "a": random.randint(1, 400)}
            cur.execute(prepare(engine, q), {k: v for k, v in params.items() if "{" + k + "}" in q})
    except Exception as exc:  # noqa: BLE001 - reported once by main()
        ERRORS.append(f"{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        if c is not None:
            c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("engine", choices=["postgres", "mysql", "oracle"])
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--regress", action="store_true")
    ap.add_argument("--awr-snapshot", action="store_true", help="oracle: take an AWR snapshot after the load")
    a = ap.parse_args()
    try:  # read the repo's .env the way dbtriage does, so ORACLE_DSN etc. are there without sourcing it
        from pathlib import Path

        from dbtriage import envfile

        envfile.init(Path(__file__).resolve().parents[1] / "targets.toml")
    except ImportError:
        pass
    queries = NORMAL + (REGRESS * 2 if a.regress else [])
    stop = time.time() + a.seconds
    threads = [threading.Thread(target=worker, args=(a.engine, stop, queries)) for _ in range(a.threads)]
    holder = None
    if a.regress and a.engine == "postgres":  # an idle-in-transaction session holding a row lock
        import psycopg
        holder = psycopg.connect(PG)
        holder.execute("update orders set note = 'held' where id = 1")
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if holder:
        holder.rollback(); holder.close()
    if ERRORS:
        sys.exit(f"{a.engine}: load failed in {len(ERRORS)} of {a.threads} threads: {ERRORS[0]}")
    print(f"{a.engine}: load done ({a.seconds}s, {a.threads} threads{', regressed' if a.regress else ''})")
    if a.awr_snapshot:
        import oracledb
        c = oracledb.connect(dsn=os.environ["ORACLE_DSN"])
        c.module = "lab-snapshot"
        c.cursor().callproc("dbms_workload_repository.create_snapshot")
        print("oracle: AWR snapshot taken")


if __name__ == "__main__":
    main()
