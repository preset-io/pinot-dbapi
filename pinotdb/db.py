import asyncio
from functools import wraps
from decimal import Decimal
from typing import Any

import ciso8601
import json
import logging
import uuid
from collections import namedtuple
from enum import Enum
from pprint import pformat

import httpx
from urllib import parse

from pinotdb import exceptions

logger = logging.getLogger(__name__)

_QUERY_STATS_EXCLUDED_FIELDS = frozenset({
    "resultTable",
    "exceptions",
    "traceInfo",
    "selectionResults",
    "aggregationResults",
})


class Type(Enum):
    STRING = 1
    NUMBER = 2
    BOOLEAN = 3
    TIMESTAMP = 4
    JSON = 5
    BINARY = 6


_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off", ""})


def as_bool(value, name):
    """Read a boolean option that may arrive as a URL or JSON string.

    ``bool("false")`` is True, so string values are parsed explicitly and
    anything unrecognized is rejected rather than silently enabled.
    """
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    raise exceptions.InterfaceError(
        f"Invalid boolean value for {name}: {value!r}")


def connect(*args, **kwargs):
    """
    Constructor for creating a connection to the database.

        >>> conn = connect('localhost', 8099)
        >>> curs = conn.cursor()

    """
    return Connection(*args, **kwargs)


def connect_async(*args, **kwargs):
    """
    Constructor for creating a connection to the database.

        >>> conn = connect_async('localhost', 8099)
        >>> curs = conn.cursor()

    """
    return AsyncConnection(*args, **kwargs)


def check_closed(f):
    """Decorator that checks if connection/cursor is closed."""

    @wraps(f)
    def g(self, *args, **kwargs):
        if self.closed:
            raise exceptions.Error(f"{self.__class__.__name__} already closed")
        return f(self, *args, **kwargs)

    return g


def check_result(f):
    """Decorator that checks if the cursor has results from `execute`."""

    @wraps(f)
    def g(self, *args, **kwargs):
        if self._results is None:
            raise exceptions.Error("Called before `execute`")
        return f(self, *args, **kwargs)

    return g


def get_description_from_types(column_names, types):
    return [
        (
            name,  # name
            tc.code,  # type_code
            None,  # [display_size]
            None,  # [internal_size]
            None,  # [precision]
            None,  # [scale]
            None,  # [null_ok]
        )
        for name, tc in zip(column_names, types)
    ]


def get_columns_and_types(column_names, types):
    return [
        {
            'name': name,
            'type': type
        }
        for name, type in zip(column_names, types)
    ]


def get_query_stats(payload):
    """Return scalar top-level broker query stats from a Pinot response."""
    return {
        key: value for key, value in payload.items()
        if key not in _QUERY_STATS_EXCLUDED_FIELDS
        and not isinstance(value, (dict, list))
    }


TypeCodeAndValue = namedtuple(
    "TypeCodeAndValue", ["code", "is_iterable", "needs_conversion"]
)


def get_types_from_column_data_types(column_data_types):
    types = [None] * len(column_data_types)
    for column_index, column_data_type in enumerate(column_data_types):
        is_iterable = column_data_type.endswith("_ARRAY")
        data_type = (
            column_data_type[:-6] if is_iterable else column_data_type
        )
        if (
            data_type == "INT"
            or data_type == "LONG"
            or data_type == "FLOAT"
            or data_type == "DOUBLE"
        ):
            types[column_index] = TypeCodeAndValue(
                Type.NUMBER, is_iterable, False)
        elif data_type == "BIG_DECIMAL":
            types[column_index] = TypeCodeAndValue(
                Type.NUMBER, is_iterable, True)
        elif data_type == "BYTES":
            types[column_index] = TypeCodeAndValue(
                Type.BINARY, is_iterable, True)
        elif data_type == "STRING":
            types[column_index] = TypeCodeAndValue(
                Type.STRING, is_iterable, False)
        elif data_type == "BOOLEAN":
            types[column_index] = TypeCodeAndValue(
                Type.BOOLEAN, is_iterable, False)
        elif data_type == "TIMESTAMP":
            types[column_index] = TypeCodeAndValue(
                Type.TIMESTAMP, is_iterable, True)
        elif data_type == "JSON":
            types[column_index] = TypeCodeAndValue(
                Type.JSON, is_iterable, True)
        else:
            types[column_index] = TypeCodeAndValue(
                Type.STRING, is_iterable, True)
    return types


