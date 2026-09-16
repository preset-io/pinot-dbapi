// Internal publication path for pinotdb.
//
// Publishes to the Preset internal package bucket (s3://preset-pypi), which is
// served read-only behind nginx. The public base URL is deliberately NOT
// written here: this repository is public, and the host is internal-only.
// Consumers pin the immutable artifact URL directly; that URL is recorded in
// the internal tracking for this change, not in this repo.
//
// Modelled on the existing Preset publishers (api-clients, service-lib-py,
// claude-commands). No credential material lives in this file: the AWS keys
// are bound at runtime by Jenkins from the 'ci-user' credential and are never
// echoed.
//
// Versioning follows the four-component convention already used in this
// bucket (upstream PyHive 0.7.0 -> Preset pyhive-0.7.0.1). Here that makes the
// Preset build of upstream 9.1.2 into 9.1.2.1, declared explicitly in
// pyproject.toml so the published version is auditable in git rather than
// synthesised at build time.
//
//   * master      -> bare 9.1.2.1, guarded by an existence check so a
//     published artifact is never overwritten.
//   * PR branches -> 9.1.2.1+PR-<n>.<shortsha> (PEP 440 local version).
//
// Note 9.1.2.1 sorts ABOVE upstream 9.1.2, so it must never be offered to a
// dependency resolver as a candidate for the plain `pinotdb` name. Pinning the
// immutable artifact URL keeps resolution fixed to an exact file and leaves any
// future upstream release free to win.
//
// Unlike the Drill publisher, the "already published?" probe reads s3:// rather
// than fetching the public URL. That keeps the internal host out of this file
// and checks the source of truth instead of whatever nginx happens to serve.

// Bucket directory and distribution name. For pinotdb these coincide, but both
// are spelled out because the overwrite guard below depends on the exact
// artifact filename.
LIB_NAME = 'pinotdb'
DIST_NAME = 'pinotdb'
String currentVersion = ""

