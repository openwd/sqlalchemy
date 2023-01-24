# testing/assertions.py
# Copyright (C) 2005-2023 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php
# mypy: ignore-errors


from __future__ import annotations

from collections import defaultdict
import contextlib
from copy import copy
from itertools import filterfalse
import re
import sys
import warnings

from . import assertsql
from . import config
from . import engines
from . import mock
from .exclusions import db_spec
from .util import fail
from .. import exc as sa_exc
from .. import schema
from .. import sql
from .. import types as sqltypes
from .. import util
from ..engine import default
from ..engine import url
from ..sql.selectable import LABEL_STYLE_TABLENAME_PLUS_COL
from ..util import decorator


def expect_warnings(*messages, **kw):
    """Context manager which expects one or more warnings.

    With no arguments, squelches all SAWarning emitted via
    sqlalchemy.util.warn and sqlalchemy.util.warn_limited.   Otherwise
    pass string expressions that will match selected warnings via regex;
    all non-matching warnings are sent through.

    The expect version **asserts** that the warnings were in fact seen.

    Note that the test suite sets SAWarning warnings to raise exceptions.

    """  # noqa
    return _expect_warnings(sa_exc.SAWarning, messages, **kw)


@contextlib.contextmanager
def expect_warnings_on(db, *messages, **kw):
    """Context manager which expects one or more warnings on specific
    dialects.

    The expect version **asserts** that the warnings were in fact seen.

    """
    spec = db_spec(db)

    if isinstance(db, str) and not spec(config._current):
        yield
    else:
        with expect_warnings(*messages, **kw):
            yield


def emits_warning(*messages):
    """Decorator form of expect_warnings().

    Note that emits_warning does **not** assert that the warnings
    were in fact seen.

    """

    @decorator
    def decorate(fn, *args, **kw):
        with expect_warnings(assert_=False, *messages):
            return fn(*args, **kw)

    return decorate


def expect_deprecated(*messages, **kw):
    return _expect_warnings(sa_exc.SADeprecationWarning, messages, **kw)


def expect_deprecated_20(*messages, **kw):
    return _expect_warnings(sa_exc.Base20DeprecationWarning, messages, **kw)


def emits_warning_on(db, *messages):
    """Mark a test as emitting a warning on a specific dialect.

    With no arguments, squelches all SAWarning failures.  Or pass one or more
    strings; these will be matched to the root of the warning description by
    warnings.filterwarnings().

    Note that emits_warning_on does **not** assert that the warnings
    were in fact seen.

    """

    @decorator
    def decorate(fn, *args, **kw):
        with expect_warnings_on(db, assert_=False, *messages):
            return fn(*args, **kw)

    return decorate


def uses_deprecated(*messages):
    """Mark a test as immune from fatal deprecation warnings.

    With no arguments, squelches all SADeprecationWarning failures.
    Or pass one or more strings; these will be matched to the root
    of the warning description by warnings.filterwarnings().

    As a special case, you may pass a function name prefixed with //
    and it will be re-written as needed to match the standard warning
    verbiage emitted by the sqlalchemy.util.deprecated decorator.

    Note that uses_deprecated does **not** assert that the warnings
    were in fact seen.

    """

    @decorator
    def decorate(fn, *args, **kw):
        with expect_deprecated(*messages, assert_=False):
            return fn(*args, **kw)

    return decorate


_FILTERS = None
_SEEN = None
_EXC_CLS = None


@contextlib.contextmanager
def _expect_warnings(
    exc_cls,
    messages,
    regex=True,
    search_msg=False,
    assert_=True,
    raise_on_any_unexpected=False,
    squelch_other_warnings=False,
):

    global _FILTERS, _SEEN, _EXC_CLS

    if regex or search_msg:
        filters = [re.compile(msg, re.I | re.S) for msg in messages]
    else:
        filters = list(messages)

    if _FILTERS is not None:
        # nested call; update _FILTERS and _SEEN, return.  outer
        # block will assert our messages
        assert _SEEN is not None
        assert _EXC_CLS is not None
        _FILTERS.extend(filters)
        _SEEN.update(filters)
        _EXC_CLS += (exc_cls,)
        yield
    else:
        seen = _SEEN = set(filters)
        _FILTERS = filters
        _EXC_CLS = (exc_cls,)

        if raise_on_any_unexpected:

            def real_warn(msg, *arg, **kw):
                raise AssertionError("Got unexpected warning: %r" % msg)

        else:
            real_warn = warnings.warn

        def our_warn(msg, *arg, **kw):

            if isinstance(msg, _EXC_CLS):
                exception = type(msg)
                msg = str(msg)
            elif arg:
                exception = arg[0]
            else:
                exception = None

            if not exception or not issubclass(exception, _EXC_CLS):
                if not squelch_other_warnings:
                    return real_warn(msg, *arg, **kw)
                else:
                    return

            if not filters and not raise_on_any_unexpected:
                return

            for filter_ in filters:
                if (
                    (search_msg and filter_.search(msg))
                    or (regex and filter_.match(msg))
                    or (not regex and filter_ == msg)
                ):
                    seen.discard(filter_)
                    break
            else:
                if not squelch_other_warnings:
                    real_warn(msg, *arg, **kw)

        with mock.patch("warnings.warn", our_warn):
            try:
                yield
            finally:
                _SEEN = _FILTERS = _EXC_CLS = None

                if assert_:
                    assert not seen, "Warnings were not seen: %s" % ", ".join(
                        "%r" % (s.pattern if regex else s) for s in seen
                    )


