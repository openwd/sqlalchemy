# oracle/base.py
# Copyright (C) 2005, 2006, 2007, 2008, 2009 Michael Bayer mike_mp@zzzcomputing.com
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php
"""Support for the Oracle database.

Oracle version 8 through current (11g at the time of this writing) are supported.

For information on connecting via specific drivers, see the documentation
for that driver.

Connect Arguments
-----------------

The dialect supports several :func:`~sqlalchemy.create_engine()` arguments which 
affect the behavior of the dialect regardless of driver in use.

* *use_ansi* - Use ANSI JOIN constructs (see the section on Oracle 8).  Defaults
  to ``True``.  If ``False``, Oracle-8 compatible constructs are used for joins.

* *optimize_limits* - defaults to ``False``. see the section on LIMIT/OFFSET.

Auto Increment Behavior
-----------------------

SQLAlchemy Table objects which include integer primary keys are usually assumed to have
"autoincrementing" behavior, meaning they can generate their own primary key values upon
INSERT.  Since Oracle has no "autoincrement" feature, SQLAlchemy relies upon sequences 
to produce these values.   With the Oracle dialect, *a sequence must always be explicitly
specified to enable autoincrement*.  This is divergent with the majority of documentation 
examples which assume the usage of an autoincrement-capable database.   To specify sequences,
use the sqlalchemy.schema.Sequence object which is passed to a Column construct::

  t = Table('mytable', metadata, 
        Column('id', Integer, Sequence('id_seq'), primary_key=True),
        Column(...), ...
  )

This step is also required when using table reflection, i.e. autoload=True::

  t = Table('mytable', metadata, 
        Column('id', Integer, Sequence('id_seq'), primary_key=True),
        autoload=True
  ) 

Identifier Casing
-----------------

In Oracle, the data dictionary represents all case insensitive identifier names 
using UPPERCASE text.   SQLAlchemy on the other hand considers an all-lower case identifier
name to be case insensitive.   The Oracle dialect converts all case insensitive identifiers
to and from those two formats during schema level communication, such as reflection of
tables and indexes.   Using an UPPERCASE name on the SQLAlchemy side indicates a 
case sensitive identifier, and SQLAlchemy will quote the name - this will cause mismatches
against data dictionary data received from Oracle, so unless identifier names have been
truly created as case sensitive (i.e. using quoted names), all lowercase names should be
used on the SQLAlchemy side.

Unicode
-------

SQLAlchemy 0.6 uses the "native unicode" mode provided as of cx_oracle 5.  cx_oracle 5.0.2
or greater is recommended for support of NCLOB.   If not using cx_oracle 5, the NLS_LANG
environment variable needs to be set in order for the oracle client library to use 
proper encoding, such as "AMERICAN_AMERICA.UTF8".

Also note that Oracle supports unicode data through the NVARCHAR and NCLOB data types.
When using the SQLAlchemy Unicode and UnicodeText types, these DDL types will be used
within CREATE TABLE statements.   Usage of VARCHAR2 and CLOB with unicode text still 
requires NLS_LANG to be set.

LIMIT/OFFSET Support
--------------------

Oracle has no support for the LIMIT or OFFSET keywords.  Whereas previous versions of SQLAlchemy
used the "ROW NUMBER OVER..." construct to simulate LIMIT/OFFSET, SQLAlchemy 0.5 now uses 
a wrapped subquery approach in conjunction with ROWNUM.  The exact methodology is taken from
http://www.oracle.com/technology/oramag/oracle/06-sep/o56asktom.html .  Note that the 
"FIRST ROWS()" optimization keyword mentioned is not used by default, as the user community felt
this was stepping into the bounds of optimization that is better left on the DBA side, but this
prefix can be added by enabling the optimize_limits=True flag on create_engine().

ON UPDATE CASCADE
-----------------

Oracle doesn't have native ON UPDATE CASCADE functionality.  A trigger based solution 
is available at http://asktom.oracle.com/tkyte/update_cascade/index.html .

When using the SQLAlchemy ORM, the ORM has limited ability to manually issue
cascading updates - specify ForeignKey objects using the 
"deferrable=True, initially='deferred'" keyword arguments,
and specify "passive_updates=False" on each relation().

Oracle 8 Compatibility
----------------------

When using Oracle 8, a "use_ansi=False" flag is available which converts all
JOIN phrases into the WHERE clause, and in the case of LEFT OUTER JOIN
makes use of Oracle's (+) operator.

Synonym/DBLINK Reflection
-------------------------

When using reflection with Table objects, the dialect can optionally search for tables
indicated by synonyms that reference DBLINK-ed tables by passing the flag 
oracle_resolve_synonyms=True as a keyword argument to the Table construct.  If DBLINK 
is not in use this flag should be left off.

"""