podTemplate(
    imagePullSecrets: ['preset-pull'],
    nodeUsageMode: 'NORMAL',
    containers: [
        containerTemplate(
            alwaysPullImage: true,
            name: 'ci',
            image: 'preset/ci:latest',
            ttyEnabled: true,
            command: 'cat',
            resourceRequestCpu: '100m',
            resourceLimitCpu: '200m',
            resourceRequestMemory: '1000Mi',
            resourceLimitMemory: '2000Mi',
        ),
        containerTemplate(
            alwaysPullImage: true,
            // pinotdb requires python >=3.10,<4, so the 3.9 image used by the
            // older publishers is not usable here.
            name: 'py-ci',
            image: 'preset/python:3.11.14-2026-05-22-ci',
            ttyEnabled: true,
            command: 'cat'
        )
    ]
) {
    node(POD_LABEL) {
        def repo = checkout scm
        def shortGitRev = sh(
                returnStdout: true,
                script: 'git rev-parse --short HEAD'
        ).trim()

        container('py-ci') {
            stage('Read version') {
                // pyproject.toml is the single source of truth. Read it without
                // importing or building so a broken build cannot fake it.
                currentVersion = sh(
                        script: "grep '^version' pyproject.toml | head -1 | cut -d'\"' -f2",
                        returnStdout: true,
                        label: 'Get current version'
                ).trim()
                if (!currentVersion) {
                    error('Could not read version from pyproject.toml')
                }
                echo "Declared version: ${currentVersion}"
            }
        }

        container('ci') {
            stage('Check not already published') {
                if (env.BRANCH_NAME == 'master') {
                    withCredentials([
                        [
                            $class           : 'AmazonWebServicesCredentialsBinding',
                            credentialsId    : 'ci-user',
                            accessKeyVariable: 'AWS_ACCESS_KEY_ID',
                            secretKeyVariable: 'AWS_SECRET_ACCESS_KEY',
                        ]
                    ]) {
                        def retVal = sh(
                                script: "aws s3 ls s3://preset-pypi/${LIB_NAME}/${DIST_NAME}-${currentVersion}.tar.gz",
                                returnStatus: true,
                                label: 'Check for existing tarball'
                        )
                        // If the artifact exists we bail: published files are
                        // immutable and must never be overwritten in place.
                        if (retVal == 0) {
                            error("Version ${currentVersion} of ${LIB_NAME} already exists! Version bump required.")
                        }
                    }
                }
            }
        }

        container('py-ci') {
            stage('Tests') {
                sh(script: 'python -m pip install --upgrade pip', label: 'Upgrade pip')
                sh(script: 'python -m pip install poetry', label: 'Install poetry')
                sh(script: 'poetry install --all-extras --with=dev', label: 'Install dependencies')
                // Unit tests only. The integration suite needs a live Pinot
                // quickstart container and is covered by the GitHub Actions
                // matrix on this repo.
                sh(script: 'poetry run pytest -o addopts= tests/unit', label: 'Unit tests')
            }

            stage('Package Release') {
                if (env.BRANCH_NAME.startsWith("PR-")) {
                    def pullRequestVersion = "${currentVersion}+${env.BRANCH_NAME}.${shortGitRev}"
                    sh(
                        script: "sed -i \"s/^version = \\\"${currentVersion}\\\"/version = \\\"${pullRequestVersion}\\\"/\" pyproject.toml",
                        label: 'Changing version for PR'
                    )
                    sh(script: "echo PR version: ${pullRequestVersion}", label: 'PR Release candidate version')
                }
                sh(script: 'python -m pip install build && python -m build', label: 'Bundling release')

                // Fail closed: the filename version, the sdist metadata version
                // and the declared version must all agree, and the artifacts
                // must not ship the top-level tests package.
                sh(
                    script: '''
                        set -eu
                        EXPECTED="$(grep '^version' pyproject.toml | head -1 | cut -d'"' -f2)"
                        test -f "dist/pinotdb-${EXPECTED}.tar.gz" \
                            || { echo "missing sdist for ${EXPECTED}"; ls -1 dist; exit 1; }
                        test -f "dist/pinotdb-${EXPECTED}-py3-none-any.whl" \
                            || { echo "missing wheel for ${EXPECTED}"; ls -1 dist; exit 1; }
                        META="$(tar -xzOf "dist/pinotdb-${EXPECTED}.tar.gz" \
                            "pinotdb-${EXPECTED}/PKG-INFO" | awk '/^Version:/{print $2; exit}')"
                        test "$META" = "$EXPECTED" \
                            || { echo "metadata version ${META} != ${EXPECTED}"; exit 1; }
                        if tar -tzf "dist/pinotdb-${EXPECTED}.tar.gz" \
                            | grep -qE "^pinotdb-${EXPECTED}/tests/"; then
                            echo "sdist must not ship the tests package"; exit 1
                        fi
                        echo "artifact check OK: ${EXPECTED}"
                        sha256sum dist/*.tar.gz dist/*.whl
                    ''',
                    label: 'Verify artifact contents'
                )

                sh(script: "mkdir -p dist/${LIB_NAME} && mv dist/*.gz dist/*.whl dist/${LIB_NAME}", label: 'Setup release folder')
            }
        }

        container('ci') {
            stage('Upload Release') {
                withCredentials([
                    [
                        $class           : 'AmazonWebServicesCredentialsBinding',
                        credentialsId    : 'ci-user',
                        accessKeyVariable: 'AWS_ACCESS_KEY_ID',
                        secretKeyVariable: 'AWS_SECRET_ACCESS_KEY',
                    ]
                ]) {
                    if ((env.BRANCH_NAME == 'master') || (env.BRANCH_NAME.startsWith("PR-"))) {
                        sh(script: "aws s3 sync ./dist s3://preset-pypi", label: "Uploading to s3")
                        echo "✅ Published ${LIB_NAME} ${currentVersion}"
                    }
                    else {
                        echo "Skipping upload as this isn't master..."
                    }
                }
            }
        }
    }
}