def global_cleanup_assertions():
    """Check things that have to be finalized at the end of a test suite.

    Hardcoded at the moment, a modular system can be built here
    to support things like PG prepared transactions, tables all
    dropped, etc.

    """
    _assert_no_stray_pool_connections()


def _assert_no_stray_pool_connections():
    engines.testing_reaper.assert_all_closed()


def int_within_variance(expected, received, variance):
    deviance = int(expected * variance)
    assert (
        abs(received - expected) < deviance
    ), "Given int value %s is not within %d%% of expected value %s" % (
        received,
        variance * 100,
        expected,
    )


def eq_regex(a, b, msg=None):
    assert re.match(b, a), msg or "%r !~ %r" % (a, b)


def eq_(a, b, msg=None):
    """Assert a == b, with repr messaging on failure."""
    assert a == b, msg or "%r != %r" % (a, b)


def ne_(a, b, msg=None):
    """Assert a != b, with repr messaging on failure."""
    assert a != b, msg or "%r == %r" % (a, b)


def le_(a, b, msg=None):
    """Assert a <= b, with repr messaging on failure."""
    assert a <= b, msg or "%r != %r" % (a, b)


def is_instance_of(a, b, msg=None):
    assert isinstance(a, b), msg or "%r is not an instance of %r" % (a, b)


def is_none(a, msg=None):
    is_(a, None, msg=msg)


def is_not_none(a, msg=None):
    is_not(a, None, msg=msg)


def is_true(a, msg=None):
    is_(bool(a), True, msg=msg)


def is_false(a, msg=None):
    is_(bool(a), False, msg=msg)


def is_(a, b, msg=None):
    """Assert a is b, with repr messaging on failure."""
    assert a is b, msg or "%r is not %r" % (a, b)


def is_not(a, b, msg=None):
    """Assert a is not b, with repr messaging on failure."""
    assert a is not b, msg or "%r is %r" % (a, b)


# deprecated.  See #5429
is_not_ = is_not


def in_(a, b, msg=None):
    """Assert a in b, with repr messaging on failure."""
    assert a in b, msg or "%r not in %r" % (a, b)


def not_in(a, b, msg=None):
    """Assert a in not b, with repr messaging on failure."""
    assert a not in b, msg or "%r is in %r" % (a, b)


# deprecated.  See #5429
not_in_ = not_in


def startswith_(a, fragment, msg=None):
    """Assert a.startswith(fragment), with repr messaging on failure."""
    assert a.startswith(fragment), msg or "%r does not start with %r" % (
        a,
        fragment,
    )


def eq_ignore_whitespace(a, b, msg=None):
    a = re.sub(r"^\s+?|\n", "", a)
    a = re.sub(r" {2,}", " ", a)
    a = re.sub(r"\t", "", a)
    b = re.sub(r"^\s+?|\n", "", b)
    b = re.sub(r" {2,}", " ", b)
    b = re.sub(r"\t", "", b)

    assert a == b, msg or "%r != %r" % (a, b)


def _assert_proper_exception_context(exception):
    """assert that any exception we're catching does not have a __context__
    without a __cause__, and that __suppress_context__ is never set.

    Python 3 will report nested as exceptions as "during the handling of
    error X, error Y occurred". That's not what we want to do.  we want
    these exceptions in a cause chain.

    """

    if (
        exception.__context__ is not exception.__cause__
        and not exception.__suppress_context__
    ):
        assert False, (
            "Exception %r was correctly raised but did not set a cause, "
            "within context %r as its cause."
            % (exception, exception.__context__)
        )


