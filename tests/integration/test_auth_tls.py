"""Native Pinot TLS and basic auth (AUTH QuickStart plus resultTypes)."""
import os
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, Table, create_engine, select, text
from sqlalchemy.exc import NoSuchTableError

from pinotdb import exceptions

pytestmark = pytest.mark.skipif(
    os.getenv('PINOT_AUTH_TLS') != '1',
    reason='requires the AUTH QuickStart with native TLS listeners',
)

HOST = os.getenv('PINOT_HOST', '127.0.0.1')
BROKER = os.getenv('PINOT_BROKER_HTTPS_PORT', '8443')
BROKER_HTTP = os.getenv('PINOT_BROKER_PORT', '8000')
CONTROLLER = os.getenv('PINOT_CONTROLLER_HTTPS_PORT', '9443')
# AUTH QuickStart's built-in demo principal.
USER = os.getenv('PINOT_USER', 'admin')
PASSWORD = os.getenv('PINOT_PASSWORD', 'verysecret')
EXACT = [Decimal('123456789012345678901234567890.12345678901234567890'),
         None, Decimal('-0.00000000000000000001')]


def url(user=USER, password=PASSWORD, extra=''):
    auth = f'{user}:{password}@' if user else ''
    return (f'pinot+https://{auth}{HOST}:{BROKER}/query/sql'
            f'?controller=https://{HOST}:{CONTROLLER}/'
            f'&query_options=enableNullHandling%3Dtrue{extra}')


@pytest.fixture
def engine_for():
    engines = []

    def make(value):
        engines.append(create_engine(value))
        return engines[-1]

    yield make
    for value in engines:
        value.dispose()


@pytest.mark.parametrize('extra', ['', '&use_multistage_engine=true'])
def test_tls_auth_values_and_binary_filter(engine_for, extra):
    engine = engine_for(url(extra=extra))
    table = Table('resultTypes', MetaData(), autoload_with=engine)
    other = table.alias('other')
    statement = (select(table.c.amount, table.c.payload)
                 .order_by(table.c.id))
    if extra:
        statement = statement.join(other, table.c.id == other.c.id)
    with engine.connect() as connection:
        rows = connection.execute(statement).all()
        matched = connection.execute(
            select(table.c.id).where(table.c.payload == b'\x00\xff\x80A'),
        ).scalars().all()
    assert [row[0] for row in rows] == EXACT
    assert [row[1] for row in rows] == [b'\x00\xff\x80A', None, b'']
    assert matched == [1]


@pytest.mark.parametrize('user,password', [(USER, 'wrong'), (None, None)])
def test_broker_rejects_bad_credentials(engine_for, user, password):
    engine = engine_for(url(user=user, password=password))
    with engine.connect() as connection:
        with pytest.raises(Exception) as info:
            connection.execute(text('SELECT count(*) FROM resultTypes'))
    assert isinstance(info.value.orig, exceptions.OperationalError)
    assert 'timed out' not in str(info.value)


def test_controller_rejects_bad_credentials(engine_for):
    engine = engine_for(url(password='wrong'))
    with pytest.raises(exceptions.OperationalError):
        Table('resultTypes', MetaData(), autoload_with=engine)


def test_missing_table_is_still_no_such_table(engine_for):
    with pytest.raises(NoSuchTableError):
        Table('doesNotExist', MetaData(), autoload_with=engine_for(url()))


def test_plain_http_without_credentials_is_rejected(engine_for):
    engine = engine_for(
        f'pinot+http://{HOST}:{BROKER_HTTP}/query/sql'
        f'?controller=https://{HOST}:{CONTROLLER}/')
    with engine.connect() as connection:
        with pytest.raises(Exception) as info:
            connection.execute(text('SELECT count(*) FROM resultTypes'))
    assert isinstance(info.value.orig, exceptions.OperationalError)