class Connection:
    """Connection to a Pinot database."""

    def __init__(self, *args, **kwargs):
        self._debug = as_bool(kwargs.get("debug", False), "debug")
        self._args = args
        self._kwargs = kwargs
        self.closed = False
        self.use_multistage_engine = as_bool(
            kwargs.get('use_multistage_engine', False),
            'use_multistage_engine',
        )
        self.query_options = kwargs.get('query_options', None)
        self.cursors = []
        self.session = kwargs.get('session')
        self.is_session_external = False
        if self.session:
            self.verify_session()
            self.is_session_external = True

    def verify_session(self):
        if self.session:
            assert isinstance(self.session, httpx.Client)

    @check_closed
    def close(self):
        """Close the connection now."""
        self.closed = True
        for cursor in self.cursors:
            try:
                cursor.close()
            except exceptions.Error:
                pass  # already closed
        # if we're managing the httpx session, attempt to close it
        if not self.is_session_external and self.session:
            self.session.close()

    @check_closed
    def commit(self):
        """
        Commit any pending transaction to the database.

        Not supported.
        """
        pass

    @check_closed
    def cursor(self):
        """Return a new Cursor Object using the connection."""
        if not self.session or self.session.is_closed:
            self.session = httpx.Client(
                verify=self._kwargs.get('verify_ssl'),
                timeout=(
                    float(self._kwargs.get('timeout'))
                    if self._kwargs.get('timeout')
                    else None
                ),
            )

        self._kwargs['session'] = self.session
        cursor = Cursor(*self._args, **self._kwargs)
        self.cursors.append(cursor)

        return cursor

    @check_closed
    def execute(self, operation, parameters=None):
        cursor = self.cursor()
        return cursor.execute(operation, parameters, self.query_options)

    def __enter__(self):
        return self.cursor()

    def __exit__(self, *exc):
        self.close()


class AsyncConnection(Connection):

    def verify_session(self):
        if self.session:
            assert isinstance(self.session, httpx.AsyncClient)

    @check_closed
    def cursor(self):
        """Return a new Cursor Object using the connection."""
        if not self.session or self.session.is_closed:
            self.session = httpx.AsyncClient(
                verify=self._kwargs.get('verify_ssl'),
                timeout=(
                    float(self._kwargs.get('timeout'))
                    if self._kwargs.get('timeout')
                    else None
                ),
            )

        self._kwargs['session'] = self.session
        cursor = AsyncCursor(*self._args, **self._kwargs)
        self.cursors.append(cursor)

        return cursor

    @check_closed
    async def close(self):
        """Close the connection now."""
        self.closed = True
        close_reqs = []
        for cursor in self.cursors:
            try:
                close_reqs.append(cursor.close())
            except exceptions.Error:
                pass  # already closed

        await asyncio.gather(*close_reqs)
        # if we're managing the httpx session, attempt to close it
        if not self.is_session_external:
            await self.session.aclose()

    @check_closed
    async def execute(self, operation, parameters=None):
        cursor = self.cursor()
        return await cursor.execute(operation, parameters, self.query_options)

    async def __aenter__(self):
        return self.cursor()

    async def __aexit__(self, *exc):
        await self.close()


def convert_result_if_required(data_types, rows):
    needs_conversion = any(t.needs_conversion for t in data_types)
    if not needs_conversion:
        return rows
    for i, t in enumerate(data_types):
        if t.needs_conversion:
            for row in rows:
                if row[i] is not None:
                    try:
                        row[i] = convert_result(t, row[i])
                    except (ValueError, TypeError, ArithmeticError) as e:
                        # PEP 249: invalid values from the server are data
                        # errors, not bare Python exceptions.
                        raise exceptions.DataError(
                            f"Cannot convert column {i} value {row[i]!r} "
                            f"as {t.code.name}: {e}"
                        ) from e
    return rows


def _to_decimal(value):
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str) or (
        isinstance(value, int) and not isinstance(value, bool)
    ):
        return Decimal(value)
    # A JSON float has already been rounded to binary floating point; the
    # response parser keeps such values as exact Decimal text instead.
    raise TypeError(f"expected a decimal string, got {type(value).__name__}")


