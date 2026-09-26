# Logical database fixture

Two Pinot logical databases, `db2` and `db3`, each with a table named `rt`
(the `resultTypes` schema) holding different rows. Use a disposable local batch
QuickStart (see `../result_types/README.md`), never a shared cluster.

```sh
docker cp tests/integration/fixtures/logical_databases pinot-result-types:/tmp/logical-databases
for db in db2 db3; do
  curl -X POST -H 'Content-Type: application/json' -H "Database: $db" \
    -d @tests/integration/fixtures/logical_databases/$db/schema.json \
    http://127.0.0.1:<9000-port>/schemas
  curl -X POST -H 'Content-Type: application/json' -H "Database: $db" \
    -d @tests/integration/fixtures/logical_databases/$db/table.json \
    http://127.0.0.1:<9000-port>/tables
  docker exec pinot-result-types /opt/pinot/bin/pinot-admin.sh \
    LaunchDataIngestionJob -jobSpecFile /tmp/logical-databases/$db/job.yaml
done
# Wait until GET /databases lists db2 and db3 and both tables return rows.
PINOT_LOGICAL_DATABASES=1 PINOT_HOST=127.0.0.1 \
  PINOT_BROKER_PORT=<8000-port> PINOT_CONTROLLER_PORT=<9000-port> \
  python -m pytest -q tests/integration/test_logical_databases.py
```

The single-stage engine resolves `db2.rt` without a `Database` header; the
multi-stage engine resolves a table only in the database named by the
request's `Database` header. The dialect therefore qualifies the table and
routes the request.
