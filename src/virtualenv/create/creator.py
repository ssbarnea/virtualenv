from __future__ import absolute_import, print_function, unicode_literals

import json
import logging
import os
import shutil
import sys
from abc import ABCMeta, abstractmethod
from argparse import ArgumentTypeError
from ast import literal_eval
from collections import OrderedDict
from stat import S_IWUSR

from six import add_metaclass

from virtualenv.discovery.cached_py_info import LogCmd
from virtualenv.info import WIN_CPYTHON_2
from virtualenv.pyenv_cfg import PyEnvCfg
from virtualenv.util.path import Path
from virtualenv.util.six import ensure_str, ensure_text
from virtualenv.util.subprocess import run_cmd
from virtualenv.util.zipapp import ensure_file_on_disk
from virtualenv.version import __version__

HERE = Path(os.path.abspath(__file__)).parent
DEBUG_SCRIPT = HERE / "debug.py"


@add_metaclass(ABCMeta)
class Creator(object):
    """A class that given a python Interpreter creates a virtual environment"""

    def __init__(self, options, interpreter):
        """Construct a new virtual environment creator.

        :param options: the CLI option as parsed from :meth:`add_parser_arguments`
        :param interpreter: the interpreter to create virtual environment from
        """
        self.interpreter = interpreter
        self._debug = None
        self.dest = Path(options.dest)
        self.clear = options.clear
        self.pyenv_cfg = PyEnvCfg.from_folder(self.dest)

    def __repr__(self):
        return ensure_str(self.__unicode__())

    def __unicode__(self):
        return "{}({})".format(self.__class__.__name__, ", ".join("{}={}".format(k, v) for k, v in self._args()))

    def _args(self):
        return [
            ("dest", ensure_text(str(self.dest))),
            ("clear", self.clear),
        ]

    @classmethod
    def can_create(cls, interpreter):
        """Determine if we can create a virtual environment.

        :param interpreter: the interpreter in question
        :return: ``None`` if we can't create, any other object otherwise that will be forwarded to \
                  :meth:`add_parser_arguments`
        """
        return True

    @classmethod
    def add_parser_arguments(cls, parser, interpreter, meta):
        """Add CLI arguments for the creator.

        :param parser: the CLI parser
        :param interpreter: the interpreter we're asked to create virtual environment for
        :param meta: value as returned by :meth:`can_create`
        """
        parser.add_argument(
            "dest", help="directory to create virtualenv at", type=cls.validate_dest,
        )
        parser.add_argument(
            "--clear",
            dest="clear",
            action="store_true",
            help="remove the destination directory if exist before starting (will overwrite files otherwise)",
            default=False,
        )

    @abstractmethod
    def create(self):
        """Perform the virtual environment creation."""
        raise NotImplementedError

    @classmethod
    def validate_dest(cls, raw_value):
        """No path separator in the path, valid chars and must be write-able"""

        def non_write_able(dest, value):
            common = Path(*os.path.commonprefix([value.parts, dest.parts]))
            raise ArgumentTypeError(
                "the destination {} is not write-able at {}".format(dest.relative_to(common), common)
            )

        # the file system must be able to encode
        # note in newer CPython this is always utf-8 https://www.python.org/dev/peps/pep-0529/
        encoding = sys.getfilesystemencoding()
        refused = OrderedDict()
        kwargs = {"errors": "ignore"} if encoding != "mbcs" else {}
        for char in ensure_text(raw_value):
            try:
                trip = char.encode(encoding, **kwargs).decode(encoding)
                if trip == char:
                    continue
                raise ValueError(trip)
            except ValueError:
                refused[char] = None
        if refused:
            raise ArgumentTypeError(
                "the file system codec ({}) cannot handle characters {!r} within {!r}".format(
                    encoding, "".join(refused.keys()), raw_value
                )
            )
        for char in (i for i in (os.pathsep, os.altsep) if i is not None):
            if char in raw_value:
                raise ArgumentTypeError(
                    "destination {!r} must not contain the path separator ({}) as this would break "
                    "the activation scripts".format(raw_value, char)
                )

        value = Path(raw_value)
        if value.exists() and value.is_file():
            raise ArgumentTypeError("the destination {} already exists and is a file".format(value))
        if (3, 3) <= sys.version_info <= (3, 6):
            # pre 3.6 resolve is always strict, aka must exists, sidestep by using os.path operation
            dest = Path(os.path.realpath(raw_value))
        else:
            dest = Path(os.path.abspath(str(value))).resolve()  # on Windows absolute does not imply resolve so use both
        value = dest
        while dest:
            if dest.exists():
                if os.access(ensure_text(str(dest)), os.W_OK):
                    break
                else:
                    non_write_able(dest, value)
            base, _ = dest.parent, dest.name
            if base == dest:
                non_write_able(dest, value)  # pragma: no cover
            dest = base
        return str(value)

    def run(self):
        if self.dest.exists() and self.clear:
            logging.debug("delete %s", self.dest)

            def onerror(func, path, exc_info):
                if not os.access(path, os.W_OK):
                    os.chmod(path, S_IWUSR)
                    func(path)
                else:
                    raise

            shutil.rmtree(str(self.dest), ignore_errors=True, onerror=onerror)
        self.create()
        self.set_pyenv_cfg()

    def set_pyenv_cfg(self):
        self.pyenv_cfg.content = OrderedDict()
        self.pyenv_cfg["home"] = self.interpreter.system_exec_prefix
        self.pyenv_cfg["implementation"] = self.interpreter.implementation
        self.pyenv_cfg["version_info"] = ".".join(str(i) for i in self.interpreter.version_info)
        self.pyenv_cfg["virtualenv"] = __version__

    @property
    def debug(self):
        """
        :return: debug information about the virtual environment (only valid after :meth:`create` has run)
        """
        if self._debug is None and self.exe is not None:
            self._debug = get_env_debug_info(self.exe, self.debug_script())
        return self._debug

    # noinspection PyMethodMayBeStatic
    def debug_script(self):
        return DEBUG_SCRIPT


def get_env_debug_info(env_exe, debug_script):
    env = os.environ.copy()
    env.pop(str("PYTHONPATH"), None)

    with ensure_file_on_disk(debug_script) as debug_script:
        cmd = [str(env_exe), str(debug_script)]
        if WIN_CPYTHON_2:
            cmd = [ensure_text(i) for i in cmd]
        logging.debug(str("debug via %r"), LogCmd(cmd))
        code, out, err = run_cmd(cmd)

    # noinspection PyBroadException
    try:
        if code != 0:
            result = literal_eval(out)
        else:
            result = json.loads(out)
        if err:
            result["err"] = err
    except Exception as exception:
        return {"out": out, "err": err, "returncode": code, "exception": repr(exception)}
    if "sys" in result and "path" in result["sys"]:
        del result["sys"]["path"][0]
    return result