import random, re

from sqlalchemy import schema as sa_schema
from sqlalchemy import util, sql, log
from sqlalchemy.engine import default, base, reflection
from sqlalchemy.sql import compiler, visitors, expression
from sqlalchemy.sql import operators as sql_operators, functions as sql_functions
from sqlalchemy import types as sqltypes
from sqlalchemy.types import VARCHAR, NVARCHAR, CHAR, DATE, DATETIME, \
                BLOB, CLOB, TIMESTAMP, FLOAT
                
RESERVED_WORDS = set('''SHARE RAW DROP BETWEEN FROM DESC OPTION PRIOR LONG THEN DEFAULT ALTER IS INTO MINUS INTEGER NUMBER GRANT IDENTIFIED ALL TO ORDER ON FLOAT DATE HAVING CLUSTER NOWAIT RESOURCE ANY TABLE INDEX FOR UPDATE WHERE CHECK SMALLINT WITH DELETE BY ASC REVOKE LIKE SIZE RENAME NOCOMPRESS NULL GROUP VALUES AS IN VIEW EXCLUSIVE COMPRESS SYNONYM SELECT INSERT EXISTS NOT TRIGGER ELSE CREATE INTERSECT PCTFREE DISTINCT USER CONNECT SET MODE OF UNIQUE VARCHAR2 VARCHAR LOCK OR CHAR DECIMAL UNION PUBLIC AND START UID COMMENT'''.split()) 

class RAW(sqltypes.Binary):
    pass
OracleRaw = RAW

class NCLOB(sqltypes.Text):
    __visit_name__ = 'NCLOB'

VARCHAR2 = VARCHAR
NVARCHAR2 = NVARCHAR

class NUMBER(sqltypes.Numeric):
    __visit_name__ = 'NUMBER'
    
class BFILE(sqltypes.Binary):
    __visit_name__ = 'BFILE'

class DOUBLE_PRECISION(sqltypes.Numeric):
    __visit_name__ = 'DOUBLE_PRECISION'

class LONG(sqltypes.Text):
    __visit_name__ = 'LONG'
    
class _OracleBoolean(sqltypes.Boolean):
    def get_dbapi_type(self, dbapi):
        return dbapi.NUMBER
    
    def result_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            return value and True or False
        return process

    def bind_processor(self, dialect):
        def process(value):
            if value is True:
                return 1
            elif value is False:
                return 0
            elif value is None:
                return None
            else:
                return value and True or False
        return process

colspecs = {
    sqltypes.Boolean : _OracleBoolean,
}

ischema_names = {
    'VARCHAR2' : VARCHAR,
    'NVARCHAR2' : NVARCHAR,
    'CHAR' : CHAR,
    'DATE' : DATE,
    'DATETIME' : DATETIME,
    'NUMBER' : NUMBER,
    'BLOB' : BLOB,
    'BFILE' : BFILE,
    'CLOB' : CLOB,
    'NCLOB' : NCLOB,
    'TIMESTAMP' : TIMESTAMP,
    'RAW' : RAW,
    'FLOAT' : FLOAT,
    'DOUBLE PRECISION' : DOUBLE_PRECISION,
    'LONG' : LONG,
}