def convert_result(data_type, raw_row):
    if raw_row is None:
        return None
    if data_type.is_iterable and data_type.code != Type.STRING:
        scalar_type = data_type._replace(is_iterable=False)
        return [convert_result(scalar_type, value) for value in raw_row]
    if data_type.code == Type.NUMBER:
        # BIG_DECIMAL is a decimal string, not a JSON-encoded string or float.
        return _to_decimal(raw_row)
    elif data_type.code == Type.BINARY:
        # The broker serializes BYTES as hexadecimal, not text bytes.
        if not isinstance(raw_row, str):
            raise TypeError(
                f"expected a hexadecimal string, got {type(raw_row).__name__}"
            )
        return bytes.fromhex(raw_row)
    elif data_type.code == Type.TIMESTAMP:
        # Pinot returns TIMESTAMP as STRING
        return ciso8601.parse_datetime(raw_row)
    elif data_type.code == Type.JSON:
        # Pinot returns JSON as STRING
        return json.loads(raw_row) if raw_row != '' else None
    else:
        return json.dumps(raw_row)


def _contains_float(value):
    if isinstance(value, list):
        return any(_contains_float(item) for item in value)
    return isinstance(value, float)


def _decimals_to_floats(value):
    if isinstance(value, list):
        return [_decimals_to_floats(item) for item in value]
    return float(value) if isinstance(value, Decimal) else value


def preserve_exact_decimals(column_data_types, rows, response_text):
    """Re-read JSON numbers in BIG_DECIMAL columns without float rounding.

    Pinot serializes BIG_DECIMAL as a string, but a number is valid JSON and
    the default parser would silently round it to a binary float. Only when
    that happens is the response parsed again with exact decimals; every other
    column keeps the float the default parser produced.
    """
    decimal_columns = {
        i for i, column_data_type in enumerate(column_data_types)
        if column_data_type in ("BIG_DECIMAL", "BIG_DECIMAL_ARRAY")
    }
    if not decimal_columns or not any(
        _contains_float(row[i]) for row in rows for i in decimal_columns
    ):
        return rows
    exact_rows = json.loads(
        response_text, parse_float=Decimal
    )["resultTable"]["rows"]
    return [
        [
            value if i in decimal_columns else _decimals_to_floats(value)
            for i, value in enumerate(row)
        ]
        for row in exact_rows
    ]


