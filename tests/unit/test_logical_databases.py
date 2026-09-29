"""Pinot logical databases: listing, metadata routing and query routing."""
from unittest.mock import MagicMock, patch

import httpx
import pytest
import requests
import responses
from sqlalchemy.exc import CompileError
from sqlalchemy import (
    Column, Integer, MetaData, Table, create_engine, select, text,
)

from pinotdb import exceptions
from pinotdb.db import Cursor
from pinotdb.sqlalchemy import PinotDialect

CONTROLLER = 'http://controller.test:9000/'


def dialect(**kwargs):
    return PinotDialect(server=CONTROLLER, **kwargs)


def sent_database(call):
    return call.request.headers.get('Database')


@responses.activate
def test_schema_names_come_from_the_controller():
    responses.get(CONTROLLER + 'databases', json=['db3', 'default', 'db2'])
    assert dialect().get_schema_names(None) == ['default', 'db2', 'db3']
    assert sent_database(responses.calls[0]) is None


@responses.activate
def test_schema_names_fall_back_on_servers_without_databases():
    responses.get(CONTROLLER + 'databases', status=404, json={'code': 404})
    assert dialect().get_schema_names(None) == ['default']


@responses.activate
@pytest.mark.parametrize('status', [401, 403, 405, 500, 503])
def test_schema_names_fall_back_when_the_controller_refuses(status):
    # Schema pickers must keep working for principals without cluster-level
    # database access and when the controller fails.
    responses.get(CONTROLLER + 'databases', status=status,
                  json={'code': status})
    assert dialect().get_schema_names(None) == ['default']


@responses.activate
def test_schema_names_fall_back_when_the_controller_is_unreachable():
    responses.get(CONTROLLER + 'databases',
                  body=requests.exceptions.ConnectionError('refused'))
    assert dialect().get_schema_names(None) == ['default']


def test_schema_names_without_a_controller_make_no_call():
    assert PinotDialect().get_schema_names(None) == ['default']


@responses.activate
@pytest.mark.parametrize('body', [{'unexpected': True}, ['db2', 3]])
def test_malformed_schema_names_are_not_hidden(body):
    responses.get(CONTROLLER + 'databases', json=body)
    with pytest.raises(exceptions.DatabaseError):
        dialect().get_schema_names(None)


def test_database_option_is_an_explicit_single_database_mode():
    value = dialect()
    value._database = 'db2'
    assert value.get_schema_names(None) == ['db2']  # no controller call


@responses.activate
@pytest.mark.parametrize('configured,schema,header', [
    (None, None, None), (None, 'default', None), (None, 'db2', 'db2'),
    ('db2', None, 'db2'), ('db2', 'db2', 'db2'), ('db2', 'default', 'default'),
])
def test_metadata_calls_query_the_requested_database(
    configured, schema, header,
):
    responses.get(CONTROLLER + 'tables', json={'tables': ['db2.rt']})
    responses.get(CONTROLLER + 'tables/rt/schema', json={
        'dimensionFieldSpecs': [{'name': 'id', 'dataType': 'INT'}]})
    value = dialect()
    value._database = configured
    assert value.get_table_names(None, schema=schema) == ['rt']
    assert value.has_table(None, 'rt', schema=schema)
    assert [c['name'] for c in value.get_columns(None, 'rt', schema=schema)
            ] == ['id']
    assert {sent_database(c) for c in responses.calls} == {header}


def table(schema):
    return Table('rt', MetaData(), Column('id', Integer), schema=schema)


@pytest.mark.parametrize('configured,schema,sql,routed', [
    (None, None, 'FROM rt', None),
    (None, 'default', 'FROM rt', None),
    (None, 'db2', 'FROM db2.rt', 'db2'),
    ('db2', 'db2', 'FROM rt', None),
    ('db2', 'default', 'FROM "default".rt', 'default'),
])
def test_compiled_tables_are_qualified_and_routed(
    configured, schema, sql, routed,
):
    value = dialect()
    value._database = configured
    t = table(schema)
    compiled = select(t.c.id).where(t.c.id > 1).compile(dialect=value)
    assert sql in str(compiled)
    # Columns are table-qualified only.
    assert 'rt.id' in str(compiled) and '.rt.id' not in str(compiled)
    assert compiled.pinot_database == routed


@pytest.mark.parametrize('first,second', [
    ('db2', 'db3'), ('db2', None), (None, 'db2')])
def test_statements_mixing_databases_do_not_compile(first, second):
    a, b = table(first), table(second)
    with pytest.raises(CompileError, match='more than one logical database'):
        select(a.c.id, b.c.id).compile(dialect=dialect())


def run(statement, url_query=''):
    engine = create_engine(
        f'pinot://localhost:8000/query/sql?controller={CONTROLLER}{url_query}')
    calls = []

    def fake_execute(self, operation, parameters=None, *args, **kwargs):
        calls.append(kwargs.get('database'))
        self.description = [('id', None, None, None, None, None, None)]
        self._results = []
        return self

    with patch.object(Cursor, 'execute', fake_execute):
        with engine.connect() as connection:
            connection.execute(statement).all()
    engine.dispose()
    return calls


def test_execution_routes_to_the_statements_database():
    db2 = table('db2')
    assert run(select(db2.c.id)) == ['db2']
    assert run(select(db2.c.id).where(db2.c.id == 1)) == ['db2']
    assert run(select(table(None).c.id)) == [None]
    assert run(text('SELECT 1 FROM rt')) == [None]
    assert run(select(table('db2').c.id), '&database=db2') == [None]


def test_request_database_header_overrides_the_connection_option():
    session = MagicMock(spec=httpx.Client)
    session.headers = httpx.Headers()
    session.post.return_value = httpx.Response(200, json={
        'resultTable': {'dataSchema': {'columnNames': ['id'],
                                       'columnDataTypes': ['INT']},
                        'rows': [[1]]},
        'numServersQueried': 1, 'numServersResponded': 1})
    cursor = Cursor(host='localhost', session=session, database='db3')
    assert session.headers['database'] == 'db3'
    cursor.execute('SELECT id FROM rt', database='db2')
    assert session.post.call_args.kwargs['headers']['database'] == 'db2'
    cursor.execute('SELECT id FROM rt')
    assert 'database' not in session.post.call_args.kwargs['headers']
