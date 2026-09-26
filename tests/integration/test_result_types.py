"""Real batch QuickStart plus the resultTypes precision/null fixture."""
import os
from decimal import Decimal

import pytest
import requests
from sqlalchemy import MetaData, Table, create_engine, select, types


pytestmark = pytest.mark.skipif(
    os.getenv('PINOT_RESULT_TYPES') != '1',
    reason='requires the batch QuickStart and resultTypes fixtures',
)


@pytest.fixture(params=['pinot', 'pinot+http'])
def engine(request):
    host = os.getenv('PINOT_HOST', 'localhost')
    broker = os.getenv('PINOT_BROKER_PORT', '8000')
    controller = os.getenv('PINOT_CONTROLLER_PORT', '9000')
    value = create_engine(
        f'{request.param}://{host}:{broker}/query/sql'
        f'?controller=http://{host}:{controller}/'
        '&query_options=enableNullHandling%3Dtrue',
    )
    try:
        yield value
    finally:
        value.dispose()


@pytest.mark.parametrize('schema', [None, 'default'])
@pytest.mark.parametrize('table_name,column_name,python_type,convert', [
    ('billing', 'overdueBalance', Decimal, Decimal),
    ('starbucksStores', 'location_st_point', bytes, bytes.fromhex),
    ('airlineStats', 'DivAirports', list, list),
    ('airlineStats', 'DivAirportIDs', list, list),
])
def test_quickstart_reflected_values(
    engine, schema, table_name, column_name, python_type, convert,
):
    table = Table(table_name, MetaData(), schema=schema, autoload_with=engine)
    statement = select(table.c[column_name]).limit(2)
    sql = str(statement.compile(
        engine, compile_kwargs={'literal_binds': True},
    ))
    # Independent wire oracle: never decode with the DBAPI under test.
    response = requests.post(
        f'http://{engine.url.host}:{engine.url.port}/query/sql',
        json={'sql': sql, 'queryOptions': 'enableNullHandling=true'},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    assert not payload.get('exceptions'), payload
    expected = [convert(row[0]) for row in payload['resultTable']['rows']]
    with engine.connect() as connection:
        actual = connection.execute(statement).scalars().all()
    assert len(actual) == 2
    assert actual == expected
    assert all(isinstance(value, python_type) for value in actual)
    if python_type is list:
        assert isinstance(table.c[column_name].type, types.ARRAY)
        item_type = (types.String if column_name == 'DivAirports'
                     else types.BigInteger)
        assert isinstance(table.c[column_name].type.item_type, item_type)


@pytest.mark.parametrize('schema', [None, 'default'])
@pytest.mark.parametrize('column_name,expected', [
    ('amount', [
        Decimal('123456789012345678901234567890.12345678901234567890'),
        None, Decimal('-0.00000000000000000001'),
    ]),
    ('payload', [b'\x00\xff\x80A', None, b'']),
    ('tags', [['SJC', 'ABQ'], ['null-row'], ['empty-bytes']]),
    ('numbers', [[1, 2], [0], [-1]]),
])
def test_exact_values_and_native_nulls(engine, schema, column_name, expected):
    table = Table('resultTypes', MetaData(), schema=schema,
                  autoload_with=engine)
    with engine.connect() as connection:
        actual = connection.execute(
            select(table.c[column_name]).order_by(table.c.id).limit(3),
        ).scalars().all()
    assert actual == expected
    assert [type(v) for v in actual] == [type(v) for v in expected]
    if column_name == 'amount':
        assert actual[0].as_tuple() == expected[0].as_tuple()


@pytest.fixture(params=['use_multistage_engine=true', 'multistage-dialect'])
def multistage_engine(request):
    host = os.getenv('PINOT_HOST', 'localhost')
    broker = os.getenv('PINOT_BROKER_PORT', '8000')
    controller = os.getenv('PINOT_CONTROLLER_PORT', '9000')
    query = (f'?controller=http://{host}:{controller}/'
             '&query_options=enableNullHandling%3Dtrue')
    if request.param == 'multistage-dialect':
        from sqlalchemy.dialects import registry
        registry.register('pinot.multistage', 'pinotdb.sqlalchemy',
                          'PinotMultiStageDialect')
        url = f'pinot+multistage://{host}:{broker}/query/sql{query}'
    else:
        url = f'pinot://{host}:{broker}/query/sql{query}&{request.param}'
    value = create_engine(url)
    try:
        yield value
    finally:
        value.dispose()


@pytest.mark.parametrize('column_name,expected', [
    ('amount', [
        Decimal('123456789012345678901234567890.12345678901234567890'),
        None, Decimal('-0.00000000000000000001'),
    ]),
    ('payload', [b'\x00\xff\x80A', None, b'']),
    ('tags', [['SJC', 'ABQ'], ['null-row'], ['empty-bytes']]),
    ('numbers', [[1, 2], [0], [-1]]),
])
def test_multistage_engine_values(multistage_engine, column_name, expected):
    table = Table('resultTypes', MetaData(), autoload_with=multistage_engine)
    # A self-join is rejected by the single-stage engine, so rows prove the
    # multi-stage engine served the query.
    other = table.alias('other')
    statement = (
        select(table.c[column_name])
        .join(other, table.c.id == other.c.id)
        .order_by(table.c.id).limit(3)
    )
    with multistage_engine.connect() as connection:
        actual = connection.execute(statement).scalars().all()
    assert actual == expected
    assert [type(v) for v in actual] == [type(v) for v in expected]
    if column_name == 'amount':
        assert actual[0].as_tuple() == expected[0].as_tuple()


def test_single_stage_rejects_the_join_used_as_multistage_proof(engine):
    table = Table('resultTypes', MetaData(), autoload_with=engine)
    other = table.alias('other')
    statement = select(table.c.id).join(other, table.c.id == other.c.id)
    with engine.connect() as connection, pytest.raises(Exception):
        connection.execute(statement).all()


def test_false_multistage_option_stays_single_stage(engine):
    url = engine.url.update_query_dict({'use_multistage_engine': 'false'})
    single = create_engine(url)
    try:
        test_single_stage_rejects_the_join_used_as_multistage_proof(single)
    finally:
        single.dispose()


def test_reflected_types_render_names(engine):
    table = Table('resultTypes', MetaData(), autoload_with=engine)
    rendered = {c.name: c.type.compile(dialect=engine.dialect)
                for c in table.columns}
    assert rendered['payload'] == 'BYTES'
    assert rendered['tags'] == 'VARCHAR ARRAY'
    assert rendered['numbers'] == 'NUMERIC ARRAY'
    assert rendered['amount'] == 'NUMERIC'
