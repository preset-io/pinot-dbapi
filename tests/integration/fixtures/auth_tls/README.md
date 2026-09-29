# Native TLS and basic-auth fixture

Disposable local cluster only. `tls.properties` adds HTTPS listeners (8443
broker, 9443 controller) next to the plain HTTP ones to Pinot's AUTH
QuickStart, whose built-in demo principal is `admin`/`verysecret`. Generate a
throwaway CA and a server certificate with `subjectAltName=IP:127.0.0.1`, pack
it as `keystore.p12` (password `changeit`), and trust the CA in the client.

```sh
docker create --name pinot-auth-tls \
  -p 127.0.0.1::8000 -p 127.0.0.1::8443 -p 127.0.0.1::9443 \
  -e JAVA_OPTS='-Xms512m -Xmx3g -XX:ActiveProcessorCount=4' \
  apachepinot/pinot@sha256:3097ab095157757ca16c8a869066e036891517b82c534b6ae67bd4f2c078c6a5 \
  QuickStart -type AUTH -configFile /tmp/tls/tls.properties
mkdir -p tls && cp tests/integration/fixtures/auth_tls/tls.properties keystore.p12 tls/
docker cp tls pinot-auth-tls:/tmp/tls
docker cp tests/integration/fixtures/result_types pinot-auth-tls:/tmp/result-types
docker start pinot-auth-tls
# Once HTTPS queries succeed, add resultTypes with -user admin -password
# verysecret, and append `authToken: Basic YWRtaW46dmVyeXNlY3JldA==` to a copy
# of job.yaml before LaunchDataIngestionJob.
SSL_CERT_FILE=ca.pem REQUESTS_CA_BUNDLE=ca.pem PINOT_AUTH_TLS=1 \
  PINOT_BROKER_PORT=<8000> PINOT_BROKER_HTTPS_PORT=<8443> \
  PINOT_CONTROLLER_HTTPS_PORT=<9443> \
  python -m pytest -q tests/integration/test_auth_tls.py
docker rm -fv pinot-auth-tls
```

If `SSL_CERT_FILE` is not honoured by your HTTP client, append the CA to the
environment's certifi bundle instead.
