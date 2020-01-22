"""
This file implements the code-generator for parallel-vectorize.

ParallelUFunc is the platform independent base class for generating
the thread dispatcher.  This thread dispatcher launches threads
that execute the generated function of UFuncCore.
UFuncCore is subclassed to specialize for the input/output types.
The actual workload is invoked inside the function generated by UFuncCore.
UFuncCore also defines a work-stealing mechanism that allows idle threads
to steal works from other threads.
"""

import os
import platform
import sys
import warnings
from threading import RLock as threadRLock
import multiprocessing

import numpy as np

import llvmlite.llvmpy.core as lc
import llvmlite.binding as ll

from numba.npyufunc import ufuncbuilder
from numba.numpy_support import as_dtype
from numba import types, config, utils
from numba.npyufunc.wrappers import _wrapper_info


def get_thread_count():
    """
    Gets the available thread count.
    """
    t = config.NUMBA_NUM_THREADS
    if t < 1:
        raise ValueError("Number of threads specified must be > 0.")
    return t


NUM_THREADS = get_thread_count()


def build_gufunc_kernel(library, ctx, info, sig, inner_ndim):
    """Wrap the original CPU ufunc/gufunc with a parallel dispatcher.
    This function will wrap gufuncs and ufuncs something like.

    Args
    ----
    ctx
        numba's codegen context

    info: (library, env, name)
        inner function info

    sig
        type signature of the gufunc

    inner_ndim
        inner dimension of the gufunc (this is len(sig.args) in the case of a
        ufunc)

    Returns
    -------
    wrapper_info : (library, env, name)
        The info for the gufunc wrapper.

    Details
    -------

    The kernel signature looks like this:

    void kernel(char **args, npy_intp *dimensions, npy_intp* steps, void* data)

    args - the input arrays + output arrays
    dimensions - the dimensions of the arrays
    steps - the step size for the array (this is like sizeof(type))
    data - any additional data

    The parallel backend then stages multiple calls to this kernel concurrently
    across a number of threads. Practically, for each item of work, the backend
    duplicates `dimensions` and adjusts the first entry to reflect the size of
    the item of work, it also forms up an array of pointers into the args for
    offsets to read/write from/to with respect to its position in the items of
    work. This allows the same kernel to be used for each item of work, with
    simply adjusted reads/writes/domain sizes and is safe by virtue of the
    domain partitioning.

    NOTE: The execution backend is passed the requested thread count, but it can
    choose to ignore it (TBB)!
    """
    assert isinstance(info, tuple)  # guard against old usage
    # Declare types and function
    byte_t = lc.Type.int(8)
    byte_ptr_t = lc.Type.pointer(byte_t)
    byte_ptr_ptr_t = lc.Type.pointer(byte_ptr_t)

    intp_t = ctx.get_value_type(types.intp)
    intp_ptr_t = lc.Type.pointer(intp_t)

    fnty = lc.Type.function(lc.Type.void(), [lc.Type.pointer(byte_ptr_t),
                                             lc.Type.pointer(intp_t),
                                             lc.Type.pointer(intp_t),
                                             byte_ptr_t])
    wrapperlib = ctx.codegen().create_library('parallelgufuncwrapper')
    mod = wrapperlib.create_ir_module('parallel.gufunc.wrapper')
    kernel_name = ".kernel.{}_{}".format(id(info.env), info.name)
    lfunc = mod.add_function(fnty, name=kernel_name)

    bb_entry = lfunc.append_basic_block('')

    # Function body starts
    builder = lc.Builder(bb_entry)

    args, dimensions, steps, data = lfunc.args

    # Release the GIL (and ensure we have the GIL)
    # Note: numpy ufunc may not always release the GIL; thus,
    #       we need to ensure we have the GIL.
    pyapi = ctx.get_python_api(builder)
    gil_state = pyapi.gil_ensure()
    thread_state = pyapi.save_thread()

    def as_void_ptr(arg):
        return builder.bitcast(arg, byte_ptr_t)

    # Array count is input signature plus 1 (due to output array)
    array_count = len(sig.args) + 1

    parallel_for_ty = lc.Type.function(lc.Type.void(),
                                       [byte_ptr_t] * 5 + [intp_t, ] * 2)
    parallel_for = mod.get_or_insert_function(parallel_for_ty,
                                              name='numba_parallel_for')

    # Reference inner-function and link
    innerfunc_fnty = lc.Type.function(
        lc.Type.void(),
        [byte_ptr_ptr_t, intp_ptr_t, intp_ptr_t, byte_ptr_t],
    )
    tmp_voidptr = mod.get_or_insert_function(
        innerfunc_fnty, name=info.name,
    )
    wrapperlib.add_linking_library(info.library)

    # Prepare call
    fnptr = builder.bitcast(tmp_voidptr, byte_ptr_t)
    innerargs = [as_void_ptr(x) for x
                 in [args, dimensions, steps, data]]
    builder.call(parallel_for, [fnptr] + innerargs +
                 [intp_t(x) for x in (inner_ndim, array_count)])

    # Release the GIL
    pyapi.restore_thread(thread_state)
    pyapi.gil_release(gil_state)

    builder.ret_void()

    wrapperlib.add_ir_module(mod)
    wrapperlib.add_linking_library(library)
    return _wrapper_info(library=wrapperlib, name=lfunc.name, env=info.env)