class Cursor:
    """Connection cursor."""

    def __init__(
        self,
        host,
        port=8099,
        scheme="http",
        path="/query/sql",
        username=None,
        password=None,
        # TODO: Remove this unused parameter when we can afford to break the
        #  interface (e.g. new minor version).
        verify_ssl=True,
        timeout=10.0,
        extra_request_headers="",
        debug=False,
        preserve_types=False,
        ignore_exception_error_codes="",
        acceptable_respond_fraction=-1,
        # TODO: Move this parameter when we can afford to break the
        #  interface (e.g. new minor version).
        session=None,
        use_multistage_engine=False,
        query_options=None,
        **kwargs
    ):
        self.url = parse.urlunparse(
            (scheme, f"{host}:{port}", path, None, None, None))
        self.session = session

        # This read/write attribute specifies the number of rows to fetch at a
        # time with .fetchmany(). It defaults to 1 meaning to fetch a single
        # row at a time.
        self.arraysize = 1

        self.closed = False

        # these are updated only after a query
        self.description = None
        self.schema = None
        self.rowcount = -1
        self._results = None
        self.raw_query_response = None
        self.query_stats = {}
        self.timeUsedMs = -1
        self._debug = as_bool(debug, "debug")
        self._preserve_types = as_bool(preserve_types, "preserve_types")
        self._use_multistage_engine = as_bool(
            use_multistage_engine, "use_multistage_engine")
        self._query_options = query_options
        self.acceptable_respond_fraction = acceptable_respond_fraction
        if ignore_exception_error_codes:
            self._ignore_exception_error_codes = set(
                [int(x) for x in ignore_exception_error_codes.split(",")]
            )
        else:
            self._ignore_exception_error_codes = []

        self.auth = None
        if username and password:
            self.auth = httpx.DigestAuth(username, password)

        self.session.headers.update({"Content-Type": "application/json"})

        extra_headers = {}
        if extra_request_headers:
            for header in extra_request_headers.split(","):
                k, v = header.split("=", 1)
                extra_headers[k] = v
        if 'database' in kwargs:
            extra_headers['database'] = kwargs['database']
        self.session.headers.update(extra_headers)

    @check_closed
    def close(self):
        """Close the cursor."""
        if self.session is not None and not self.session.is_closed:
            self.session.close()
        self.closed = True

    def is_valid_exception(self, e):
        if "errorCode" not in e:
            return True
        else:
            return e["errorCode"] not in self._ignore_exception_error_codes

    def check_sufficient_responded(self, query, queried, responded):
        fraction = self.acceptable_respond_fraction
        if fraction == 0:
            return
        if queried < 0 or responded < 0:
            responded = -1
            needed = -1
        elif fraction <= -1:
            needed = queried
        elif 0 < fraction < 1:
            needed = int(fraction * queried)
        else:
            needed = fraction
        if responded < 0 or responded < needed:
            raise exceptions.DatabaseError(
                f"Query\n\n{query} timed out: Out of {queried}, only"
                f" {responded} responded, while needed was {needed}"
            )

    def finalize_query_payload(
            self, operation, parameters=None, queryOptions=None
    ):
        query = apply_parameters(operation, parameters or {})

        if self._preserve_types:
            query += " OPTION(preserveType='true')"

        if self._use_multistage_engine:
            if queryOptions:
                queryOptions += ";useMultistageEngine=true"
            else:
                queryOptions = "useMultistageEngine=true"
        if queryOptions:
            return {"sql": query, "queryOptions": queryOptions}
        else:
            return {"sql": query}

    def normalize_query_response(self, input_query, query_response):
        try:
            payload = query_response.json()
            self.raw_query_response = {
                "response": payload,
                "status_code": query_response.status_code,
            }
        except Exception as e:
            self.raw_query_response = {
                "response": query_response.text,
                "status_code": query_response.status_code,
            }
            raise exceptions.DatabaseError(
                f"Error when querying {input_query} from {self.url}, "
                f"raw response is:\n{query_response.text}"
            ) from e

        if self._debug:
            status_code = (
                0 if not query_response else query_response.status_code)
            logger.info(
                f"Got the payload of type {type(payload)} "
                f"with the status code {status_code}:\n{payload}"
            )

        # Raise HTTP errors before reading query stats: an error response
        # carries no server counts, which would otherwise be misreported as
        # a timeout.
        if query_response.status_code != 200:
            msg = (
                f"Query\n\n{input_query}\n\nreturned an error: "
                f"{query_response.status_code}\n"
                f"Full response is {pformat(payload)}")
            if query_response.status_code in (401, 403):
                raise exceptions.OperationalError(
                    "Pinot broker rejected the request credentials. " + msg)
            raise exceptions.ProgrammingError(msg)

        self.query_stats = get_query_stats(payload)
        num_servers_responded = self.query_stats.get("numServersResponded", -1)
        num_servers_queried = self.query_stats.get("numServersQueried", -1)
        self.timeUsedMs = self.query_stats.get("timeUsedMs", -1)

        self.check_sufficient_responded(
            input_query, num_servers_queried, num_servers_responded
        )

        query_exceptions = [
            e for e in payload.get("exceptions", [])
            if self.is_valid_exception(e)
        ]
        if query_exceptions:
            msg = "\n".join(
                pformat(exception) for exception in query_exceptions)
            raise exceptions.DatabaseError(msg)

        # array of array, where inner array is array of column values
        rows = []
        # column names, such that len(column_names) == len(rows[0])
        column_names = []
        # column data types 1:1 mapping to column_names
        column_data_types = []
        if "resultTable" in payload:
            results = payload["resultTable"]
            data_schema = results.get("dataSchema")
            column_names = data_schema.get("columnNames")
            column_data_types = data_schema.get("columnDataTypes")
            values = results.get("rows")
            if column_names:
                rows = preserve_exact_decimals(
                    column_data_types, values, query_response.text)
            else:
                raise exceptions.DatabaseError(
                    "Expected columns and results in resultTable, "
                    f"but got {pformat(results)} instead"
                )

        logger.debug(
            f"Got the rows as a type {type(rows)} of size {len(rows)}")
        if logger.isEnabledFor(logging.DEBUG):  # pragma: no cover
            logger.debug(pformat(rows))
        self.description = None
        self._results = []
        if column_data_types:
            types = get_types_from_column_data_types(column_data_types)
            if self._debug:
                logger.info(
                    f"Column_names are {pformat(column_names)}, "
                    f"Column_data_types are {pformat(column_data_types)}, "
                    f"Types are {pformat(types)}"
                )
            self._results = convert_result_if_required(types, rows)
            self.description = get_description_from_types(column_names, types)
            self.schema = get_columns_and_types(
                column_names, column_data_types)
        return self

    @check_closed
    # TODO: Rename queryOptions to query_options when releasing a breaking
    #  version - even though Pinot understands "queryOptions", we don't need
    #  to follow the same camel casing convention, but rather should stick
    #  to PEP-8 instead.
    def execute(self, operation, parameters=None, queryOptions=None, **kwargs):
        if not queryOptions:
            queryOptions = ""
        if self._query_options:
            queryOptions = queryOptions + ";" + self._query_options

        query = self.finalize_query_payload(
            operation, parameters, queryOptions)

        correlation_id = str(uuid.uuid4())
        if self.auth and self.auth._username and self.auth._password:
            r = self.session.post(
                self.url,
                json=query,
                headers={"X-Correlation-Id": correlation_id},
                auth=(self.auth._username, self.auth._password),
                **kwargs)
        else:
            r = self.session.post(
                self.url,
                json=query,
                headers={"X-Correlation-Id": correlation_id},
                **kwargs)

        return self.normalize_query_response(query, r)

    @check_closed
    def executemany(self, operation, seq_of_parameters=None):
        raise exceptions.NotSupportedError(
            "`executemany` is not supported, use `execute` instead"
        )

    @check_result
    @check_closed
    def fetchone(self):
        """
        Fetch the next row of a query result set, returning a single sequence,
        or `None` when no more data is available.
        """
        try:
            return self._results.pop(0)
        except IndexError:
            return None

    @check_result
    @check_closed
    def fetchmany(self, size=None):
        """
        Fetch the next set of rows of a query result, returning a sequence of
        sequences (e.g. a list of tuples). An empty sequence is returned when
        no more rows are available.
        """
        size = size or self.arraysize
        output, self._results = self._results[:size], self._results[size:]
        return output

    @check_result
    @check_closed
    def fetchall(self):
        """
        Fetch all (remaining) rows of a query result, returning them as a
        sequence of sequences (e.g. a list of tuples).
        """
        results, self._results = self._results, []
        return results

    @check_result
    @check_closed
    def fetchwithschema(self):
        """
        Fetch results with schema. Schema includs column names and type
        """
        return {'schema': self.schema,
                'results': self._results}

    @check_closed
    def setinputsizes(self, sizes):
        # not supported
        pass

    @check_closed
    def setoutputsizes(self, sizes):
        # not supported
        pass

    @check_closed
    def __iter__(self):
        return self

    @check_closed
    def __next__(self):
        output = self.fetchone()
        if output is None:
            raise StopIteration

        return output

    next = __next__


