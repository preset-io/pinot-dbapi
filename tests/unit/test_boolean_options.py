"""String boolean options must not be enabled by Python truthiness."""
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.engine import make_url

from pinotdb import connect, exceptions
from pinotdb.db import Cursor
from pinotdb.sqlalchemy import PinotDialect, PinotMultiStageDialect


def payload(**options):
    cursor = Cursor(host='localhost', session=MagicMock(spec=httpx.Client),
                    **options)
    return cursor.finalize_query_payload('SELECT 1')


@pytest.mark.parametrize('value', ['false', 'False', '0', 'no', 'off', '',
                                   False, 0, None])
def test_false_strings_keep_single_stage_engine(value):
    assert payload(use_multistage_engine=value) == {'sql': 'SELECT 1'}


@pytest.mark.parametrize('value', ['true', 'TRUE', '1', 'yes', 'on', True, 1])
def test_true_values_enable_multistage_engine(value):
    assert payload(use_multistage_engine=value) == {
        'sql': 'SELECT 1', 'queryOptions': 'useMultistageEngine=true'}


@pytest.mark.parametrize('value', ['false', '0'])
def test_false_preserve_types_string_adds_no_option(value):
    assert payload(preserve_types=value) == {'sql': 'SELECT 1'}


@pytest.mark.parametrize('option', [
    'use_multistage_engine', 'preserve_types', 'debug'])
def test_unrecognized_boolean_is_rejected(option):
    with pytest.raises(exceptions.InterfaceError):
        payload(**{option: 'maybe'})


@pytest.mark.parametrize('value,expected', [
    ('true', True), ('1', True), ('yes', True), (True, True),
    ('false', False), ('0', False), ('no', False), (False, False),
])
def test_verify_ssl_is_parsed_like_the_other_options(value, expected):
    # verify_ssl=1 or verify_ssl=yes used to disable TLS verification.
    assert PinotDialect(verify_ssl=value)._verify_ssl is expected


def test_verify_ssl_defaults_on_and_rejects_unknown_values():
    assert PinotDialect()._verify_ssl is True
    with pytest.raises(exceptions.InterfaceError):
        PinotDialect(verify_ssl='maybe')


def test_dialect_verify_ssl_false_reaches_connect_args():
    dialect = PinotDialect(verify_ssl=False)
    _, kwargs = dialect.create_connect_args(
        make_url('pinot://localhost:8000/'))
    assert kwargs['verify_ssl'] is False


def test_verify_ssl_accepts_a_ca_bundle_path(tmp_path):
    bundle = tmp_path / 'ca.pem'
    bundle.write_text('')
    assert PinotDialect(verify_ssl=str(bundle))._verify_ssl == str(bundle)


@pytest.mark.parametrize('value,expected', [
    (None, True), ('true', True), ('1', True), ('yes', True), (True, True),
    ('false', False), ('0', False), ('no', False), (False, False),
])
def test_dbapi_verify_ssl_is_parsed_before_reaching_httpx(
        monkeypatch, value, expected):
    seen = {}
    real_client = httpx.Client

    def recording_client(*args, verify=True, **kwargs):
        seen['verify'] = verify
        return real_client(*args, verify=verify, **kwargs)

    monkeypatch.setattr(httpx, 'Client', recording_client)
    options = {} if value is None else {'verify_ssl': value}
    connect(host='localhost', **options).cursor()
    assert seen['verify'] is expected


def test_dbapi_verify_ssl_rejects_unknown_values():
    with pytest.raises(exceptions.InterfaceError):
        connect(host='localhost', verify_ssl='maybe').cursor()


@pytest.mark.parametrize('value,expected', [
    ('false', {'sql': 'SELECT 1'}),
    ('true', {'sql': 'SELECT 1',
              'queryOptions': 'useMultistageEngine=true'}),
])
def test_url_query_option_reaches_cursor(value, expected):
    dialect = PinotDialect()
    url = make_url(
        'pinot://localhost:8000/query/sql?controller=http://localhost:9000/'
        f'&use_multistage_engine={value}&debug=false')
    _, kwargs = dialect.create_connect_args(url)
    assert dialect._debug is False
    connection = connect(session=MagicMock(spec=httpx.Client), **kwargs)
    assert connection.use_multistage_engine is (value == 'true')
    assert connection._debug is False
    cursor = connection.cursor()
    assert cursor.finalize_query_payload('SELECT 1') == expected


def test_multistage_dialect_still_enables_engine():
    _, kwargs = PinotMultiStageDialect().create_connect_args(make_url(
        'pinot://localhost:8000/query/sql?controller=http://localhost:9000/'))
    cursor = connect(session=MagicMock(spec=httpx.Client), **kwargs).cursor()
    assert cursor.finalize_query_payload('SELECT 1')['queryOptions'] == (
        'useMultistageEngine=true')
