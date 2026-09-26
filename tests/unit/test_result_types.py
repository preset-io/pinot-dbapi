"""Broker wire types and reflected SQLAlchemy result processors agree."""
import json
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import types

from pinotdb import exceptions
from pinotdb.db import (
    Cursor, apply_parameters, convert_result_if_required,
    get_types_from_column_data_types,
)
from pinotdb.sqlalchemy import PinotDialect


def decode(wire_type, value):
    return convert_result_if_required(
        get_types_from_column_data_types([wire_type]), [[value]],
    )[0][0]


def process(type_, value):
    dialect = PinotDialect()
    processor = type_.dialect_impl(dialect).result_processor(dialect, None)
    return processor(value) if processor else value


@pytest.mark.parametrize('value', [
    '123456789012345678901234567890.12345678901234567890',
    '-0.00000000000000000001', '0.0000', '1E+40', None,
])
def test_decimal_wire_and_numeric_precision(value):
    expected = Decimal(value) if value is not None else None
    decoded = decode('BIG_DECIMAL', value)
    assert decoded == expected
    actual = process(types.Numeric(), decoded)
    assert actual == expected
    if value is not None:
        assert isinstance(actual, Decimal)
        assert actual.as_tuple() == expected.as_tuple()


@pytest.mark.parametrize('value', ['00ff8041', '', 'ABCDEF', None])
def test_bytes_wire_and_binary(value):
    expected = bytes.fromhex(value) if value is not None else None
    decoded = decode('BYTES', value)
    assert decoded == expected
    assert process(types.LargeBinary(), decoded) == expected


@pytest.mark.parametrize('wire_type,value', [
    ('BIG_DECIMAL', 'invalid'), ('BIG_DECIMAL', True), ('BIG_DECIMAL', {}),
    ('BIG_DECIMAL_ARRAY', ['1', 'x']),
    ('BYTES', 'zz'), ('BYTES', 'f'), ('BYTES', 12), ('BYTES_ARRAY', ['0g']),
    ('TIMESTAMP', 'not a timestamp'), ('JSON', '{broken'),
])
def test_invalid_wire_values_raise_data_error(wire_type, value):
    # PEP 249: undecodable server values surface as DatabaseError subclasses
    # that callers catching the DBAPI hierarchy can handle.
    with pytest.raises(exceptions.DataError) as info:
        decode(wire_type, value)
    assert isinstance(info.value, exceptions.DatabaseError)
    assert info.value.__cause__ is not None


def query(payload_text):
    cursor = Cursor(host='localhost', session=MagicMock(spec=httpx.Client))
    response = httpx.Response(200, content=payload_text.encode())
    return cursor.normalize_query_response('SELECT 1', response)


def payload(column_types, rows_json):
    names = json.dumps([f'c{i}' for i in range(len(column_types))])
    return (
        '{"resultTable": {"dataSchema": {"columnNames": ' + names
        + ', "columnDataTypes": ' + json.dumps(column_types) + '}, '
        '"rows": ' + rows_json + '}, "exceptions": [], '
        '"numServersQueried": 1, "numServersResponded": 1, '
        '"timeUsedMs": 1.5}'
    )


def test_big_decimal_json_number_keeps_exact_digits():
    # A JSON number must not be routed through float: this one has more
    # significant digits than a double can hold.
    cursor = query(payload(
        ['BIG_DECIMAL', 'DOUBLE', 'BIG_DECIMAL_ARRAY', 'DOUBLE_ARRAY', 'INT'],
        '[[12345678901234567890.123456789, 0.1, [1.10, "2.5", 3], '
        '[0.1, 2.5], 7]]',
    ))
    row = cursor.fetchall()[0]
    assert row[0] == Decimal('12345678901234567890.123456789')
    assert row[0].as_tuple() == Decimal(
        '12345678901234567890.123456789').as_tuple()
    assert row[2] == [Decimal('1.10'), Decimal('2.5'), Decimal(3)]
    assert row[2][0].as_tuple() == Decimal('1.10').as_tuple()
    # Other columns keep the default parser's plain floats and ints.
    assert row[1] == 0.1 and type(row[1]) is float
    assert row[3] == [0.1, 2.5] and all(type(v) is float for v in row[3])
    assert row[4] == 7 and type(row[4]) is int
    assert cursor.query_stats['timeUsedMs'] == 1.5
    assert type(cursor.query_stats['timeUsedMs']) is float