class OracleTypeCompiler(compiler.GenericTypeCompiler):
    # Note:
    # Oracle DATE == DATETIME
    # Oracle does not allow milliseconds in DATE
    # Oracle does not support TIME columns
    
    def visit_datetime(self, type_):
        return self.visit_DATE(type_)
    
    def visit_float(self, type_):
        if type_.precision is None:
            return "NUMERIC"
        else:
            return "NUMERIC(%(precision)s, %(scale)s)" % {'precision': type_.precision, 'scale' : 2}
        
    def visit_unicode(self, type_):
        return self.visit_NVARCHAR(type_)
        
    def visit_VARCHAR(self, type_):
        return "VARCHAR(%(length)s)" % {'length' : type_.length}

    def visit_NVARCHAR(self, type_):
        return "NVARCHAR2(%(length)s)" % {'length' : type_.length}
    
    def visit_text(self, type_):
        return self.visit_CLOB(type_)

    def visit_unicode_text(self, type_):
        return self.visit_NCLOB(type_)

    def visit_binary(self, type_):
        return self.visit_BLOB(type_)
    
    def visit_boolean(self, type_):
        return self.visit_SMALLINT(type_)
    
    def visit_RAW(self, type_):
        return "RAW(%(length)s)" % {'length' : type_.length}

