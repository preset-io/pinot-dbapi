# Reflected result-type regression fixture

Use a disposable local batch QuickStart, never a shared database. This fixture
adds a table containing an exact 50-digit decimal, a tiny negative decimal,
binary bytes, empty bytes, native nulls, and integer/string multi-values.

```sh
docker run -d --name pinot-result-types \
  -p 127.0.0.1::8000 -p 127.0.0.1::9000 \
  -e JAVA_OPTS='-Xms512m -Xmx3g -XX:ActiveProcessorCount=4' \
  apachepinot/pinot@sha256:3097ab095157757ca16c8a869066e036891517b82c534b6ae67bd4f2c078c6a5 \
  QuickStart -type batch
docker port pinot-result-types
# Wait for billing, starbucksStores and airlineStats to be queryable.
docker cp tests/integration/fixtures/result_types pinot-result-types:/tmp/result-types
docker exec pinot-result-types /opt/pinot/bin/pinot-admin.sh AddTable \
  -schemaFile /tmp/result-types/schema.json \
  -tableConfigFile /tmp/result-types/table.json -exec
docker exec pinot-result-types /opt/pinot/bin/pinot-admin.sh LaunchDataIngestionJob \
  -jobSpecFile /tmp/result-types/job.yaml
# Wait for SELECT COUNT(*) FROM resultTypes to return 3.
PINOT_RESULT_TYPES=1 PINOT_HOST=127.0.0.1 \
  PINOT_BROKER_PORT=<mapped-8000-port> PINOT_CONTROLLER_PORT=<mapped-9000-port> \
  python -m pytest -q tests/integration/test_result_types.py
docker rm -fv pinot-result-types
docker ps --filter name=pinot-result-types
```

The integration module is opt-in because the normal test service need not have
these extra datasets. With the flag set, absent fixtures fail rather than skip.
Both URL aliases and both reflection schema controls run. The broker's raw JSON
is the independent oracle for bundled fixtures; explicit expected values are
the oracle for the custom fixture. `enableNullHandling=true` and the table's
null index distinguish true nulls from default sentinels.