# ------------------------------------------------------------------------------

class ParallelUFuncBuilder(ufuncbuilder.UFuncBuilder):
    def build(self, cres, sig):
        _launch_threads()

        # Buider wrapper for ufunc entry point
        ctx = cres.target_context
        signature = cres.signature
        library = cres.library
        fname = cres.fndesc.llvm_func_name

        info = build_ufunc_wrapper(library, ctx, fname, signature, cres)
        ptr = info.library.get_pointer_to_function(info.name)
        # Get dtypes
        dtypenums = [np.dtype(a.name).num for a in signature.args]
        dtypenums.append(np.dtype(signature.return_type.name).num)
        keepalive = ()
        return dtypenums, ptr, keepalive


def build_ufunc_wrapper(library, ctx, fname, signature, cres):
    innerfunc = ufuncbuilder.build_ufunc_wrapper(library, ctx, fname,
                                                 signature, objmode=False,
                                                 cres=cres)
    info = build_gufunc_kernel(library, ctx, innerfunc, signature,
                               len(signature.args))
    return info

# ---------------------------------------------------------------------------


class ParallelGUFuncBuilder(ufuncbuilder.GUFuncBuilder):
    def __init__(self, py_func, signature, identity=None, cache=False,
                 targetoptions={}):
        # Force nopython mode
        targetoptions.update(dict(nopython=True))
        super(
            ParallelGUFuncBuilder,
            self).__init__(
            py_func=py_func,
            signature=signature,
            identity=identity,
            cache=cache,
            targetoptions=targetoptions)

    def build(self, cres):
        """
        Returns (dtype numbers, function ptr, EnvironmentObject)
        """
        _launch_threads()

        # Build wrapper for ufunc entry point
        info = build_gufunc_wrapper(
            self.py_func, cres, self.sin, self.sout, cache=self.cache,
            is_parfors=False,
        )
        ptr = info.library.get_pointer_to_function(info.name)
        env = info.env

        # Get dtypes
        dtypenums = []
        for a in cres.signature.args:
            if isinstance(a, types.Array):
                ty = a.dtype
            else:
                ty = a
            dtypenums.append(as_dtype(ty).num)

        return dtypenums, ptr, env


# This is not a member of the ParallelGUFuncBuilder function because it is
# called without an enclosing instance from parfors

def build_gufunc_wrapper(py_func, cres, sin, sout, cache, is_parfors):
    """Build gufunc wrapper for the given arguments.
    The *is_parfors* is a boolean indicating whether the gufunc is being
    built for use as a ParFors kernel. This changes codegen and caching
    behavior.
    """
    library = cres.library
    ctx = cres.target_context
    signature = cres.signature
    innerinfo = ufuncbuilder.build_gufunc_wrapper(
        py_func, cres, sin, sout, cache=cache, is_parfors=is_parfors,
    )
    sym_in = set(sym for term in sin for sym in term)
    sym_out = set(sym for term in sout for sym in term)
    inner_ndim = len(sym_in | sym_out)

    info = build_gufunc_kernel(
        library, ctx, innerinfo, signature, inner_ndim,
    )
    return info

# ---------------------------------------------------------------------------


_backend_init_thread_lock = threadRLock()

_windows = sys.platform.startswith('win32')


class _nop(object):
    """A no-op contextmanager
    """
    def __enter__(self):
        pass

    def __exit__(self, *args):
        pass


try:
    # Force the use of an RLock in the case a fork was used to start the
    # process and thereby the init sequence, some of the threading backend
    # init sequences are not fork safe. Also, windows global mp locks seem
    # to be fine.
    if "fork" in multiprocessing.get_start_method() or _windows:
        _backend_init_process_lock = multiprocessing.get_context().RLock()
    else:
        _backend_init_process_lock = _nop()

except OSError as e:

    # probably lack of /dev/shm for semaphore writes, warn the user
    msg = ("Could not obtain multiprocessing lock due to OS level error: %s\n"
           "A likely cause of this problem is '/dev/shm' is missing or"
           "read-only such that necessary semaphores cannot be written.\n"
           "*** The responsibility of ensuring multiprocessing safe access to "
           "this initialization sequence/module import is deferred to the "
           "user! ***\n")
    warnings.warn(msg % str(e))

    _backend_init_process_lock = _nop()

_is_initialized = False

# this is set by _launch_threads
_threading_layer = None


def threading_layer():
    """
    Get the name of the threading layer in use for parallel CPU targets
    """
    if _threading_layer is None:
        raise ValueError("Threading layer is not initialized.")
    else:
        return _threading_layer