def assert_raises(except_cls, callable_, *args, **kw):
    return _assert_raises(except_cls, callable_, args, kw, check_context=True)


def assert_raises_context_ok(except_cls, callable_, *args, **kw):
    return _assert_raises(except_cls, callable_, args, kw)


def assert_raises_message(except_cls, msg, callable_, *args, **kwargs):
    return _assert_raises(
        except_cls, callable_, args, kwargs, msg=msg, check_context=True
    )


def assert_warns(except_cls, callable_, *args, **kwargs):
    """legacy adapter function for functions that were previously using
    assert_raises with SAWarning or similar.

    has some workarounds to accommodate the fact that the callable completes
    with this approach rather than stopping at the exception raise.


    """
    with _expect_warnings(except_cls, [".*"], squelch_other_warnings=True):
        return callable_(*args, **kwargs)


def assert_warns_message(except_cls, msg, callable_, *args, **kwargs):
    """legacy adapter function for functions that were previously using
    assert_raises with SAWarning or similar.

    has some workarounds to accommodate the fact that the callable completes
    with this approach rather than stopping at the exception raise.

    Also uses regex.search() to match the given message to the error string
    rather than regex.match().

    """
    with _expect_warnings(
        except_cls,
        [msg],
        search_msg=True,
        regex=False,
        squelch_other_warnings=True,
    ):
        return callable_(*args, **kwargs)


def assert_raises_message_context_ok(
    except_cls, msg, callable_, *args, **kwargs
):
    return _assert_raises(except_cls, callable_, args, kwargs, msg=msg)


def _assert_raises(
    except_cls, callable_, args, kwargs, msg=None, check_context=False
):

    with _expect_raises(except_cls, msg, check_context) as ec:
        callable_(*args, **kwargs)
    return ec.error


class _ErrorContainer:
    error = None


@contextlib.contextmanager
def _expect_raises(except_cls, msg=None, check_context=False):
    if (
        isinstance(except_cls, type)
        and issubclass(except_cls, Warning)
        or isinstance(except_cls, Warning)
    ):
        raise TypeError(
            "Use expect_warnings for warnings, not "
            "expect_raises / assert_raises"
        )
    ec = _ErrorContainer()
    if check_context:
        are_we_already_in_a_traceback = sys.exc_info()[0]
    try:
        yield ec
        success = False
    except except_cls as err:
        ec.error = err
        success = True
        if msg is not None:
            # I'm often pdbing here, and "err" above isn't
            # in scope, so assign the string explicitly
            error_as_string = str(err)
            assert re.search(msg, error_as_string, re.UNICODE), "%r !~ %s" % (
                msg,
                error_as_string,
            )
        if check_context and not are_we_already_in_a_traceback:
            _assert_proper_exception_context(err)
        print(str(err).encode("utf-8"))

    # it's generally a good idea to not carry traceback objects outside
    # of the except: block, but in this case especially we seem to have
    # hit some bug in either python 3.10.0b2 or greenlet or both which
    # this seems to fix:
    # https://github.com/python-greenlet/greenlet/issues/242
    del ec

    # assert outside the block so it works for AssertionError too !
    assert success, "Callable did not raise an exception"


def expect_raises(except_cls, check_context=True):
    return _expect_raises(except_cls, check_context=check_context)


def expect_raises_message(except_cls, msg, check_context=True):
    return _expect_raises(except_cls, msg=msg, check_context=check_context)