class OracleCompiler(compiler.SQLCompiler):
    """Oracle compiler modifies the lexical structure of Select
    statements to work under non-ANSI configured Oracle databases, if
    the use_ansi flag is False.
    """

    def __init__(self, *args, **kwargs):
        super(OracleCompiler, self).__init__(*args, **kwargs)
        self.__wheres = {}
        self._quoted_bind_names = {}

    def visit_mod(self, binary, **kw):
        return "mod(%s, %s)" % (self.process(binary.left), self.process(binary.right))
    
    def visit_now_func(self, fn, **kw):
        return "CURRENT_TIMESTAMP"
    
    def visit_char_length_func(self, fn, **kw):
        return "LENGTH" + self.function_argspec(fn, **kw)
        
    def visit_match_op(self, binary, **kw):
        return "CONTAINS (%s, %s)" % (self.process(binary.left), self.process(binary.right))
    
    def function_argspec(self, fn, **kw):
        if len(fn.clauses) > 0:
            return compiler.SQLCompiler.function_argspec(self, fn, **kw)
        else:
            return ""
        
    def bindparam_string(self, name):
        # TODO: its not clear how much of bind parameter quoting is "Oracle"
        # and how much is "cx_Oracle".
        if self.preparer._bindparam_requires_quotes(name):
            quoted_name = '"%s"' % name
            self._quoted_bind_names[name] = quoted_name
            return compiler.SQLCompiler.bindparam_string(self, quoted_name)
        else:
            return compiler.SQLCompiler.bindparam_string(self, name)

    def default_from(self):
        """Called when a ``SELECT`` statement has no froms, and no ``FROM`` clause is to be appended.

        The Oracle compiler tacks a "FROM DUAL" to the statement.
        """

        return " FROM DUAL"

    def visit_join(self, join, **kwargs):
        if self.dialect.use_ansi:
            return compiler.SQLCompiler.visit_join(self, join, **kwargs)
        else:
            return self.process(join.left, asfrom=True) + ", " + self.process(join.right, asfrom=True)

    def _get_nonansi_join_whereclause(self, froms):
        clauses = []

        def visit_join(join):
            if join.isouter:
                def visit_binary(binary):
                    if binary.operator == sql_operators.eq:
                        if binary.left.table is join.right:
                            binary.left = _OuterJoinColumn(binary.left)
                        elif binary.right.table is join.right:
                            binary.right = _OuterJoinColumn(binary.right)
                clauses.append(visitors.cloned_traverse(join.onclause, {}, {'binary':visit_binary}))
            else:
                clauses.append(join.onclause)

        for f in froms:
            visitors.traverse(f, {}, {'join':visit_join})
        return sql.and_(*clauses)

    def visit_outer_join_column(self, vc):
        return self.process(vc.column) + "(+)"

    def visit_sequence(self, seq):
        return self.dialect.identifier_preparer.format_sequence(seq) + ".nextval"

    def visit_alias(self, alias, asfrom=False, **kwargs):
        """Oracle doesn't like ``FROM table AS alias``.  Is the AS standard SQL??"""

        if asfrom:
            alias_name = isinstance(alias.name, expression._generated_label) and \
                            self._truncated_identifier("alias", alias.name) or alias.name
            
            return self.process(alias.original, asfrom=asfrom, **kwargs) + " " + self.preparer.format_alias(alias, alias_name)
        else:
            return self.process(alias.original, **kwargs)

    def returning_clause(self, stmt):
        returning_cols = stmt._returning
            
        def create_out_param(col, i):
            bindparam = sql.outparam("ret_%d" % i, type_=col.type)
            self.binds[bindparam.key] = bindparam
            return self.bindparam_string(self._truncate_bindparam(bindparam))
        
        columnlist = list(expression._select_iterables(returning_cols))
        
        # within_columns_clause =False so that labels (foo AS bar) don't render
        columns = [self.process(c, within_columns_clause=False) for c in columnlist]
        
        binds = [create_out_param(c, i) for i, c in enumerate(columnlist)]
        
        return 'RETURNING ' + ', '.join(columns) +  " INTO " + ", ".join(binds)

    def _TODO_visit_compound_select(self, select):
        """Need to determine how to get ``LIMIT``/``OFFSET`` into a ``UNION`` for Oracle."""
        pass

    def visit_select(self, select, **kwargs):
        """Look for ``LIMIT`` and OFFSET in a select statement, and if
        so tries to wrap it in a subquery with ``rownum`` criterion.
        """

        if not getattr(select, '_oracle_visit', None):
            if not self.dialect.use_ansi:
                if self.stack and 'from' in self.stack[-1]:
                    existingfroms = self.stack[-1]['from']
                else:
                    existingfroms = None

                froms = select._get_display_froms(existingfroms)
                whereclause = self._get_nonansi_join_whereclause(froms)
                if whereclause:
                    select = select.where(whereclause)
                    select._oracle_visit = True

            if select._limit is not None or select._offset is not None:
                # See http://www.oracle.com/technology/oramag/oracle/06-sep/o56asktom.html
                #
                # Generalized form of an Oracle pagination query:
                #   select ... from (
                #     select /*+ FIRST_ROWS(N) */ ...., rownum as ora_rn from (
                #         select distinct ... where ... order by ...
                #     ) where ROWNUM <= :limit+:offset
                #   ) where ora_rn > :offset
                # Outer select and "ROWNUM as ora_rn" can be dropped if limit=0

                # TODO: use annotations instead of clone + attr set ?
                select = select._generate()
                select._oracle_visit = True

                # Wrap the middle select and add the hint
                limitselect = sql.select([c for c in select.c])
                if select._limit and self.dialect.optimize_limits:
                    limitselect = limitselect.prefix_with("/*+ FIRST_ROWS(%d) */" % select._limit)

                limitselect._oracle_visit = True
                limitselect._is_wrapper = True

                # If needed, add the limiting clause
                if select._limit is not None:
                    max_row = select._limit
                    if select._offset is not None:
                        max_row += select._offset
                    limitselect.append_whereclause(
                            sql.literal_column("ROWNUM")<=max_row)

                # If needed, add the ora_rn, and wrap again with offset.
                if select._offset is None:
                    select = limitselect
                else:
                     limitselect = limitselect.column(
                             sql.literal_column("ROWNUM").label("ora_rn"))
                     limitselect._oracle_visit = True
                     limitselect._is_wrapper = True

                     offsetselect = sql.select(
                             [c for c in limitselect.c if c.key!='ora_rn'])
                     offsetselect._oracle_visit = True
                     offsetselect._is_wrapper = True

                     offsetselect.append_whereclause(
                             sql.literal_column("ora_rn")>select._offset)

                     select = offsetselect

        kwargs['iswrapper'] = getattr(select, '_is_wrapper', False)
        return compiler.SQLCompiler.visit_select(self, select, **kwargs)

    def limit_clause(self, select):
        return ""

    def for_update_clause(self, select):
        if select.for_update == "nowait":
            return " FOR UPDATE NOWAIT"
        else:
            return super(OracleCompiler, self).for_update_clause(select)

class OracleDDLCompiler(compiler.DDLCompiler):

    def visit_create_sequence(self, create):
        return "CREATE SEQUENCE %s" % self.preparer.format_sequence(create.element)

    def visit_drop_sequence(self, drop):
        return "DROP SEQUENCE %s" % self.preparer.format_sequence(drop.element)

    def define_constraint_cascades(self, constraint):
        text = ""
        if constraint.ondelete is not None:
            text += " ON DELETE %s" % constraint.ondelete
            
        # oracle has no ON UPDATE CASCADE - 
        # its only available via triggers http://asktom.oracle.com/tkyte/update_cascade/index.html
        if constraint.onupdate is not None:
            util.warn(
                "Oracle does not contain native UPDATE CASCADE "
                 "functionality - onupdates will not be rendered for foreign keys."
                 "Consider using deferrable=True, initially='deferred' or triggers.")
        
        return text

