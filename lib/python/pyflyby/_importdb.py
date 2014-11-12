# pyflyby/_importdb.py.
# Copyright (C) 2011, 2012, 2013, 2014 Karl Chen.
# License: MIT http://opensource.org/licenses/MIT

from __future__ import absolute_import, division, with_statement

from   collections              import defaultdict
import os
import re

from   pyflyby._file            import Filename, expand_py_files_from_args
from   pyflyby._idents          import dotted_prefixes
from   pyflyby._importclns      import ImportMap, ImportSet
from   pyflyby._importstmt      import Import, ImportStatement
from   pyflyby._log             import logger
from   pyflyby._parse           import PythonBlock
from   pyflyby._util            import cached_attribute, memoize, stable_unique


@memoize
def _find_etc_dir():
    dir = Filename(__file__).real.dir
    while True:
        candidate = dir / "etc/pyflyby"
        if candidate.isdir:
            return candidate
        parent = dir.dir
        if parent == dir:
            break
        dir = parent
    return None



def _get_env_var(env_var_name, default):
    '''
    Get an environment variable and split on ":", replacing C{-} with the
    default.
    '''
    assert re.match("^[A-Z_]+$", env_var_name)
    assert isinstance(default, (tuple, list))
    value = filter(None, os.environ.get(env_var_name, '').split(':'))
    if not value:
        return default
    # Replace '-' with C{default}
    try:
        idx = value.index('-')
    except ValueError:
        pass
    else:
        value[idx:idx+1] = default
    return value


def _get_python_path(env_var_name, default_path, target_dirname):
    '''
    Expand an environment variable specifying pyflyby input config files.

      - Default to C{default_path} if the environment variable is undefined.
      - Process colon delimiters.
      - Replace "-" with C{default_path}.
      - Expand triple dots.
      - Recursively traverse directories.

    @rtype:
      C{tuple} of C{Filename}s
    '''
    pathnames = _get_env_var(env_var_name, default_path)
    pathnames = [os.path.expanduser(p) for p in pathnames]
    pathnames = _expand_tripledots(pathnames, target_dirname)
    pathnames = [Filename(fn) for fn in pathnames]
    pathnames = stable_unique(pathnames)
    pathnames = expand_py_files_from_args(pathnames)
    if not pathnames:
        logger.warning(
            "No import libraries found (%s=%r, default=%r)"
            % (env_var_name, os.environ.get(env_var_name), default_path))
    return tuple(pathnames)


# TODO: stop memoizing here after using StatCache.  Actually just inline into
# _ancestors_on_same_partition
@memoize
def _get_st_dev(filename):
    filename = Filename(filename)
    try:
        return os.stat(str(filename)).st_dev
    except OSError:
        return None


def _ancestors_on_same_partition(filename):
    """
    Generate ancestors of C{filename} that exist and are on the same partition
    as the first existing ancestor of C{filename}.

    For example, suppose a partition is mounted on /u/homer; /u is a different
    partition.  Suppose /u/homer/aa exists but /u/homer/aa/bb does not exist.
    Then::
      >> _ancestors_on_same_partition("/u/homer/aa/bb/cc")
      [Filename("/u/homer", Filename("/u/homer/aa")]

    @rtype:
      C{list} of C{Filename}
    """
    result = []
    dev = None
    for f in filename.ancestors:
        this_dev = _get_st_dev(f)
        if this_dev is None:
            continue
        if dev is None:
            dev = this_dev
        elif dev != this_dev:
            break
        result.append(f)
    return result


