"""Pinot logical databases on a live broker and controller."""
import os
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

pytestmark = pytest.mark.skipif(
    os.getenv('PINOT_LOGICAL_DATABASES') != '1',
    reason='requires the logical_databases fixture',
)

HOST = os.getenv('PINOT_HOST', 'localhost')
BROKER = os.getenv('PINOT_BROKER_PORT', '8000')
CONTROLLER = os.getenv('PINOT_CONTROLLER_PORT', '9000')
ROWS = {
    'db2': [(1, Decimal('1.5'), b'\x0a'), (2, Decimal('2.5'), b'\x0b')],
    'db3': [(7, Decimal('7.25'), b'\xff')],
}


def url(extra=''):
    return (f'pinot://{HOST}:{BROKER}/query/sql'
            f'?controller=http://{HOST}:{CONTROLLER}/{extra}')


@pytest.fixture(params=['single-stage', 'multi-stage'])
def engine(request):
    extra = ('&use_multistage_engine=true'
             if request.param == 'multi-stage' else '')
    value = create_engine(url(extra))
    try:
        yield value
    finally:
        value.dispose()


def test_schema_names_list_every_logical_database(engine):
    names = inspect(engine).get_schema_names()
    assert names[0] == 'default'
    assert {'db2', 'db3'} <= set(names)


def test_table_names_come_from_the_requested_database(engine):
    inspector = inspect(engine)
    assert inspector.get_table_names(schema='db2') == ['rt']
    assert inspector.get_table_names(schema='db3') == ['rt']
    default = inspector.get_table_names()
    assert 'baseballStats' in default and 'rt' not in default
    assert inspector.get_table_names(schema='default') == default
    assert inspector.has_table('rt', schema='db2')
    assert not inspector.has_table('rt')
    assert not inspector.has_table('rt', schema='default')


@pytest.mark.parametrize('database', ['db2', 'db3'])
def test_reflect_and_query_each_database(engine, database):
    table = Table('rt', MetaData(), schema=database, autoload_with=engine)
    assert [c.name for c in table.columns] == [
        'id', 'amount', 'payload', 'tags', 'numbers']
    with engine.connect() as connection:
        rows = connection.execute(
            select(table.c.id, table.c.amount, table.c.payload)
            .order_by(table.c.id)).all()
        matched = connection.execute(
            select(table.c.id).where(table.c.payload == ROWS[database][0][2]),
        ).scalars().all()
    assert [tuple(r) for r in rows] == ROWS[database]
    assert matched == [ROWS[database][0][0]]


def test_default_database_is_unchanged(engine):
    for schema in (None, 'default'):
        table = Table('baseballStats', MetaData(), schema=schema,
                      autoload_with=engine)
        with engine.connect() as connection:
            assert connection.execute(
                select(table.c.playerName).limit(2)).scalars().all()


@pytest.mark.parametrize('extra', ['', '&use_multistage_engine=true'])
def test_database_option_is_explicit_single_database_mode(extra):
    value = create_engine(url('&database=db3' + extra))
    try:
        inspector = inspect(value)
        assert inspector.get_schema_names() == ['db3']
        assert inspector.get_table_names() == ['rt']
        with value.connect() as connection:
            assert connection.execute(
                text('SELECT id FROM rt')).scalars().all() == [7]
            table = Table('rt', MetaData(), autoload_with=value)
            assert connection.execute(
                select(table.c.id)).scalars().all() == [7]
    finally:
        value.dispose()