def test_big_decimal_strings_are_not_reparsed():
    cursor = query(payload(
        ['BIG_DECIMAL', 'DOUBLE'], '[["1.000000000000000000001", 0.5]]'))
    assert cursor.fetchall() == [[Decimal('1.000000000000000000001'), 0.5]]


def test_numeric_float_behavior_unchanged_and_asdecimal_false():
    dialect = PinotDialect()
    original = types.Numeric().result_processor(dialect, None)
    assert process(types.Numeric(), 1.25) == original(1.25)
    actual = process(types.Numeric(asdecimal=False), Decimal('1.25'))
    assert actual == 1.25
    assert isinstance(actual, float)
    assert process(types.Numeric(asdecimal=False), None) is None


@pytest.mark.parametrize('wire_type,item_type,value', [
    ('INT', types.BigInteger, [1, 2]),
    ('LONG', types.BigInteger, [1234567890123456789]),
    ('STRING', types.String, ['SJC', 'ABQ']),
    ('FLOAT', types.Float, [1.25, 2.5]),
    ('DOUBLE', types.Numeric, [1.25, 2.5]),
])
def test_multi_value_reflection(monkeypatch, wire_type, item_type, value):
    dialect = PinotDialect()
    monkeypatch.setattr(dialect, 'get_metadata_from_controller', lambda path: {
        'dimensionFieldSpecs': [
            {'name': 'multi', 'dataType': wire_type,
             'singleValueField': False},
            {'name': 'scalar', 'dataType': wire_type},
            {'name': 'explicit', 'dataType': wire_type,
             'singleValueField': True},
        ],
    })
    columns = dialect.get_columns(None, 'fixture')
    array = columns[0]['type']
    assert isinstance(array, types.ARRAY)
    assert isinstance(array.item_type, item_type)
    assert columns[1]['type'] is item_type
    assert columns[2]['type'] is item_type
    decoded = decode(wire_type + '_ARRAY', value)
    assert process(array, decoded) == [process(item_type(), v) for v in value]
    assert process(array, []) == []
    assert process(array, None) is None
    assert process(array, [None]) == [None]


@pytest.mark.parametrize('wire_type,value,expected', [
    ('BYTES_ARRAY', ['00ff', None, ''], [b'\x00\xff', None, b'']),
    ('BIG_DECIMAL_ARRAY', ['1.000000000000000001', None],
     [Decimal('1.000000000000000001'), None]),
    ('STRING_ARRAY', ['null', ''], ['null', '']),
    ('UNKNOWN_ARRAY', [1, 2], '[1, 2]'),
])
def test_array_wire_conversion(wire_type, value, expected):
    assert decode(wire_type, value) == expected


def test_array_tuple_option():
    array = types.ARRAY(types.Numeric, as_tuple=True)
    assert process(array, [1.25, None]) == (
        Decimal('1.2500000000'), None,
    )


@pytest.mark.parametrize('type_,expected', [
    (types.ARRAY(types.String, dimensions=1), 'VARCHAR ARRAY'),
    (types.ARRAY(types.BigInteger, dimensions=1), 'NUMERIC ARRAY'),
    (types.ARRAY(types.Numeric, dimensions=1), 'NUMERIC ARRAY'),
    (types.ARRAY(types.LargeBinary, dimensions=1), 'BYTES ARRAY'),
    (types.LargeBinary(), 'BYTES'),
])
def test_reflected_types_have_names(type_, expected):
    # Callers such as BI tools render reflected column types to text; an
    # unrenderable type loses the column type entirely.
    assert type_.compile(dialect=PinotDialect()) == expected