def _expand_tripledots(pathnames, target_dirname):
    """
    Expand pathnames of the form C{".../foo/bar"} as "../../foo/bar",
    "../foo/bar", "./foo/bar" etc., up to the oldest ancestor with the same
    st_dev.

    For example, suppose a partition is mounted on /u/homer; /u is a different
    partition.  Then::
      >> _expand_tripledots(["/foo", ".../tt"], "/u/homer/aa")
      [Filename("/foo"), Filename("/u/homer/tt"), Filename("/u/homer/aa/tt")]

    @type pathnames:
      sequence of C{str} (not C{Filename})
    @type target_dirname:
      L{Filename}
    @rtype:
      C{list} of L{Filename}
    """
    target_dirname = Filename(target_dirname)
    if not isinstance(pathnames, (tuple, list)):
        pathnames = [pathnames]
    result = []
    for pathname in pathnames:
        if not pathname.startswith(".../"):
            result.append(Filename(pathname))
            continue
        suffix = pathname[4:]
        expanded = [
            p / suffix for p in _ancestors_on_same_partition(target_dirname) ]
        result.extend(expanded[::-1])
    return result


class ImportDB(object):
    """
    A database of known, mandatory, canonical imports.

    @iattr known_imports:
      Set of known imports.  For use by tidy-imports and autoimporter.
    @iattr mandatory_imports:
      Set of imports that must be added by tidy-imports.
    @iattr canonical_imports:
      Map of imports that tidy-imports transforms on every run.
    @iattr forget_imports:
      Set of imports to remove from known_imports, mandatory_imports,
      canonical_imports.
    """

    def __new__(cls, *args):
        if len(args) != 1:
            raise TypeError
        arg, = args
        if isinstance(arg, cls):
            return arg
        if isinstance(arg, ImportSet):
            return cls._from_data(arg, [], [], [])
        return cls._from_args(arg) # PythonBlock, Filename, etc


    _default_cache = {}

    @classmethod
    def get_default(cls, target_filename):
        """
        Return the default import library for the given target filename.

        This will read various .../.pyflyby files as specified by
        $PYFLYBY_PATH as well as older deprecated environment variables.

        Memoized.

        @param target_filename:
          The target filename for which to get the import database.  Note that
          the target filename itself is not read.  Instead, the target
          filename is relevant because we look for .../.pyflyby based on the
          target filename.
        @rtype:
          L{ImportDB}
        """
        # We're going to canonicalize target_filenames in a number of steps.
        # At each step, see if we've seen the input so far.  We do the cache
        # checking incrementally since the steps involve syscalls.  Since this
        # is going to potentially be executed inside the IPython interactive
        # loop, we cache as much as possible.
        cache_keys = []
        target_filename = Filename(target_filename or ".")
        if target_filename.startswith("/dev"):
            target_filename = Filename(".")
        target_dirname = target_filename
        # TODO: with StatCache
        while True:
            cache_keys.append((target_dirname,
                               os.getenv("PYFLYBY_PATH"),
                               os.getenv("PYFLYBY_KNOWN_IMPORTS_PATH"),
                               os.getenv("PYFLYBY_MANDATORY_IMPORTS_PATH")))
            try:
                return cls._default_cache[cache_keys[-1]]
            except KeyError:
                pass
            if target_dirname.isdir:
                break
            target_dirname = target_dirname.dir
        target_dirname = target_dirname.real
        if target_dirname != cache_keys[-1][0]:
            cache_keys.append((target_dirname,
                               os.getenv("PYFLYBY_PATH"),
                               os.getenv("PYFLYBY_KNOWN_IMPORTS_PATH"),
                               os.getenv("PYFLYBY_MANDATORY_IMPORTS_PATH")))
            try:
                return cls._default_cache[cache_keys[-1]]
            except KeyError:
                pass
        DEFAULT_PYFLYBY_PATH = []
        etc_dir = _find_etc_dir()
        if etc_dir:
            DEFAULT_PYFLYBY_PATH.append(str(etc_dir))
        DEFAULT_PYFLYBY_PATH += [
            ".../.pyflyby",
            "~/.pyflyby",
            ]
        logger.debug("DEFAULT_PYFLYBY_PATH=%s", DEFAULT_PYFLYBY_PATH)
        filenames = _get_python_path("PYFLYBY_PATH", DEFAULT_PYFLYBY_PATH,
                                     target_dirname)
        mandatory_imports_filenames = ()
        if "SUPPORT DEPRECATED BEHAVIOR":
            PYFLYBY_PATH = _get_env_var("PYFLYBY_PATH", DEFAULT_PYFLYBY_PATH)
            # If the old deprecated environment variables are set, then heed
            # them.
            if os.getenv("PYFLYBY_KNOWN_IMPORTS_PATH"):
                # Use PYFLYBY_PATH as the default for
                # PYFLYBY_KNOWN_IMPORTS_PATH.  Note that the default is
                # relevant even though we only enter this code path when the
                # variable is set to anything, because the env var can
                # reference "-" to include the default.
                # Before pyflyby version 0.8, the default value would have
                # been
                #   [d/"known_imports" for d in PYFLYBY_PATH]
                # Instead of using that, we just use PYFLYBY_PATH directly as
                # the default.  This simplifies things and avoids need for a
                # "known_imports=>." symlink for backwards compatibility.  It
                # means that ~/.pyflyby/**/*.py (as opposed to only
                # ~/.pyflyby/known_imports/**/*.py) would be included.
                # Although this differs slightly from the old behavior, it
                # matches the behavior of the newer PYFLYBY_PATH; matching the
                # new behavior seems higher utility than exactly matching the
                # old behavior.  Files under ~/.pyflyby/mandatory_imports will
                # be included in known_imports as well, but that should not
                # cause any problems.
                default_path = PYFLYBY_PATH
                # Expand $PYFLYBY_KNOWN_IMPORTS_PATH.
                filenames = _get_python_path(
                    "PYFLYBY_KNOWN_IMPORTS_PATH", default_path, target_dirname)
                logger.debug(
                    "The environment variable PYFLYBY_KNOWN_IMPORTS_PATH is deprecated.  "
                    "Use PYFLYBY_PATH.")
            if os.getenv("PYFLYBY_MANDATORY_IMPORTS_PATH"):
                # Compute the "default" path.
                # Note that we still calculate the erstwhile default value,
                # even though it's no longer the defaults, in order to still
                # allow the "-" in the variable.
                default_path = [
                    os.path.join(d,"mandatory_imports") for d in PYFLYBY_PATH]
                # Expand $PYFLYBY_MANDATORY_IMPORTS_PATH.
                mandatory_imports_filenames = _get_python_path(
                    "PYFLYBY_MANDATORY_IMPORTS_PATH",
                    default_path, target_dirname)
                logger.debug(
                    "The environment variable PYFLYBY_MANDATORY_IMPORTS_PATH is deprecated.  "
                    "Use PYFLYBY_PATH and write __mandatory_imports__=['...'] in your files.")
        cache_keys.append((filenames, mandatory_imports_filenames))
        try:
            return cls._default_cache[cache_keys[-1]]
        except KeyError:
            pass
        result = cls._from_filenames(filenames, mandatory_imports_filenames)
        for k in cache_keys:
            cls._default_cache[k] = result
        return result

    @classmethod
    def interpret_arg(cls, arg, target_filename):
        if arg is None:
            return cls.get_default(target_filename)
        else:
            return cls(arg)

    @classmethod
    def _from_data(cls, known_imports, mandatory_imports,
                   canonical_imports, forget_imports):
        self = object.__new__(cls)
        self.forget_imports    = ImportSet(forget_imports   )
        self.known_imports     = ImportSet(known_imports    ).without_imports(forget_imports)
        self.mandatory_imports = ImportSet(mandatory_imports).without_imports(forget_imports)
        # TODO: provide more fine-grained control about canonical_imports.
        self.canonical_imports = ImportMap(canonical_imports).without_imports(forget_imports)
        return self

    @classmethod
    def _from_args(cls, args):
        # TODO: support merging input ImportDBs.  For now we support
        # L{PythonBlock}s and convertibles such as L{Filename}.
        return cls._from_code(args)

    @classmethod
    def _from_code(cls, blocks,
                   _mandatory_imports_blocks_deprecated=(),
                   _forget_imports_blocks_deprecated=(),
               ):
        """
        Load an import database from code.

          >>> ImportDB._from_code('''
          ...     import foo, bar as barf
          ...     from xx import yy
          ...     __mandatory_imports__ = ['__future__.division',
          ...                              'import aa . bb . cc as dd']
          ...     __forget_imports__ = ['xx.yy', 'from xx import zz']
          ...     __canonical_imports__ = {'bad.baad': 'good.goood'}
          ... ''')
          ImportDB('''
            import bar as barf
            import foo
          <BLANKLINE>
            __mandatory_imports__ = [
              'from __future__ import division',
              'from aa.bb import cc as dd',
            ]
          <BLANKLINE>
            __canonical_imports__ = {
              'bad.baad': 'good.goood',
            }
          <BLANKLINE>
            __forget_imports__ = [
              'from xx import yy',
              'from xx import zz',
            ]
          ''')

        @rtype:
          L{ImportDB}
        """
        if not isinstance(blocks, (tuple, list)):
            blocks = [blocks]
        if not isinstance(_mandatory_imports_blocks_deprecated, (tuple, list)):
            _mandatory_imports_blocks_deprecated = [_mandatory_imports_blocks_deprecated]
        if not isinstance(_forget_imports_blocks_deprecated, (tuple, list)):
            _forget_imports_blocks_deprecated = [_forget_imports_blocks_deprecated]
        known_imports     = []
        mandatory_imports = []
        canonical_imports = []
        forget_imports    = []
        blocks = [PythonBlock(b) for b in blocks]
        for block in blocks:
            for statement in block.statements:
                if statement.is_comment_or_blank:
                    continue
                if statement.is_import:
                    known_imports.extend(ImportStatement(statement).imports)
                    continue
                try:
                    name, value = statement.get_assignment_literal_value()
                    if name == "__mandatory_imports__":
                        mandatory_imports.append(cls._parse_import_set(value))
                    elif name == "__canonical_imports__":
                        canonical_imports.append(cls._parse_import_map(value))
                    elif name == "__forget_imports__":
                        forget_imports.append(cls._parse_import_set(value))
                    else:
                        raise ValueError(
                            "Unknown assignment to %r (expected one of "
                            "__mandatory_imports__, __canonical_imports__, "
                            "__forget_imports__)" % (name,))
                except ValueError as e:
                    raise ValueError(
                        "While parsing %s: error in %r: %s"
                        % (block.filename, statement, e))
        for block in _mandatory_imports_blocks_deprecated:
            mandatory_imports.append(ImportSet(block))
        for block in _forget_imports_blocks_deprecated:
            forget_imports.append(ImportSet(block))
        return cls._from_data(known_imports,
                              mandatory_imports,
                              canonical_imports,
                              forget_imports)

    @classmethod
    def _from_filenames(cls, filenames, _mandatory_filenames_deprecated=[]):
        """
        Load an import database from filenames.

        This function exists to support deprecated behavior.
        When we stop supporting the old behavior, we will delete this function.

        @type filenames:
          Sequence of L{Filename}s
        @param filenames:
          Filenames of files to read.
        @rtype:
          L{ImportDB}
        """
        if not isinstance(filenames, (tuple, list)):
            filenames = [filenames]
        filenames = [Filename(f) for f in filenames]
        logger.debug("ImportDB: loading [%s], mandatory=[%s]",
                     ', '.join(map(str, filenames)),
                     ', '.join(map(str, _mandatory_filenames_deprecated)))
        if "SUPPORT DEPRECATED BEHAVIOR":
            # Before 2014-10, pyflyby read the following:
            #   * known_imports from $PYFLYBY_PATH/known_imports/**/*.py or
            #     $PYFLYBY_KNOWN_IMPORTS_PATH/**/*.py,
            #   * mandatory_imports from $PYFLYBY_PATH/mandatory_imports/**/*.py or
            #     $PYFLYBY_MANDATORY_IMPORTS_PATH/**/*.py, and
            #   * forget_imports from $PYFLYBY_PATH/known_imports/**/__remove__.py
            # After 2014-10, pyflyby reads the following:
            #   * $PYFLYBY_PATH/**/*.py
            #     (with directives inside the file)
            # For backwards compatibility, for now we continue supporting the
            # old, deprecated behavior.
            blocks = []
            mandatory_imports_blocks = [
                Filename(f) for f in _mandatory_filenames_deprecated]
            forget_imports_blocks = []
            for filename in filenames:
                if filename.base == "__remove__.py":
                    forget_imports_blocks.append(filename)
                elif "mandatory_imports" in str(filename).split("/"):
                    mandatory_imports_blocks.append(filename)
                else:
                    blocks.append(filename)
            return cls._from_code(
                blocks, mandatory_imports_blocks, forget_imports_blocks)
        else:
            return cls._from_code(filenames)

    @classmethod
    def _parse_import_set(cls, arg):
        if isinstance(arg, basestring):
            arg = [arg]
        if not isinstance(arg, (tuple, list)):
            raise ValueError("Expected a list, not a %s" % (type(arg).__name__,))
        for item in arg:
            if not isinstance(item, basestring):
                raise ValueError(
                    "Expected a list of str, not %s" % (type(item).__name__,))
        return ImportSet(arg)

    @classmethod
    def _parse_import_map(cls, arg):
        if isinstance(arg, basestring):
            arg = [arg]
        if not isinstance(arg, dict):
            raise ValueError("Expected a dict, not a %s" % (type(arg).__name__,))
        for k, v in arg.items():
            if not isinstance(k, basestring):
                raise ValueError(
                    "Expected a dict of str, not %s" % (type(k).__name__,))
            if not isinstance(v, basestring):
                raise ValueError(
                    "Expected a dict of str, not %s" % (type(v).__name__,))
        return ImportMap(arg)

    @cached_attribute
    def by_fullname_or_import_as(self):
        """
        Map from C{fullname} and C{import_as} to L{Import}s.

          >>> import pprint
          >>> db = ImportDB('from aa.bb import cc as dd')
          >>> pprint.pprint(db.by_fullname_or_import_as)
          {'aa': (Import('import aa'),),
           'aa.bb': (Import('import aa.bb'),),
           'dd': (Import('from aa.bb import cc as dd'),)}

        @rtype:
          C{dict} mapping from C{str} to tuple of L{Import}s
        """
        # TODO: make known_imports take into account the below forget_imports,
        # then move this function into ImportSet
        d = defaultdict(set)
        for imp in self.known_imports.imports:
            # Given an import like "from foo.bar import quux as QUUX", add the
            # following entries:
            #   - "QUUX"         => "from foo.bar import quux as QUUX"
            #   - "foo.bar"      => "import foo.bar"
            #   - "foo"          => "import foo"
            # We don't include an entry labeled "quux" because the user has
            # implied he doesn't want to pollute the global namespace with
            # "quux", only "QUUX".
            d[imp.import_as].add(imp)
            for prefix in dotted_prefixes(imp.fullname)[:-1]:
                d[prefix].add(Import.from_parts(prefix, prefix))
        return dict( (k, tuple(sorted(v - set(self.forget_imports.imports))))
                     for k, v in d.iteritems())

    def __repr__(self):
        printed = self.pretty_print()
        lines = "".join("  "+line for line in printed.splitlines(True))
        return "%s('''\n%s''')" % (type(self).__name__, lines)

    def pretty_print(self):
        s = self.known_imports.pretty_print()
        if self.mandatory_imports:
            s += "\n__mandatory_imports__ = [\n"
            for imp in self.mandatory_imports.imports:
                s += "  '%s',\n" % imp
            s += "]\n"
        if self.canonical_imports:
            s += "\n__canonical_imports__ = {\n"
            for k, v in sorted(self.canonical_imports.items()):
                s += "  '%s': '%s',\n" % (k, v)
            s += "}\n"
        if self.forget_imports:
            s += "\n__forget_imports__ = [\n"
            for imp in self.forget_imports.imports:
                s += "  '%s',\n" % imp
            s += "]\n"
        return s