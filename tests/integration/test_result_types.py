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