def _launch_threads():
    with _backend_init_process_lock:
        with _backend_init_thread_lock:
            global _is_initialized
            if _is_initialized:
                return

            from ctypes import CFUNCTYPE, c_int

            def select_known_backend(backend):
                """
                Loads a specific threading layer backend based on string
                """
                lib = None
                if backend.startswith("tbb"):
                    try:
                        from . import tbbpool as lib
                    except ImportError:
                        pass
                elif backend.startswith("omp"):
                    # TODO: Check that if MKL is present that it is a version
                    # that understands GNU OMP might be present
                    try:
                        from . import omppool as lib
                    except ImportError:
                        pass
                elif backend.startswith("workqueue"):
                    from . import workqueue as lib
                else:
                    msg = "Unknown value specified for threading layer: %s"
                    raise ValueError(msg % backend)
                return lib

            def select_from_backends(backends):
                """
                Selects from presented backends and returns the first working
                """
                lib = None
                for backend in backends:
                    lib = select_known_backend(backend)
                    if lib is not None:
                        break
                else:
                    backend = ''
                return lib, backend

            t = str(config.THREADING_LAYER).lower()
            namedbackends = ['tbb', 'omp', 'workqueue']

            lib = None
            _IS_OSX = platform.system() == "Darwin"
            _IS_LINUX = platform.system() == "Linux"
            err_helpers = dict()
            err_helpers['TBB'] = ("Intel TBB is required, try:\n"
                                  "$ conda/pip install tbb")
            err_helpers['OSX_OMP'] = ("Intel OpenMP is required, try:\n"
                                      "$ conda/pip install intel-openmp")
            requirements = []

            def raise_with_hint(required):
                errmsg = "No threading layer could be loaded.\n%s"
                hintmsg = "HINT:\n%s"
                if len(required) == 0:
                    hint = ''
                if len(required) == 1:
                    hint = hintmsg % err_helpers[required[0]]
                if len(required) > 1:
                    options = '\nOR\n'.join([err_helpers[x] for x in required])
                    hint = hintmsg % ("One of:\n%s" % options)
                raise ValueError(errmsg % hint)

            if t in namedbackends:
                # Try and load the specific named backend
                lib = select_known_backend(t)
                if not lib:
                    # something is missing preventing a valid backend from
                    # loading, set requirements for hinting
                    if t == 'tbb':
                        requirements.append('TBB')
                    elif t == 'omp' and _IS_OSX:
                        requirements.append('OSX_OMP')
                libname = t
            elif t in ['threadsafe', 'forksafe', 'safe']:
                # User wants a specific behaviour...
                available = ['tbb']
                requirements.append('TBB')
                if t == "safe":
                    # "safe" is TBB, which is fork and threadsafe everywhere
                    pass
                elif t == "threadsafe":
                    if _IS_OSX:
                        requirements.append('OSX_OMP')
                    # omp is threadsafe everywhere
                    available.append('omp')
                elif t == "forksafe":
                    # everywhere apart from linux (GNU OpenMP) has a guaranteed
                    # forksafe OpenMP, as OpenMP has better performance, prefer
                    # this to workqueue
                    if not _IS_LINUX:
                        available.append('omp')
                    if _IS_OSX:
                        requirements.append('OSX_OMP')
                    # workqueue is forksafe everywhere
                    available.append('workqueue')
                else:  # unreachable
                    msg = "No threading layer available for purpose %s"
                    raise ValueError(msg % t)
                # select amongst available
                lib, libname = select_from_backends(available)
            elif t == 'default':
                # If default is supplied, try them in order, tbb, omp,
                # workqueue
                lib, libname = select_from_backends(namedbackends)
                if not lib:
                    # set requirements for hinting
                    requirements.append('TBB')
                    if _IS_OSX:
                        requirements.append('OSX_OMP')
            else:
                msg = "The threading layer requested '%s' is unknown to Numba."
                raise ValueError(msg % t)

            # No lib found, raise and hint
            if not lib:
                raise_with_hint(requirements)

            ll.add_symbol('numba_parallel_for', lib.parallel_for)
            ll.add_symbol('do_scheduling_signed', lib.do_scheduling_signed)
            ll.add_symbol('do_scheduling_unsigned', lib.do_scheduling_unsigned)

            launch_threads = CFUNCTYPE(None, c_int)(lib.launch_threads)
            launch_threads(NUM_THREADS)

            # set library name so it can be queried
            global _threading_layer
            _threading_layer = libname
            _is_initialized = True


_DYLD_WORKAROUND_SET = 'NUMBA_DYLD_WORKAROUND' in os.environ
_DYLD_WORKAROUND_VAL = int(os.environ.get('NUMBA_DYLD_WORKAROUND', 0))

if _DYLD_WORKAROUND_SET and _DYLD_WORKAROUND_VAL:
    _launch_threads()