class OracleDefaultRunner(base.DefaultRunner):
    def visit_sequence(self, seq):
        return self.execute_string("SELECT " + 
                    self.dialect.identifier_preparer.format_sequence(seq) + 
                    ".nextval FROM DUAL", {})

class OracleIdentifierPreparer(compiler.IdentifierPreparer):
    
    reserved_words = set([x.lower() for x in RESERVED_WORDS])
    illegal_initial_characters = re.compile(r'[0-9_$]')

    def _bindparam_requires_quotes(self, value):
        """Return True if the given identifier requires quoting."""
        lc_value = value.lower()
        return (lc_value in self.reserved_words
                or self.illegal_initial_characters.match(value[0])
                or not self.legal_characters.match(unicode(value))
                )
    
    def format_savepoint(self, savepoint):
        name = re.sub(r'^_+', '', savepoint.ident)
        return super(OracleIdentifierPreparer, self).format_savepoint(savepoint, name)
        
class OracleDialect(default.DefaultDialect):
    name = 'oracle'
    supports_alter = True
    supports_unicode_statements = False
    supports_unicode_binds = False
    max_identifier_length = 30
    supports_sane_rowcount = True
    supports_sane_multi_rowcount = False
    supports_sequences = True
    sequences_optional = False
    preexecute_pk_sequences = True
    supports_pk_autoincrement = False
    default_paramstyle = 'named'
    colspecs = colspecs
    ischema_names = ischema_names
    requires_name_normalize = True
    
    supports_default_values = False
    supports_empty_insert = False
    
    statement_compiler = OracleCompiler
    ddl_compiler = OracleDDLCompiler
    type_compiler = OracleTypeCompiler
    preparer = OracleIdentifierPreparer
    defaultrunner = OracleDefaultRunner
    
    reflection_options = ('oracle_resolve_synonyms', )
    
    
    def __init__(self, 
                use_ansi=True, 
                optimize_limits=False, 
                **kwargs):
        default.DefaultDialect.__init__(self, **kwargs)
        self.use_ansi = use_ansi
        self.optimize_limits = optimize_limits

    def has_table(self, connection, table_name, schema=None):
        if not schema:
            schema = self.get_default_schema_name(connection)
        cursor = connection.execute("""select table_name from all_tables where table_name=:name and owner=:schema_name""", {'name':self.denormalize_name(table_name), 'schema_name':self.denormalize_name(schema)})
        return cursor.fetchone() is not None

    def has_sequence(self, connection, sequence_name, schema=None):
        if not schema:
            schema = self.get_default_schema_name(connection)
        cursor = connection.execute("""select sequence_name from all_sequences where sequence_name=:name and sequence_owner=:schema_name""", {'name':self.denormalize_name(sequence_name), 'schema_name':self.denormalize_name(schema)})
        return cursor.fetchone() is not None

    def normalize_name(self, name):
        if name is None:
            return None
        elif name.upper() == name and not self.identifier_preparer._requires_quotes(name.lower().decode(self.encoding)):
            return name.lower().decode(self.encoding)
        else:
            return name.decode(self.encoding)

    def denormalize_name(self, name):
        if name is None:
            return None
        elif name.lower() == name and not self.identifier_preparer._requires_quotes(name.lower()):
            return name.upper().encode(self.encoding)
        else:
            return name.encode(self.encoding)

    def get_default_schema_name(self, connection):
        return self.normalize_name(connection.execute('SELECT USER FROM DUAL').scalar())

    def table_names(self, connection, schema):
        # note that table_names() isnt loading DBLINKed or synonym'ed tables
        if schema is None:
            s = "select table_name from all_tables where nvl(tablespace_name, 'no tablespace') NOT IN ('SYSTEM', 'SYSAUX')"
            cursor = connection.execute(s)
        else:
            s = "select table_name from all_tables where nvl(tablespace_name, 'no tablespace') NOT IN ('SYSTEM','SYSAUX') AND OWNER = :owner"
            cursor = connection.execute(s, {'owner': self.denormalize_name(schema)})
        return [self.normalize_name(row[0]) for row in cursor]

    def _resolve_synonym(self, connection, desired_owner=None, desired_synonym=None, desired_table=None):
        """search for a local synonym matching the given desired owner/name.

        if desired_owner is None, attempts to locate a distinct owner.

        returns the actual name, owner, dblink name, and synonym name if found.
        """

        sql = """select OWNER, TABLE_OWNER, TABLE_NAME, DB_LINK, SYNONYM_NAME
                   from   ALL_SYNONYMS WHERE """

        clauses = []
        params = {}
        if desired_synonym:
            clauses.append("SYNONYM_NAME=:synonym_name")
            params['synonym_name'] = desired_synonym
        if desired_owner:
            clauses.append("TABLE_OWNER=:desired_owner")
            params['desired_owner'] = desired_owner
        if desired_table:
            clauses.append("TABLE_NAME=:tname")
            params['tname'] = desired_table

        sql += " AND ".join(clauses)

        result = connection.execute(sql, **params)
        if desired_owner:
            row = result.fetchone()
            if row:
                return row['TABLE_NAME'], row['TABLE_OWNER'], row['DB_LINK'], row['SYNONYM_NAME']
            else:
                return None, None, None, None
        else:
            rows = result.fetchall()
            if len(rows) > 1:
                raise AssertionError("There are multiple tables visible to the schema, you must specify owner")
            elif len(rows) == 1:
                row = rows[0]
                return row['TABLE_NAME'], row['TABLE_OWNER'], row['DB_LINK'], row['SYNONYM_NAME']
            else:
                return None, None, None, None

    @reflection.cache
    def _prepare_reflection_args(self, connection, table_name, schema=None,
                                 resolve_synonyms=False, dblink='', **kw):

        if resolve_synonyms:
            actual_name, owner, dblink, synonym = self._resolve_synonym(connection, desired_owner=self.denormalize_name(schema), desired_synonym=self.denormalize_name(table_name))
        else:
            actual_name, owner, dblink, synonym = None, None, None, None
        if not actual_name:
            actual_name = self.denormalize_name(table_name)
        if not dblink:
            dblink = ''
        if not owner:
            owner = self.denormalize_name(schema or self.get_default_schema_name(connection))
        return (actual_name, owner, dblink, synonym)

    @reflection.cache
    def get_schema_names(self, connection, **kw):
        s = "SELECT username FROM all_users ORDER BY username"
        cursor = connection.execute(s,)
        return [self.normalize_name(row[0]) for row in cursor]

    @reflection.cache
    def get_table_names(self, connection, schema=None, **kw):
        schema = self.denormalize_name(schema or self.get_default_schema_name(connection))
        return self.table_names(connection, schema)

    @reflection.cache
    def get_view_names(self, connection, schema=None, **kw):
        schema = self.denormalize_name(schema or self.get_default_schema_name(connection))
        s = "select view_name from all_views where OWNER = :owner"
        cursor = connection.execute(s,
                {'owner':self.denormalize_name(schema)})
        return [self.normalize_name(row[0]) for row in cursor]

    @reflection.cache
    def get_columns(self, connection, table_name, schema=None, **kw):
        """

        kw arguments can be:

            oracle_resolve_synonyms

            dblink

        """

        resolve_synonyms = kw.get('oracle_resolve_synonyms', False)
        dblink = kw.get('dblink', '')
        info_cache = kw.get('info_cache')

        (table_name, schema, dblink, synonym) = \
            self._prepare_reflection_args(connection, table_name, schema,
                                          resolve_synonyms, dblink,
                                          info_cache=info_cache)
        columns = []
        c = connection.execute ("select COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, "
                                "DATA_SCALE, NULLABLE, DATA_DEFAULT from ALL_TAB_COLUMNS%(dblink)s "
                                "where TABLE_NAME = :table_name and OWNER = :owner" % 
                                {'dblink':dblink}, {'table_name':table_name, 'owner':schema}
                                )

        for row in c:

            (colname, coltype, length, precision, scale, nullable, default) = \
                (self.normalize_name(row[0]), row[1], row[2], row[3], row[4], row[5]=='Y', row[6])

            # INTEGER if the scale is 0 and precision is null
            # NUMBER if the scale and precision are both null
            # NUMBER(9,2) if the precision is 9 and the scale is 2
            # NUMBER(3) if the precision is 3 and scale is 0
            #length is ignored except for CHAR and VARCHAR2
            if coltype == 'NUMBER' :
                if precision is None and scale is None:
                    coltype = sqltypes.NUMERIC
                elif precision is None and scale == 0:
                    coltype = sqltypes.INTEGER
                else :
                    coltype = sqltypes.NUMERIC(precision, scale)
            elif coltype=='CHAR' or coltype=='VARCHAR2':
                coltype = self.ischema_names.get(coltype)(length)
            else:
                coltype = re.sub(r'\(\d+\)', '', coltype)
                try:
                    coltype = self.ischema_names[coltype]
                except KeyError:
                    util.warn("Did not recognize type '%s' of column '%s'" %
                              (coltype, colname))
                    coltype = sqltypes.NULLTYPE

            cdict = {
                'name': colname,
                'type': coltype,
                'nullable': nullable,
                'default': default,
            }
            columns.append(cdict)
        return columns

    @reflection.cache
    def get_indexes(self, connection, table_name, schema=None,
                    resolve_synonyms=False, dblink='', **kw):

        
        info_cache = kw.get('info_cache')
        (table_name, schema, dblink, synonym) = \
            self._prepare_reflection_args(connection, table_name, schema,
                                          resolve_synonyms, dblink,
                                          info_cache=info_cache)
        indexes = []
        q = """
        SELECT a.INDEX_NAME, a.COLUMN_NAME, b.UNIQUENESS
        FROM ALL_IND_COLUMNS%(dblink)s a
        INNER JOIN ALL_INDEXES%(dblink)s b
            ON a.INDEX_NAME = b.INDEX_NAME
            AND a.TABLE_OWNER = b.TABLE_OWNER
            AND a.TABLE_NAME = b.TABLE_NAME
        WHERE a.TABLE_NAME = :table_name
        AND a.TABLE_OWNER = :schema
        ORDER BY a.INDEX_NAME, a.COLUMN_POSITION
        """ % dict(dblink=dblink)
        rp = connection.execute(q,
            dict(table_name=self.denormalize_name(table_name),
                 schema=self.denormalize_name(schema)))
        indexes = []
        last_index_name = None
        pkeys = self.get_primary_keys(connection, table_name, schema,
                                      resolve_synonyms=resolve_synonyms,
                                      dblink=dblink,
                                      info_cache=kw.get('info_cache'))
        uniqueness = dict(NONUNIQUE=False, UNIQUE=True)
        for rset in rp:
            # don't include the primary key columns
            if rset.column_name in [s.upper() for s in pkeys]:
                continue
            if rset.index_name != last_index_name:
                index = dict(name=self.normalize_name(rset.index_name), column_names=[])
                indexes.append(index)
            index['unique'] = uniqueness.get(rset.uniqueness, False)
            index['column_names'].append(self.normalize_name(rset.column_name))
            last_index_name = rset.index_name
        return indexes

    @reflection.cache
    def _get_constraint_data(self, connection, table_name, schema=None,
                            dblink='', **kw):

        rp = connection.execute("""SELECT
             ac.constraint_name,
             ac.constraint_type,
             loc.column_name AS local_column,
             rem.table_name AS remote_table,
             rem.column_name AS remote_column,
             rem.owner AS remote_owner,
             loc.position as loc_pos,
             rem.position as rem_pos
           FROM all_constraints%(dblink)s ac,
             all_cons_columns%(dblink)s loc,
             all_cons_columns%(dblink)s rem
           WHERE ac.table_name = :table_name
           AND ac.constraint_type IN ('R','P')
           AND ac.owner = :owner
           AND ac.owner = loc.owner
           AND ac.constraint_name = loc.constraint_name
           AND ac.r_owner = rem.owner(+)
           AND ac.r_constraint_name = rem.constraint_name(+)
           AND (rem.position IS NULL or loc.position=rem.position)
           ORDER BY ac.constraint_name, loc.position"""
           
         % {'dblink':dblink}, {'table_name' : table_name, 'owner' : schema})
        constraint_data = rp.fetchall()
        return constraint_data

    @reflection.cache
    def get_primary_keys(self, connection, table_name, schema=None, **kw):
        """

        kw arguments can be:

            oracle_resolve_synonyms

            dblink

        """

        resolve_synonyms = kw.get('oracle_resolve_synonyms', False)
        dblink = kw.get('dblink', '')
        info_cache = kw.get('info_cache')

        (table_name, schema, dblink, synonym) = \
            self._prepare_reflection_args(connection, table_name, schema,
                                          resolve_synonyms, dblink,
                                          info_cache=info_cache)
        pkeys = []
        constraint_data = self._get_constraint_data(connection, table_name,
                                        schema, dblink,
                                        info_cache=kw.get('info_cache'))
                                        
        for row in constraint_data:
            #print "ROW:" , row
            (cons_name, cons_type, local_column, remote_table, remote_column, remote_owner) = \
                row[0:2] + tuple([self.normalize_name(x) for x in row[2:6]])
            if cons_type == 'P':
                pkeys.append(local_column)
        return pkeys

    @reflection.cache
    def get_foreign_keys(self, connection, table_name, schema=None, **kw):
        """

        kw arguments can be:

            oracle_resolve_synonyms

            dblink

        """

        requested_schema = schema # to check later on
        resolve_synonyms = kw.get('oracle_resolve_synonyms', False)
        dblink = kw.get('dblink', '')
        info_cache = kw.get('info_cache')

        (table_name, schema, dblink, synonym) = \
            self._prepare_reflection_args(connection, table_name, schema,
                                          resolve_synonyms, dblink,
                                          info_cache=info_cache)

        constraint_data = self._get_constraint_data(connection, table_name,
                                                schema, dblink,
                                                info_cache=kw.get('info_cache'))

        def fkey_rec():
            return {
                'name' : None,
                'constrained_columns' : [],
                'referred_schema' : None,
                'referred_table' : None,
                'referred_columns' : []
            }

        fkeys = util.defaultdict(fkey_rec)
        
        for row in constraint_data:
            (cons_name, cons_type, local_column, remote_table, remote_column, remote_owner) = \
                    row[0:2] + tuple([self.normalize_name(x) for x in row[2:6]])

            if cons_type == 'R':
                if remote_table is None:
                    # ticket 363
                    util.warn(
                        ("Got 'None' querying 'table_name' from "
                         "all_cons_columns%(dblink)s - does the user have "
                         "proper rights to the table?") % {'dblink':dblink})
                    continue

                rec = fkeys[cons_name]
                rec['name'] = cons_name
                local_cols, remote_cols = rec['constrained_columns'], rec['referred_columns']

                if not rec['referred_table']:
                    if resolve_synonyms:
                        ref_remote_name, ref_remote_owner, ref_dblink, ref_synonym = \
                                self._resolve_synonym(
                                    connection, 
                                    desired_owner=self.denormalize_name(remote_owner), 
                                    desired_table=self.denormalize_name(remote_table)
                                )
                        if ref_synonym:
                            remote_table = self.normalize_name(ref_synonym)
                            remote_owner = self.normalize_name(ref_remote_owner)
                    
                    rec['referred_table'] = remote_table
                    
                    if requested_schema is not None or self.denormalize_name(remote_owner) != schema:
                        rec['referred_schema'] = remote_owner
                
                local_cols.append(local_column)
                remote_cols.append(remote_column)

        return fkeys.values()

    @reflection.cache
    def get_view_definition(self, connection, view_name, schema=None,
                            resolve_synonyms=False, dblink='', **kw):
        info_cache = kw.get('info_cache')
        (view_name, schema, dblink, synonym) = \
            self._prepare_reflection_args(connection, view_name, schema,
                                          resolve_synonyms, dblink,
                                          info_cache=info_cache)
        s = """
        SELECT text FROM all_views
        WHERE owner = :schema
        AND view_name = :view_name
        """
        rp = connection.execute(s,
                                view_name=view_name, schema=schema).scalar()
        if rp:
            return rp.decode(self.encoding)
        else:
            return None



class _OuterJoinColumn(sql.ClauseElement):
    __visit_name__ = 'outer_join_column'
    
    def __init__(self, column):
        self.column = column