class AssertsCompiledSQL:
    def assert_compile(
        self,
        clause,
        result,
        params=None,
        checkparams=None,
        for_executemany=False,
        check_literal_execute=None,
        check_post_param=None,
        dialect=None,
        checkpositional=None,
        check_prefetch=None,
        use_default_dialect=False,
        allow_dialect_select=False,
        supports_default_values=True,
        supports_default_metavalue=True,
        literal_binds=False,
        render_postcompile=False,
        schema_translate_map=None,
        render_schema_translate=False,
        default_schema_name=None,
        from_linting=False,
        check_param_order=True,
    ):
        if use_default_dialect:
            dialect = default.DefaultDialect()
            dialect.supports_default_values = supports_default_values
            dialect.supports_default_metavalue = supports_default_metavalue
        elif allow_dialect_select:
            dialect = None
        else:
            if dialect is None:
                dialect = getattr(self, "__dialect__", None)

            if dialect is None:
                dialect = config.db.dialect
            elif dialect == "default" or dialect == "default_qmark":
                if dialect == "default":
                    dialect = default.DefaultDialect()
                else:
                    dialect = default.DefaultDialect("qmark")
                dialect.supports_default_values = supports_default_values
                dialect.supports_default_metavalue = supports_default_metavalue
            elif dialect == "default_enhanced":
                dialect = default.StrCompileDialect()
            elif isinstance(dialect, str):
                dialect = url.URL.create(dialect).get_dialect()()

        if default_schema_name:
            dialect.default_schema_name = default_schema_name

        kw = {}
        compile_kwargs = {}

        if schema_translate_map:
            kw["schema_translate_map"] = schema_translate_map

        if params is not None:
            kw["column_keys"] = list(params)

        if literal_binds:
            compile_kwargs["literal_binds"] = True

        if render_postcompile:
            compile_kwargs["render_postcompile"] = True

        if for_executemany:
            kw["for_executemany"] = True

        if render_schema_translate:
            kw["render_schema_translate"] = True

        if from_linting or getattr(self, "assert_from_linting", False):
            kw["linting"] = sql.FROM_LINTING

        from sqlalchemy import orm

        if isinstance(clause, orm.Query):
            stmt = clause._statement_20()
            stmt._label_style = LABEL_STYLE_TABLENAME_PLUS_COL
            clause = stmt

        if compile_kwargs:
            kw["compile_kwargs"] = compile_kwargs

        class DontAccess:
            def __getattribute__(self, key):
                raise NotImplementedError(
                    "compiler accessed .statement; use "
                    "compiler.current_executable"
                )

        class CheckCompilerAccess:
            def __init__(self, test_statement):
                self.test_statement = test_statement
                self._annotations = {}
                self.supports_execution = getattr(
                    test_statement, "supports_execution", False
                )

                if self.supports_execution:
                    self._execution_options = test_statement._execution_options

                    if hasattr(test_statement, "_returning"):
                        self._returning = test_statement._returning
                    if hasattr(test_statement, "_inline"):
                        self._inline = test_statement._inline
                    if hasattr(test_statement, "_return_defaults"):
                        self._return_defaults = test_statement._return_defaults

            @property
            def _variant_mapping(self):
                return self.test_statement._variant_mapping

            def _default_dialect(self):
                return self.test_statement._default_dialect()

            def compile(self, dialect, **kw):
                return self.test_statement.compile.__func__(
                    self, dialect=dialect, **kw
                )

            def _compiler(self, dialect, **kw):
                return self.test_statement._compiler.__func__(
                    self, dialect, **kw
                )

            def _compiler_dispatch(self, compiler, **kwargs):
                if hasattr(compiler, "statement"):
                    with mock.patch.object(
                        compiler, "statement", DontAccess()
                    ):
                        return self.test_statement._compiler_dispatch(
                            compiler, **kwargs
                        )
                else:
                    return self.test_statement._compiler_dispatch(
                        compiler, **kwargs
                    )

        # no construct can assume it's the "top level" construct in all cases
        # as anything can be nested.  ensure constructs don't assume they
        # are the "self.statement" element
        c = CheckCompilerAccess(clause).compile(dialect=dialect, **kw)

        if isinstance(clause, sqltypes.TypeEngine):
            cache_key_no_warnings = clause._static_cache_key
            if cache_key_no_warnings:
                hash(cache_key_no_warnings)
        else:
            cache_key_no_warnings = clause._generate_cache_key()
            if cache_key_no_warnings:
                hash(cache_key_no_warnings[0])

        param_str = repr(getattr(c, "params", {}))
        param_str = param_str.encode("utf-8").decode("ascii", "ignore")
        print(("\nSQL String:\n" + str(c) + param_str).encode("utf-8"))

        cc = re.sub(r"[\n\t]", "", str(c))

        eq_(cc, result, "%r != %r on dialect %r" % (cc, result, dialect))

        if checkparams is not None:
            if render_postcompile:
                expanded_state = c.construct_expanded_state(
                    params, escape_names=False
                )
                eq_(expanded_state.parameters, checkparams)
            else:
                eq_(c.construct_params(params), checkparams)
        if checkpositional is not None:
            if render_postcompile:
                expanded_state = c.construct_expanded_state(
                    params, escape_names=False
                )
                eq_(
                    tuple(
                        [
                            expanded_state.parameters[x]
                            for x in expanded_state.positiontup
                        ]
                    ),
                    checkpositional,
                )
            else:
                p = c.construct_params(params, escape_names=False)
                eq_(tuple([p[x] for x in c.positiontup]), checkpositional)
        if check_prefetch is not None:
            eq_(c.prefetch, check_prefetch)
        if check_literal_execute is not None:
            eq_(
                {
                    c.bind_names[b]: b.effective_value
                    for b in c.literal_execute_params
                },
                check_literal_execute,
            )
        if check_post_param is not None:
            eq_(
                {
                    c.bind_names[b]: b.effective_value
                    for b in c.post_compile_params
                },
                check_post_param,
            )
        if check_param_order and getattr(c, "params", None):

            def get_dialect(paramstyle, positional):
                cp = copy(dialect)
                cp.paramstyle = paramstyle
                cp.positional = positional
                return cp

            pyformat_dialect = get_dialect("pyformat", False)
            pyformat_c = clause.compile(dialect=pyformat_dialect, **kw)
            stmt = re.sub(r"[\n\t]", "", str(pyformat_c))

            qmark_dialect = get_dialect("qmark", True)
            qmark_c = clause.compile(dialect=qmark_dialect, **kw)
            values = list(qmark_c.positiontup)
            escaped = qmark_c.escaped_bind_names

            for post_param in (
                qmark_c.post_compile_params | qmark_c.literal_execute_params
            ):
                name = qmark_c.bind_names[post_param]
                if name in values:
                    values = [v for v in values if v != name]
            positions = []
            pos_by_value = defaultdict(list)
            for v in values:
                try:
                    if v in pos_by_value:
                        start = pos_by_value[v][-1]
                    else:
                        start = 0
                    esc = escaped.get(v, v)
                    pos = stmt.index("%%(%s)s" % (esc,), start) + 2
                    positions.append(pos)
                    pos_by_value[v].append(pos)
                except ValueError:
                    msg = "Expected to find bindparam %r in %r" % (v, stmt)
                    assert False, msg

            ordered = all(
                positions[i - 1] < positions[i]
                for i in range(1, len(positions))
            )

            expected = [v for _, v in sorted(zip(positions, values))]

            msg = (
                "Order of parameters %s does not match the order "
                "in the statement %s. Statement %r" % (values, expected, stmt)
            )

            is_true(ordered, msg)


