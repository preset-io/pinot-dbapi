#!/usr/bin/env groovy

// PR / branch validation for preset-io/pinot-dbapi.
//
// Runs the offline unit suite (tests/unit) only. tests/integration needs a live
// Pinot cluster (see the `check-pinot` target in the Makefile) and stays out of
// this pipeline.
//
// This pipeline intentionally publishes nothing: no sdist, no wheel, no
// registry upload, no git tag, no credentials.

properties([
    [$class: 'BuildDiscarderProperty',
     strategy: [$class: 'LogRotator', numToKeepStr: '20']],
])

podTemplate(
    imagePullSecrets: ['preset-pull'],
    nodeUsageMode: 'NORMAL',
    containers: [
        containerTemplate(
            alwaysPullImage: true,
            name: 'py-ci',
            image: 'preset/python:3.11.14-2026-05-22-ci',
            ttyEnabled: true,
            command: 'cat',
            resourceRequestCpu: '500m',
            resourceLimitCpu: '2000m',
            resourceRequestMemory: '1000Mi',
            resourceLimitMemory: '2000Mi',
        ),
    ]
) {
    node(POD_LABEL) {
        container('py-ci') {
            stage('Checkout') {
                checkout scm
            }

            stage('Install') {
                sh(script: 'python -m pip install --upgrade pip', label: 'Upgrade pip')
                sh(
                    script: 'pip install -e ".[sqlalchemy]"',
                    label: 'Install pinotdb with the sqlalchemy extra',
                )
                sh(
                    script: 'pip install pytest pytest-cov mock responses parameterized filelock',
                    label: 'Install test dependencies',
                )
            }

            stage('Unit Tests') {
                try {
                    sh(
                        script: 'pytest tests/unit --junitxml=junit-py.xml',
                        label: 'Unit tests',
                    )
                } finally {
                    junit allowEmptyResults: false, testResults: 'junit-py.xml'
                }
            }

            stage('Dialect Entry Points') {
                sh(
                    script: '''#!/usr/bin/env bash
set -eo pipefail
python - <<'PY'
from sqlalchemy.dialects import registry
from sqlalchemy.engine.default import DefaultDialect

EXPECTED = [
    "pinot",
    "pinot.http",
    "pinot.https",
    "pinot.async",
    "pinot.rest_async",
    "pinot.http_async",
]
failures = []
for name in EXPECTED:
    try:
        dialect = registry.load(name)
    except Exception as exc:
        failures.append(f"{name}: {type(exc).__name__}: {exc}")
        continue
    if not (isinstance(dialect, type) and issubclass(dialect, DefaultDialect)):
        failures.append(f"{name}: resolved to {dialect!r}, not a Dialect class")
        continue
    print(f"OK   {name} -> {dialect.__module__}.{dialect.__name__}")

if failures:
    for line in failures:
        print(f"FAIL {line}")
    raise SystemExit("sqlalchemy.dialects entry points are broken")
print("All sqlalchemy.dialects entry points resolve to Dialect classes")
PY
''',
                    label: 'sqlalchemy.dialects entry points',
                )
            }
        }
    }
}
