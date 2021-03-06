#
# Copyright (c) 2019 UAVCAN Development Team
# This software is distributed under the terms of the MIT License.
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

import time
import gzip
import typing
import pickle
import base64
import pathlib
import logging
import itertools
import dataclasses

import pydsdl
import nunavut
import nunavut.jinja
import nunavut.postprocessors

import pyuavcan


_AnyPath = typing.Union[str, pathlib.Path]

_TEMPLATE_DIRECTORY: pathlib.Path = pathlib.Path(__file__).absolute().parent / pathlib.Path('_templates')

_OUTPUT_FILE_PERMISSIONS = 0o444
"""
Read-only for all because the files are autogenerated and should not be edited manually.
"""

_logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class GeneratedPackageInfo:
    path: pathlib.Path
    """
    Path to the directory that contains the top-level ``__init__.py``.
    """

    models: typing.Sequence[pydsdl.CompositeType]
    """
    List of PyDSDL objects describing the source DSDL definitions.
    This can be used for arbitrarily complex introspection and reflection.
    """

    name: str
    """
    The name of the generated package, which is the same as the name of the DSDL root namespace unless
    the name had to be stropped. See ``nunavut.lang.py.PYTHON_RESERVED_IDENTIFIERS``.
    """


def generate_package(root_namespace_directory:        _AnyPath,
                     lookup_directories:              typing.Optional[typing.List[_AnyPath]] = None,
                     output_directory:                typing.Optional[_AnyPath] = None,
                     allow_unregulated_fixed_port_id: bool = False) -> GeneratedPackageInfo:
    """
    This function runs the DSDL compiler, converting a specified DSDL root namespace into a Python package.
    In the generated package, nested DSDL namespaces are represented as Python subpackages,
    DSDL types as Python classes, type version numbers as class name suffixes separated via underscores
    (like ``Type_1_0``), constants as class attributes, fields as properties.
    For a more detailed information on how to use generated types, just generate them and read the resulting
    code -- it is made to be human-readable and contains docstrings.

    Generated packages can be freely moved around the file system or even deployed on other systems --
    they are fully location-invariant.

    Generated packages do not automatically import their nested subpackages. For example, if the application
    needs to use ``uavcan.node.Heartbeat.1.0``, it has to ``import uavcan.node`` explicitly; doing just
    ``import uavcan`` is not sufficient.

    If the source definition contains identifiers, type names, namespace components, or other entities whose
    names are listed in ``nunavut.lang.py.PYTHON_RESERVED_IDENTIFIERS``,
    the compiler applies stropping by suffixing such entities with an underscore ``_``.
    A small subset of applications may require access to a generated entity without knowing in advance whether
    its name is a reserved identifier or not (i.e., whether it's stropped or not). To simplify usage,
    this submodule provides helper functions
    :func:`pyuavcan.dsdl.get_attribute` and :func:`pyuavcan.dsdl.set_attribute` that provide access to generated
    class/object attributes using their original names before stropping.
    Likewise, the function :func:`pyuavcan.dsdl.get_model` can find a generated type even if any of its name
    components are stropped; e.g., a DSDL type ``str.Type.1.0`` would be imported as ``str_.Type_1_0``.
    None of it, however, is relevant for an application that does not require genericity (vast majority of
    applications don't), so a much easier approach in that case is just to look at the generated code and see
    if there are any stropped identifiers in it, and then just use appropriate names statically.

    The recommended usage pattern for this function is lazy generation.
    First, add the ``output_directory`` (if not specified it defaults to the current working directory)
    to :data:`sys.path` or to the ``PYTHONPATH`` environment variable to make the generated package(s) importable.
    Then try importing the target DSDL-generated package. If the attempt is successful, our job here is done.
    Otherwise, the package(s) need(s) to be generated by invoking this function,
    and then another import attempt will have to be made.
    Beware that before retrying the import it's necessary to invoke :func:`importlib.invalidate_caches`.

    A package generated for a particular version of PyUAVCAN may be incompatible with any other version of the
    library. If your application relies on lazy generation, consider including the library version string
    :data:`pyuavcan.__version__` in ``output_directory``, so that the generated package cache is
    invalidated automatically when a different version of the library is used.

    Having generated a package, consider updating the include path set of your Python IDE to take advantage
    of code completion and static type checking.

    When using PyUAVCAN from an interactive session (e.g., REPL or Jupyter), it is usually more convenient
    to generate packages using the command-line tool rather than invoking this function manually.
    Please refer to the command-line tool documentation for details.

    :param root_namespace_directory: The source DSDL root namespace directory path. The last component of the path
        is the name of the root namespace. For example, to generate package for the root namespace ``uavcan``,
        the path would be like ``foo/bar/uavcan``.

    :param lookup_directories: An iterable of DSDL root namespace directory paths where to search for referred DSDL
        definitions. The format of each path is the same as for the previous parameter; i.e., the last component
        of each path is a DSDL root namespace name. If you are generating code for a vendor-specific DSDL root
        namespace, make sure to provide at least the path to the standard ``uavcan`` namespace directory here.

    :param output_directory: The generated Python package directory will be placed into this directory.
        If not specified or None, the current working directory is used.
        For example, if this argument equals ``foo/bar``, and the DSDL root namespace name is ``uavcan``,
        the top-level ``__init__.py`` of the generated package will end up in ``foo/bar/uavcan/__init__.py``.
        The directory tree will be created automatically if it does not exist (like ``mkdir -p``).
        If the destination exists, it will be silently written over.
        In production, applications are recommended to shard the output directory by the library version number
        to avoid compatibility issues with code generated by older versions of the library.
        Don't forget to add the output directory to ``PYTHONPATH``, even if it's the current working directory.

    :param allow_unregulated_fixed_port_id: If True, the DSDL processing front-end will not reject unregulated
        data types with fixed port-ID. If you are not sure what it means, do not use it, and read the UAVCAN
        specification first. The default is False.

    :return: An instance of :class:`GeneratedPackageInfo` describing the generated package.

    :raises: :class:`OSError` if required operations on the file system could not be performed;
        ``pydsdl.InvalidDefinitionError`` if the source DSDL definitions are invalid;
        ``pydsdl.InternalError`` if there is a bug in the DSDL processing front-end;
        :class:`ValueError` if any of the arguments are otherwise invalid.

    The following table is an excerpt from the UAVCAN specification. Observe that *unregulated fixed port identifiers*
    are prohibited by default, but it can be overridden.

    +-------+---------------------------------------------------+----------------------------------------------+
    |Scope  | Regulated                                         | Unregulated                                  |
    +=======+===================================================+==============================================+
    |Public |Standard and contributed (e.g., vendor-specific)   |Definitions distributed separately from the   |
    |       |definitions. Fixed port identifiers are allowed;   |UAVCAN specification. Fixed port identifiers  |
    |       |they are called *"regulated port-IDs"*.            |are *not allowed*.                            |
    +-------+---------------------------------------------------+----------------------------------------------+
    |Private|Nonexistent category.                              |Definitions that are not available to anyone  |
    |       |                                                   |except their authors. Fixed port identifiers  |
    |       |                                                   |are permitted (although not recommended); they|
    |       |                                                   |are called *"unregulated fixed port-IDs"*.    |
    +-------+---------------------------------------------------+----------------------------------------------+

    Here is a brief usage example:

    >>> import sys
    >>> import pathlib
    >>> import tempfile
    >>> import importlib
    >>> import pyuavcan
    >>> dsdl_generated_dir = pathlib.Path(tempfile.gettempdir(), 'dsdl-for-my-program', pyuavcan.__version__)
    >>> dsdl_generated_dir.mkdir(parents=True, exist_ok=True)
    >>> sys.path.insert(0, str(dsdl_generated_dir))
    >>> try:
    ...     import sirius_cyber_corp
    ...     import uavcan.si.sample.volumetric_flow_rate
    ... except (ImportError, AttributeError):
    ...     _ = pyuavcan.dsdl.generate_package(root_namespace_directory='tests/dsdl/namespaces/sirius_cyber_corp',
    ...                                        lookup_directories=['tests/public_regulated_data_types/uavcan'],
    ...                                        output_directory=dsdl_generated_dir)
    ...     _ = pyuavcan.dsdl.generate_package(root_namespace_directory='tests/public_regulated_data_types/uavcan',
    ...                                        output_directory=dsdl_generated_dir)
    ...     importlib.invalidate_caches()
    ...     import sirius_cyber_corp
    ...     import uavcan.si.sample.volumetric_flow_rate
    """
    started_at = time.monotonic()

    # Read the DSDL definitions
    if isinstance(lookup_directories, (str, bytes)):
        # https://forum.uavcan.org/t/nestedrootnamespaceerror-in-basic-usage-demo/794
        raise TypeError(f'Lookup directories shall be an iterable of strings, not {type(lookup_directories).__name__}')
    composite_types = pydsdl.read_namespace(root_namespace_directory=str(root_namespace_directory),
                                            lookup_directories=list(map(str, lookup_directories or [])),
                                            allow_unregulated_fixed_port_id=allow_unregulated_fixed_port_id)
    root_namespace_name, = set(map(lambda x: x.root_namespace, composite_types))  # type: str,
    _logger.info('Read %d definitions from root namespace %r', len(composite_types), root_namespace_name)

    # Template primitives
    filters = {
        'pickle':             _pickle_object,
        'numpy_scalar_type':  _numpy_scalar_type,
    }

    # Generate code
    output_directory = pathlib.Path.cwd() if output_directory is None else output_directory
    language_context = nunavut.lang.LanguageContext('py', namespace_output_stem='__init__')
    root_ns = nunavut.build_namespace_tree(types=composite_types,
                                           root_namespace_dir=root_namespace_directory,
                                           output_dir=str(output_directory),
                                           language_context=language_context)
    generator = nunavut.jinja.DSDLCodeGenerator(namespace=root_ns,
                                                generate_namespace_types=nunavut.YesNoDefault.YES,
                                                templates_dir=_TEMPLATE_DIRECTORY,
                                                followlinks=True,
                                                additional_filters=filters,
                                                post_processors=[
                                                    nunavut.postprocessors.SetFileMode(_OUTPUT_FILE_PERMISSIONS),
                                                    nunavut.postprocessors.LimitEmptyLines(2),
                                                    nunavut.postprocessors.TrimTrailingWhitespace(),
                                                ])
    generator.generate_all()

    _logger.info('Generated %d types from the root namespace %r in %f.1 seconds',
                 len(composite_types), root_namespace_name, time.monotonic() - started_at)
    return GeneratedPackageInfo(path=pathlib.Path(output_directory) / pathlib.Path(root_namespace_name),
                                models=composite_types,
                                name=root_namespace_name)


def _pickle_object(x: typing.Any) -> str:
    pck: str = base64.b85encode(gzip.compress(pickle.dumps(x, protocol=4))).decode().strip()
    segment_gen = map(''.join, itertools.zip_longest(*([iter(pck)] * 100), fillvalue=''))
    return '\n'.join(repr(x) for x in segment_gen)


def _numpy_scalar_type(t: pydsdl.Any) -> str:
    def pick_width(w: int) -> int:
        for o in [8, 16, 32, 64]:
            if w <= o:
                return o
        raise ValueError(f'Invalid bit width: {w}')  # pragma: no cover

    if isinstance(t, pydsdl.BooleanType):
        return f'_np_.bool'
    elif isinstance(t, pydsdl.SignedIntegerType):
        return f'_np_.int{pick_width(t.bit_length)}'
    elif isinstance(t, pydsdl.UnsignedIntegerType):
        return f'_np_.uint{pick_width(t.bit_length)}'
    elif isinstance(t, pydsdl.FloatType):
        return f'_np_.float{pick_width(t.bit_length)}'
    else:
        assert not isinstance(t, pydsdl.PrimitiveType), 'Forgot to handle some primitive types'
        return f'_np_.object_'