class ComparesTables:
    def assert_tables_equal(
        self,
        table,
        reflected_table,
        strict_types=False,
        strict_constraints=True,
    ):
        assert len(table.c) == len(reflected_table.c)
        for c, reflected_c in zip(table.c, reflected_table.c):
            eq_(c.name, reflected_c.name)
            assert reflected_c is reflected_table.c[c.name]

            if strict_constraints:
                eq_(c.primary_key, reflected_c.primary_key)
                eq_(c.nullable, reflected_c.nullable)

            if strict_types:
                msg = "Type '%s' doesn't correspond to type '%s'"
                assert isinstance(reflected_c.type, type(c.type)), msg % (
                    reflected_c.type,
                    c.type,
                )
            else:
                self.assert_types_base(reflected_c, c)

            if isinstance(c.type, sqltypes.String):
                eq_(c.type.length, reflected_c.type.length)

            if strict_constraints:
                eq_(
                    {f.column.name for f in c.foreign_keys},
                    {f.column.name for f in reflected_c.foreign_keys},
                )
            if c.server_default:
                assert isinstance(
                    reflected_c.server_default, schema.FetchedValue
                )

        if strict_constraints:
            assert len(table.primary_key) == len(reflected_table.primary_key)
            for c in table.primary_key:
                assert reflected_table.primary_key.columns[c.name] is not None

    def assert_types_base(self, c1, c2):
        assert c1.type._compare_type_affinity(
            c2.type
        ), "On column %r, type '%s' doesn't correspond to type '%s'" % (
            c1.name,
            c1.type,
            c2.type,
        )