class AsyncCursor(Cursor):
    @check_closed
    async def execute(
            self, operation, parameters=None, queryOptions=None, **kwargs
    ):
        if not queryOptions:
            queryOptions = ""
        if self._query_options:
            queryOptions = queryOptions + ";" + self._query_options

        query = self.finalize_query_payload(
            operation, parameters, queryOptions)

        correlation_id = str(uuid.uuid4())
        if self.auth and self.auth._username and self.auth._password:
            r = await self.session.post(
                self.url,
                json=query,
                headers={"X-Correlation-Id": correlation_id},
                auth=(self.auth._username, self.auth._password),
                **kwargs)
        else:
            r = await self.session.post(
                self.url,
                json=query,
                headers={"X-Correlation-Id": correlation_id},
                **kwargs)

        return self.normalize_query_response(query, r)

    @check_closed
    async def close(self):
        """Close the cursor."""
        await self.session.aclose()
        self.closed = True


def apply_parameters(operation, parameters):
    escaped_parameters = {
        key: escape_parameter(value) for key, value in parameters.items()}
    escaped_operation = escape_operation(operation)
    return escaped_operation % escaped_parameters


def Binary(value):
    """PEP 249 constructor for a binary (BYTES) parameter."""
    return bytes(value)


def binary_literal(value) -> str:
    """Render bytes as a Pinot BYTES expression.

    Neither quoted form works on both engines: the single-stage engine
    compares a BYTES column with a '<hex>' string (and silently matches
    nothing for X'<hex>'), while the multi-stage engine rejects '<hex>' and
    requires X'<hex>'. hexToBytes('<hex>') is accepted by both.
    """
    return "hexToBytes('{}')".format(bytes(value).hex())


def escape_parameter(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return binary_literal(value)
    elif value == "*":
        return value
    elif isinstance(value, str):
        return "'{}'".format(value.replace("'", "''"))
    elif isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    elif isinstance(value, (list, tuple)):
        return ", ".join(str(escape_parameter(element)) for element in value)
    return value


def escape_operation(value: str) -> str:
    return value.replace('%%', '%').replace('%', '%%').replace('%(', '(')