def test_every_reflected_scalar_and_multi_value_type_renders(monkeypatch):
    dialect = PinotDialect()
    wire_types = ['INT', 'LONG', 'FLOAT', 'DOUBLE', 'BIG_DECIMAL', 'BOOLEAN',
                  'TIMESTAMP', 'STRING', 'BYTES']
    specs = [{'name': f'sv_{t}', 'dataType': t} for t in wire_types] + [
        {'name': f'mv_{t}', 'dataType': t, 'singleValueField': False}
        for t in wire_types if t not in ('BIG_DECIMAL', 'BYTES')
    ]
    monkeypatch.setattr(dialect, 'get_metadata_from_controller',
                        lambda path: {'dimensionFieldSpecs': specs})
    # Inspector.get_columns instantiates type classes the same way.
    rendered = {
        c['name']: (c['type']() if isinstance(c['type'], type)
                    else c['type']).compile(dialect=dialect)
        for c in dialect.get_columns(None, 'fixture')
    }
    assert rendered['sv_BYTES'] == 'BYTES'
    for name, value in rendered.items():
        if name.startswith('mv_'):
            assert value == rendered[name.replace('mv_', 'sv_')] + ' ARRAY'


def test_struct_and_map_remain_unsupported():
    with pytest.raises(exceptions.NotSupportedError):
        types.BLOB().compile(dialect=PinotDialect())


@pytest.mark.parametrize('value,expected', [
    (b'\x00\xff\x80A', "hexToBytes('00ff8041')"),
    (bytearray(b'\x01'), "hexToBytes('01')"),
    (memoryview(b''), "hexToBytes('')"),
])
def test_bytes_parameters_render_as_pinot_bytes(value, expected):
    # Previously interpolated as a Python repr such as b'\x00...'.
    assert apply_parameters(
        'SELECT id FROM t WHERE payload = %(p)s', {'p': value},
    ) == f'SELECT id FROM t WHERE payload = {expected}'


def test_pep249_binary_constructor():
    import pinotdb
    assert pinotdb.Binary(b'\x00A') == b'\x00A'
    assert apply_parameters('%(p)s', {'p': pinotdb.Binary(b'\x00')}) == (
        "hexToBytes('00')")


def binary_statement(value, operator='eq'):
    from sqlalchemy import column, select, table
    t = table('resultTypes', column('id'),
              column('payload', types.LargeBinary()))
    condition = (t.c.payload.in_(value) if operator == 'in'
                 else t.c.payload == value)
    return select(t.c.id).where(condition)


@pytest.mark.parametrize('value', [b'\x00\xff\x80A', '00ff8041', '00FF8041'])
def test_binary_literal_binds(value):
    sql = str(binary_statement(value).compile(
        dialect=PinotDialect(), compile_kwargs={'literal_binds': True}))
    assert sql.endswith("payload = hexToBytes('00ff8041')")


def test_binary_literal_in_list_and_invalid_hex():
    sql = str(binary_statement(['00ff8041', ''], 'in').compile(
        dialect=PinotDialect(), compile_kwargs={'literal_binds': True}))
    assert sql.endswith(
        "payload IN (hexToBytes('00ff8041'), hexToBytes(''))")
    with pytest.raises(Exception):
        str(binary_statement('zz').compile(
            dialect=PinotDialect(), compile_kwargs={'literal_binds': True}))


@pytest.mark.parametrize('value', [b'\x00\xff\x80A', '00ff8041'])
def test_binary_bind_parameter_reaches_broker_as_bytes(value):
    from sqlalchemy import create_engine
    engine = create_engine(
        'pinot://localhost:8000/query/sql?controller=http://localhost:9000/')
    sent = []

    def fake_execute(self, operation, parameters=None, *args, **kwargs):
        sent.append(apply_parameters(operation, parameters or {}))
        self.description = [('id', None, None, None, None, None, None)]
        self._results = []
        return self

    import unittest.mock as mock
    with mock.patch.object(Cursor, 'execute', fake_execute):
        with engine.connect() as connection:
            connection.execute(binary_statement(value)).all()
    assert sent[0].endswith("payload = hexToBytes('00ff8041')")