class AssertsExecutionResults:
    def assert_result(self, result, class_, *objects):
        result = list(result)
        print(repr(result))
        self.assert_list(result, class_, objects)

    def assert_list(self, result, class_, list_):
        self.assert_(
            len(result) == len(list_),
            "result list is not the same size as test list, "
            + "for class "
            + class_.__name__,
        )
        for i in range(0, len(list_)):
            self.assert_row(class_, result[i], list_[i])

    def assert_row(self, class_, rowobj, desc):
        self.assert_(
            rowobj.__class__ is class_, "item class is not " + repr(class_)
        )
        for key, value in desc.items():
            if isinstance(value, tuple):
                if isinstance(value[1], list):
                    self.assert_list(getattr(rowobj, key), value[0], value[1])
                else:
                    self.assert_row(value[0], getattr(rowobj, key), value[1])
            else:
                self.assert_(
                    getattr(rowobj, key) == value,
                    "attribute %s value %s does not match %s"
                    % (key, getattr(rowobj, key), value),
                )

    def assert_unordered_result(self, result, cls, *expected):
        """As assert_result, but the order of objects is not considered.

        The algorithm is very expensive but not a big deal for the small
        numbers of rows that the test suite manipulates.
        """

        class immutabledict(dict):
            def __hash__(self):
                return id(self)

        found = util.IdentitySet(result)
        expected = {immutabledict(e) for e in expected}

        for wrong in filterfalse(lambda o: isinstance(o, cls), found):
            fail(
                'Unexpected type "%s", expected "%s"'
                % (type(wrong).__name__, cls.__name__)
            )

        if len(found) != len(expected):
            fail(
                'Unexpected object count "%s", expected "%s"'
                % (len(found), len(expected))
            )

        NOVALUE = object()

        def _compare_item(obj, spec):
            for key, value in spec.items():
                if isinstance(value, tuple):
                    try:
                        self.assert_unordered_result(
                            getattr(obj, key), value[0], *value[1]
                        )
                    except AssertionError:
                        return False
                else:
                    if getattr(obj, key, NOVALUE) != value:
                        return False
            return True

        for expected_item in expected:
            for found_item in found:
                if _compare_item(found_item, expected_item):
                    found.remove(found_item)
                    break
            else:
                fail(
                    "Expected %s instance with attributes %s not found."
                    % (cls.__name__, repr(expected_item))
                )
        return True

    def sql_execution_asserter(self, db=None):
        if db is None:
            from . import db as db

        return assertsql.assert_engine(db)

    def assert_sql_execution(self, db, callable_, *rules):
        with self.sql_execution_asserter(db) as asserter:
            result = callable_()
        asserter.assert_(*rules)
        return result

    def assert_sql(self, db, callable_, rules):

        newrules = []
        for rule in rules:
            if isinstance(rule, dict):
                newrule = assertsql.AllOf(
                    *[assertsql.CompiledSQL(k, v) for k, v in rule.items()]
                )
            else:
                newrule = assertsql.CompiledSQL(*rule)
            newrules.append(newrule)

        return self.assert_sql_execution(db, callable_, *newrules)

    def assert_sql_count(self, db, callable_, count):
        return self.assert_sql_execution(
            db, callable_, assertsql.CountStatements(count)
        )

    def assert_multiple_sql_count(self, dbs, callable_, counts):
        recs = [
            (self.sql_execution_asserter(db), db, count)
            for (db, count) in zip(dbs, counts)
        ]
        asserters = []
        for ctx, db, count in recs:
            asserters.append(ctx.__enter__())
        try:
            return callable_()
        finally:
            for asserter, (ctx, db, count) in zip(asserters, recs):
                ctx.__exit__(None, None, None)
                asserter.assert_(assertsql.CountStatements(count))

    @contextlib.contextmanager
    def assert_execution(self, db, *rules):
        with self.sql_execution_asserter(db) as asserter:
            yield
        asserter.assert_(*rules)

    def assert_statement_count(self, db, count):
        return self.assert_execution(db, assertsql.CountStatements(count))


class ComparesIndexes:
    def compare_table_index_with_expected(
        self, table: schema.Table, expected: list, dialect_name: str
    ):
        eq_(len(table.indexes), len(expected))
        idx_dict = {idx.name: idx for idx in table.indexes}
        for exp in expected:
            idx = idx_dict[exp["name"]]
            eq_(idx.unique, exp["unique"])
            cols = [c for c in exp["column_names"] if c is not None]
            eq_(len(idx.columns), len(cols))
            for c in cols:
                is_true(c in idx.columns)
            exprs = exp.get("expressions")
            if exprs:
                eq_(len(idx.expressions), len(exprs))
                for idx_exp, expr, col in zip(
                    idx.expressions, exprs, exp["column_names"]
                ):
                    if col is None:
                        eq_(idx_exp.text, expr)
            if (
                exp.get("dialect_options")
                and f"{dialect_name}_include" in exp["dialect_options"]
            ):
                eq_(
                    idx.dialect_options[dialect_name]["include"],
                    exp["dialect_options"][f"{dialect_name}_include"],
                )
