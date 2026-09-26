"""HTTP error responses surface as the right DB-API errors."""
from unittest.mock import MagicMock

import httpx
import pytest
import responses
from sqlalchemy.exc import NoSuchTableError

from pinotdb import exceptions
from pinotdb.db import Cursor
from pinotdb.sqlalchemy import PinotDialect

CONTROLLER = 'http://controller.test:9000/'


def broker_error(status, body):
    cursor = Cursor(host='localhost', session=MagicMock(spec=httpx.Client))
    response = httpx.Response(status, json=body)
    return cursor.normalize_query_response('SELECT 1', response)


@pytest.mark.parametrize('status', [401, 403])
def test_broker_auth_failure_is_operational_error_not_timeout(status):
    # A native broker auth failure carries no server counts; it used to be
    # reported as "timed out: Out of -1, only -1 responded".
    with pytest.raises(exceptions.OperationalError) as info:
        broker_error(status, {'code': status, 'error': 'Unauthorized'})
    assert 'timed out' not in str(info.value)
    assert str(status) in str(info.value)


def test_other_broker_http_errors_stay_programming_errors():
    with pytest.raises(exceptions.ProgrammingError) as info:
        broker_error(500, {'code': 500, 'error': 'boom'})
    assert 'timed out' not in str(info.value)


def dialect():
    value = PinotDialect(server=CONTROLLER)
    return value


@responses.activate
@pytest.mark.parametrize('status', [401, 403])
def test_controller_auth_failure_is_not_an_empty_catalog(status):
    responses.add(responses.GET, CONTROLLER + 'tables',
                  json={'code': status, 'error': 'HTTP 401 Unauthorized'},
                  status=status)
    # Previously parsed as metadata: no tables, so has_table() was False and
    # a credentials problem looked like a missing table.
    with pytest.raises(exceptions.OperationalError):
        dialect().get_table_names(None)
    with pytest.raises(exceptions.OperationalError):
        dialect().has_table(None, 'resultTypes')


@responses.activate
def test_controller_missing_schema_is_no_such_table():
    responses.add(responses.GET, CONTROLLER + 'tables/missing/schema',
                  json={'code': 404, 'error': 'not found'}, status=404)
    with pytest.raises(NoSuchTableError):
        dialect().get_columns(None, 'missing')


@responses.activate
def test_controller_server_error_is_database_error():
    responses.add(responses.GET, CONTROLLER + 'tables/t/schema',
                  json={'code': 500, 'error': 'boom'}, status=500)
    with pytest.raises(exceptions.DatabaseError) as info:
        dialect().get_columns(None, 't')
    assert info.value.status_code == 500
