"""Broker wire types and reflected SQLAlchemy result processors agree."""
from decimal import Decimal

import pytest
from sqlalchemy import types

from pinotdb.db import (
    convert_result_if_required, get_types_from_column_data_types,
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
    ('BIG_DECIMAL', 'invalid'), ('BYTES', 'zz'), ('BYTES', 'f'),
])
def test_invalid_wire_values_are_not_hidden(wire_type, value):
    with pytest.raises((ValueError, ArithmeticError)):
        decode(wire_type, value)


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
