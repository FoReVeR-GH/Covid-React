import inspect
import numpy as np
import os
import sys
import ctypes
import functools

from numba.core import config, serialize, sigutils, types, typing, utils
from numba.core.compiler_lock import global_compiler_lock
from numba.core.dispatcher import Dispatcher as uber_Dispatcher
from numba.core.errors import NumbaPerformanceWarning
from numba.core.typing.templates import AbstractTemplate
from numba.core.typing.typeof import Purpose, typeof

from numba.cuda.api import get_current_device
from numba.cuda.args import wrap_arg
from numba.cuda.compiler import compile_cuda, CUDACompiler
from numba.cuda.cudadrv import driver
from numba.cuda.cudadrv.devices import get_context
from numba.cuda.cudadrv.libs import get_cudalib
from numba.cuda.descriptor import cuda_target
from numba.cuda.errors import (missing_launch_config_msg,
                               normalize_kernel_dimensions)
from numba.cuda import types as cuda_types

from numba import cuda
from numba import _dispatcher

from warnings import warn


class _Kernel(serialize.ReduceMixin):
    '''
    CUDA Kernel specialized for a given set of argument types. When called, this
    object launches the kernel on the device.
    '''

    @global_compiler_lock
    def __init__(self, py_func, argtypes, link=None, debug=False,
                 lineinfo=False, inline=False, fastmath=False, extensions=None,
                 max_registers=None, opt=True, device=False):

        if device:
            raise RuntimeError('Cannot compile a device function as a kernel')

        super().__init__()

        self.py_func = py_func
        self.argtypes = argtypes
        self.debug = debug
        self.lineinfo = lineinfo
        self.extensions = extensions or []

        nvvm_options = {
            'debug': self.debug,
            'lineinfo': self.lineinfo,
            'fastmath': fastmath,
            'opt': 3 if opt else 0
        }

        cres = compile_cuda(self.py_func, types.void, self.argtypes,
                            debug=self.debug,
                            lineinfo=self.lineinfo,
                            inline=inline,
                            fastmath=fastmath,
                            nvvm_options=nvvm_options)
        tgt_ctx = cres.target_context
        code = self.py_func.__code__
        filename = code.co_filename
        linenum = code.co_firstlineno
        lib, kernel = tgt_ctx.prepare_cuda_kernel(cres.library, cres.fndesc,
                                                  debug, nvvm_options,
                                                  filename, linenum,
                                                  max_registers)

        if not link:
            link = []

        # A kernel needs cooperative launch if grid_sync is being used.
        self.cooperative = 'cudaCGGetIntrinsicHandle' in lib.get_asm_str()
        # We need to link against cudadevrt if grid sync is being used.
        if self.cooperative:
            link.append(get_cudalib('cudadevrt', static=True))

        for filepath in link:
            lib.add_linking_file(filepath)

        # populate members
        self.entry_name = kernel.name
        self.signature = cres.signature
        self._type_annotation = cres.type_annotation
        self._codelibrary = lib
        self.call_helper = cres.call_helper

    @property
    def argument_types(self):
        return tuple(self.signature.args)

    @classmethod
    def _rebuild(cls, cooperative, name, argtypes, codelibrary, link, debug,
                 lineinfo, call_helper, extensions):
        """
        Rebuild an instance.
        """
        instance = cls.__new__(cls)
        # invoke parent constructor
        super(cls, instance).__init__()
        # populate members
        instance.cooperative = cooperative
        instance.entry_name = name
        instance.argument_types = tuple(argtypes)
        instance._type_annotation = None
        instance._codelibrary = codelibrary
        instance.debug = debug
        instance.lineinfo = lineinfo
        instance.call_helper = call_helper
        instance.extensions = extensions
        return instance

    def _reduce_states(self):
        """
        Reduce the instance for serialization.
        Compiled definitions are serialized in PTX form.
        Type annotation are discarded.
        Thread, block and shared memory configuration are serialized.
        Stream information is discarded.
        """
        return dict(cooperative=self.cooperative, name=self.entry_name,
                    argtypes=self.argtypes, codelibrary=self.codelibrary,
                    debug=self.debug, lineinfo=self.lineinfo,
                    call_helper=self.call_helper, extensions=self.extensions)

    def bind(self):
        """
        Force binding to current CUDA context
        """
        self._codelibrary.get_cufunc()

    @property
    def ptx(self):
        '''
        PTX code for this kernel.
        '''
        return self._codelibrary.get_asm_str()

    @property
    def device(self):
        """
        Get current active context
        """
        return get_current_device()

    @property
    def regs_per_thread(self):
        '''
        The number of registers used by each thread for this kernel.
        '''
        return self._codelibrary.get_cufunc().attrs.regs

    def inspect_llvm(self):
        '''
        Returns the LLVM IR for this kernel.
        '''
        return self._codelibrary.get_llvm_str()

    def inspect_asm(self, cc):
        '''
        Returns the PTX code for this kernel.
        '''
        return self._codelibrary.get_asm_str(cc=cc)

    def inspect_sass(self):
        '''
        Returns the SASS code for this kernel.

        Requires nvdisasm to be available on the PATH.
        '''
        return self._codelibrary.get_sass()

    def inspect_types(self, file=None):
        '''
        Produce a dump of the Python source of this function annotated with the
        corresponding Numba IR and type information. The dump is written to
        *file*, or *sys.stdout* if *file* is *None*.
        '''
        if self._type_annotation is None:
            raise ValueError("Type annotation is not available")

        if file is None:
            file = sys.stdout

        print("%s %s" % (self.entry_name, self.argument_types), file=file)
        print('-' * 80, file=file)
        print(self._type_annotation, file=file)
        print('=' * 80, file=file)

    def max_cooperative_grid_blocks(self, blockdim, dynsmemsize=0):
        '''
        Calculates the maximum number of blocks that can be launched for this
        kernel in a cooperative grid in the current context, for the given block
        and dynamic shared memory sizes.

        :param blockdim: Block dimensions, either as a scalar for a 1D block, or
                         a tuple for 2D or 3D blocks.
        :param dynsmemsize: Dynamic shared memory size in bytes.
        :return: The maximum number of blocks in the grid.
        '''
        ctx = get_context()
        cufunc = self._codelibrary.get_cufunc()

        if isinstance(blockdim, tuple):
            blockdim = functools.reduce(lambda x, y: x * y, blockdim)
        active_per_sm = ctx.get_active_blocks_per_multiprocessor(cufunc,
                                                                 blockdim,
                                                                 dynsmemsize)
        sm_count = ctx.device.MULTIPROCESSOR_COUNT
        return active_per_sm * sm_count

    def launch(self, args, griddim, blockdim, stream=0, sharedmem=0):
        # Prepare kernel
        cufunc = self._codelibrary.get_cufunc()

        if self.debug:
            excname = cufunc.name + "__errcode__"
            excmem, excsz = cufunc.module.get_global_symbol(excname)
            assert excsz == ctypes.sizeof(ctypes.c_int)
            excval = ctypes.c_int()
            excmem.memset(0, stream=stream)

        # Prepare arguments
        retr = []                       # hold functors for writeback

        kernelargs = []
        for t, v in zip(self.argument_types, args):
            self._prepare_args(t, v, stream, retr, kernelargs)

        if driver.USE_NV_BINDING:
            zero_stream = driver.binding.CUstream(0)
        else:
            zero_stream = None

        stream_handle = stream and stream.handle or zero_stream

        # Invoke kernel
        driver.launch_kernel(cufunc.handle,
                             *griddim,
                             *blockdim,
                             sharedmem,
                             stream_handle,
                             kernelargs,
                             cooperative=self.cooperative)

        if self.debug:
            driver.device_to_host(ctypes.addressof(excval), excmem, excsz)
            if excval.value != 0:
                # An error occurred
                def load_symbol(name):
                    mem, sz = cufunc.module.get_global_symbol("%s__%s__" %
                                                              (cufunc.name,
                                                               name))
                    val = ctypes.c_int()
                    driver.device_to_host(ctypes.addressof(val), mem, sz)
                    return val.value

                tid = [load_symbol("tid" + i) for i in 'zyx']
                ctaid = [load_symbol("ctaid" + i) for i in 'zyx']
                code = excval.value
                exccls, exc_args, loc = self.call_helper.get_exception(code)
                # Prefix the exception message with the source location
                if loc is None:
                    locinfo = ''
                else:
                    sym, filepath, lineno = loc
                    filepath = os.path.abspath(filepath)
                    locinfo = 'In function %r, file %s, line %s, ' % (sym,
                                                                      filepath,
                                                                      lineno,)
                # Prefix the exception message with the thread position
                prefix = "%stid=%s ctaid=%s" % (locinfo, tid, ctaid)
                if exc_args:
                    exc_args = ("%s: %s" % (prefix, exc_args[0]),) + \
                        exc_args[1:]
                else:
                    exc_args = prefix,
                raise exccls(*exc_args)

        # retrieve auto converted arrays
        for wb in retr:
            wb()

    def _prepare_args(self, ty, val, stream, retr, kernelargs):
        """
        Convert arguments to ctypes and append to kernelargs
        """

        # map the arguments using any extension you've registered
        for extension in reversed(self.extensions):
            ty, val = extension.prepare_args(
                ty,
                val,
                stream=stream,
                retr=retr)

        if isinstance(ty, types.Array):
            devary = wrap_arg(val).to_device(retr, stream)

            c_intp = ctypes.c_ssize_t

            meminfo = ctypes.c_void_p(0)
            parent = ctypes.c_void_p(0)
            nitems = c_intp(devary.size)
            itemsize = c_intp(devary.dtype.itemsize)

            ptr = driver.device_pointer(devary)

            if driver.USE_NV_BINDING:
                ptr = int(ptr)

            data = ctypes.c_void_p(ptr)

            kernelargs.append(meminfo)
            kernelargs.append(parent)
            kernelargs.append(nitems)
            kernelargs.append(itemsize)
            kernelargs.append(data)
            for ax in range(devary.ndim):
                kernelargs.append(c_intp(devary.shape[ax]))
            for ax in range(devary.ndim):
                kernelargs.append(c_intp(devary.strides[ax]))

        elif isinstance(ty, types.Integer):
            cval = getattr(ctypes, "c_%s" % ty)(val)
            kernelargs.append(cval)

        elif ty == types.float16:
            cval = ctypes.c_uint16(np.float16(val).view(np.uint16))
            kernelargs.append(cval)

        elif ty == types.float64:
            cval = ctypes.c_double(val)
            kernelargs.append(cval)

        elif ty == types.float32:
            cval = ctypes.c_float(val)
            kernelargs.append(cval)

        elif ty == types.boolean:
            cval = ctypes.c_uint8(int(val))
            kernelargs.append(cval)

        elif ty == types.complex64:
            kernelargs.append(ctypes.c_float(val.real))
            kernelargs.append(ctypes.c_float(val.imag))

        elif ty == types.complex128:
            kernelargs.append(ctypes.c_double(val.real))
            kernelargs.append(ctypes.c_double(val.imag))

        elif isinstance(ty, (types.NPDatetime, types.NPTimedelta)):
            kernelargs.append(ctypes.c_int64(val.view(np.int64)))

        elif isinstance(ty, types.Record):
            devrec = wrap_arg(val).to_device(retr, stream)
            ptr = devrec.device_ctypes_pointer
            if driver.USE_NV_BINDING:
                ptr = ctypes.c_void_p(int(ptr))
            kernelargs.append(ptr)

        elif isinstance(ty, types.BaseTuple):
            assert len(ty) == len(val)
            for t, v in zip(ty, val):
                self._prepare_args(t, v, stream, retr, kernelargs)

        else:
            raise NotImplementedError(ty, val)


class ForAll(object):
    def __init__(self, kernel, ntasks, tpb, stream, sharedmem):
        if ntasks < 0:
            raise ValueError("Can't create ForAll with negative task count: %s"
                             % ntasks)
        self.kernel = kernel
        self.ntasks = ntasks
        self.thread_per_block = tpb
        self.stream = stream
        self.sharedmem = sharedmem

    def __call__(self, *args):
        if self.ntasks == 0:
            return

        if self.kernel.specialized:
            kernel = self.kernel
        else:
            kernel = self.kernel.specialize(*args)
        blockdim = self._compute_thread_per_block(kernel)
        griddim = (self.ntasks + blockdim - 1) // blockdim

        return kernel[griddim, blockdim, self.stream, self.sharedmem](*args)

    def _compute_thread_per_block(self, kernel):
        tpb = self.thread_per_block
        # Prefer user-specified config
        if tpb != 0:
            return tpb
        # Else, ask the driver to give a good config
        else:
            ctx = get_context()
            # Kernel is specialized, so there's only one definition - get it so
            # we can get the cufunc from the code library
            defn = next(iter(kernel.overloads.values()))
            kwargs = dict(
                func=defn._codelibrary.get_cufunc(),
                b2d_func=0,     # dynamic-shared memory is constant to blksz
                memsize=self.sharedmem,
                blocksizelimit=1024,
            )
            _, tpb = ctx.get_max_potential_block_size(**kwargs)
            return tpb


class _LaunchConfiguration:
    def __init__(self, dispatcher, griddim, blockdim, stream, sharedmem):
        self.dispatcher = dispatcher
        self.griddim = griddim
        self.blockdim = blockdim
        self.stream = stream
        self.sharedmem = sharedmem

        if config.CUDA_LOW_OCCUPANCY_WARNINGS:
            ctx = get_context()
            smcount = ctx.device.MULTIPROCESSOR_COUNT
            grid_size = griddim[0] * griddim[1] * griddim[2]
            if grid_size < 2 * smcount:
                msg = ("Grid size ({grid}) < 2 * SM count ({sm}) "
                       "will likely result in GPU under utilization due "
                       "to low occupancy.")
                msg = msg.format(grid=grid_size, sm=2 * smcount)
                warn(NumbaPerformanceWarning(msg))

    def __call__(self, *args):
        return self.dispatcher.call(args, self.griddim, self.blockdim,
                                    self.stream, self.sharedmem)


class Dispatcher(uber_Dispatcher, serialize.ReduceMixin):
    '''
    CUDA Dispatcher object. When configured and called, the dispatcher will
    specialize itself for the given arguments (if no suitable specialized
    version already exists) & compute capability, and launch on the device
    associated with the current context.

    Dispatcher objects are not to be constructed by the user, but instead are
    created using the :func:`numba.cuda.jit` decorator.
    '''

    # Whether to fold named arguments and default values. Default values are
    # presently unsupported on CUDA, so we can leave this as False in all
    # cases.
    _fold_args = False

    targetdescr = cuda_target

    def __init__(self, py_func, sigs, targetoptions,
                 pipeline_class=CUDACompiler):
        # TODO: Check if this fixes the cuda docstring jit issue

        super().__init__(py_func, pipeline_class=pipeline_class)

        # CUDA-specific stuff - hopefully some of it can be removed ASAP

        self.sigs = []
        self.link = targetoptions.pop('link', (),)
        self._can_compile = True
        self._type = self._numba_type_

        # Specializations for given sets of argument types
        self.specializations = {}

        # defensive copy
        self.targetoptions['extensions'] = \
            list(self.targetoptions.get('extensions', []))

        if sigs:
            if len(sigs) > 1:
                raise TypeError("Only one signature supported at present")
            if targetoptions.get('device'):
                argtypes, restype = sigutils.normalize_signature(sigs[0])
                self.compile_device(argtypes)
            else:
                self.compile(sigs[0])

            self._can_compile = False

        #if targetoptions.get('device'):
        #    self._register_device_function()

    def _make_finalizer(self):
        # Dummy finalizer whilst _DispatcherBase assumes the existence of a
        # finalizer
        def finalizer():
            pass
        return finalizer

    def _register_device_function(self):
        dispatcher = self
        pyfunc = self.py_func

        class device_function_template(AbstractTemplate):
            key = dispatcher

            def generic(self, args, kws):
                assert not kws
                return dispatcher.compile(args).signature

            def get_template_info(cls):
                basepath = os.path.dirname(
                    os.path.dirname(os.path.dirname(cuda.__file__)))
                code, firstlineno = inspect.getsourcelines(pyfunc)
                path = inspect.getsourcefile(pyfunc)
                sig = str(utils.pysignature(pyfunc))
                info = {
                    'kind': "overload",
                    'name': getattr(cls.key, '__name__', "unknown"),
                    'sig': sig,
                    'filename': utils.safe_relpath(path, start=basepath),
                    'lines': (firstlineno, firstlineno + len(code) - 1),
                    'docstring': pyfunc.__doc__
                }
                return info

        from .descriptor import cuda_target
        typingctx = cuda_target.typing_context
        typingctx.insert_user_function(dispatcher, device_function_template)

    @property
    def _numba_type_(self):
        return cuda_types.CUDADispatcher(self)

    @property
    def is_compiling(self):
        """
        Whether a specialization is currently being compiled.
        """
        return self._compiling_counter

    def configure(self, griddim, blockdim, stream=0, sharedmem=0):
        griddim, blockdim = normalize_kernel_dimensions(griddim, blockdim)
        return _LaunchConfiguration(self, griddim, blockdim, stream, sharedmem)

    def __getitem__(self, args):
        if len(args) not in [2, 3, 4]:
            raise ValueError('must specify at least the griddim and blockdim')
        return self.configure(*args)

    def forall(self, ntasks, tpb=0, stream=0, sharedmem=0):
        """Returns a 1D-configured kernel for a given number of tasks.

        This assumes that:

        - the kernel maps the Global Thread ID ``cuda.grid(1)`` to tasks on a
          1-1 basis.
        - the kernel checks that the Global Thread ID is upper-bounded by
          ``ntasks``, and does nothing if it is not.

        :param ntasks: The number of tasks.
        :param tpb: The size of a block. An appropriate value is chosen if this
                    parameter is not supplied.
        :param stream: The stream on which the configured kernel will be
                       launched.
        :param sharedmem: The number of bytes of dynamic shared memory required
                          by the kernel.
        :return: A configured kernel, ready to launch on a set of arguments."""

        return ForAll(self, ntasks, tpb=tpb, stream=stream, sharedmem=sharedmem)

    @property
    def extensions(self):
        '''
        A list of objects that must have a `prepare_args` function. When a
        specialized kernel is called, each argument will be passed through
        to the `prepare_args` (from the last object in this list to the
        first). The arguments to `prepare_args` are:

        - `ty` the numba type of the argument
        - `val` the argument value itself
        - `stream` the CUDA stream used for the current call to the kernel
        - `retr` a list of zero-arg functions that you may want to append
          post-call cleanup work to.

        The `prepare_args` function must return a tuple `(ty, val)`, which
        will be passed in turn to the next right-most `extension`. After all
        the extensions have been called, the resulting `(ty, val)` will be
        passed into Numba's default argument marshalling logic.
        '''
        return self.targetoptions.get('extensions')

    def __call__(self, *args, **kwargs):
        # An attempt to launch an unconfigured kernel
        raise ValueError(missing_launch_config_msg)

    def call(self, args, griddim, blockdim, stream, sharedmem):
        '''
        Compile if necessary and invoke this kernel with *args*.
        '''
        if self.specialized:
            kernel = next(iter(self.overloads.values()))
        else:
            kernel = _dispatcher.Dispatcher._cuda_call(self, *args)

        kernel.launch(args, griddim, blockdim, stream, sharedmem)

    def _compile_for_args(self, *args, **kws):
        # Based on _DispatcherBase._compile_for_args.
        assert not kws
        argtypes = [self.typeof_pyval(a) for a in args]
        return self.compile(tuple(argtypes))

    def _search_new_conversions(self, *args, **kws):
        # Based on _DispatcherBase._search_new_conversions
        assert not kws
        args = [self.typeof_pyval(a) for a in args]
        found = False
        for sig in self.nopython_signatures:
            conv = self.typingctx.install_possible_conversions(args, sig.args)
            if conv:
                found = True
        return found

    def typeof_pyval(self, val):
        # Based on _DispatcherBase.typeof_pyval, but differs from it to support
        # the CUDA Array Interface.
        try:
            return typeof(val, Purpose.argument)
        except ValueError:
            if cuda.is_cuda_array(val):
                # When typing, we don't need to synchronize on the array's
                # stream - this is done when the kernel is launched.
                return typeof(cuda.as_cuda_array(val, sync=False),
                              Purpose.argument)
            else:
                raise

    @property
    def nopython_signatures(self):
        # Based on _DispatcherBase.nopython_signatures
        return [kernel.signature for kernel in self.overloads.values()]

    def specialize(self, *args):
        '''
        Create a new instance of this dispatcher specialized for the given
        *args*.
        '''
        cc = get_current_device().compute_capability
        argtypes = tuple(
            [self.typingctx.resolve_argument_type(a) for a in args])
        if self.specialized:
            raise RuntimeError('Dispatcher already specialized')

        specialization = self.specializations.get((cc, argtypes))
        if specialization:
            return specialization

        targetoptions = self.targetoptions
        targetoptions['link'] = self.link
        specialization = Dispatcher(self.py_func, [types.void(*argtypes)],
                                    targetoptions)
        self.specializations[cc, argtypes] = specialization
        return specialization

    def disable_compile(self, val=True):
        self._can_compile = not val

    @property
    def specialized(self):
        """
        True if the Dispatcher has been specialized.
        """
        return len(self.sigs) == 1 and not self._can_compile

    def get_regs_per_thread(self, signature=None):
        '''
        Returns the number of registers used by each thread in this kernel for
        the device in the current context.

        :param signature: The signature of the compiled kernel to get register
                          usage for. This may be omitted for a specialized
                          kernel.
        :return: The number of registers used by the compiled variant of the
                 kernel for the given signature and current device.
        '''
        if signature is not None:
            return self.overloads[signature.args].regs_per_thread
        if self.specialized:
            return next(iter(self.overloads.values())).regs_per_thread
        else:
            return {sig: overload.regs_per_thread
                    for sig, overload in self.overloads.items()}

    def get_call_template(self, args, kws):
        # Copied and simplified from _DispatcherBase.get_call_template.
        """
        Get a typing.ConcreteTemplate for this dispatcher and the given
        *args* and *kws* types.  This allows resolution of the return type.

        A (template, pysig, args, kws) tuple is returned.
        """
        with self._compiling_counter:
            # Ensure an exactly-matching overload is available if we can
            # compile. We proceed with the typing even if we can't compile
            # because we may be able to force a cast on the caller side.
            if self._can_compile:
                self.compile_device(tuple(args))

            # Create function type for typing
            func_name = self.py_func.__name__
            name = "CallTemplate({0})".format(func_name)

            call_template = typing.make_concrete_template(
                name, key=func_name, signatures=self.nopython_signatures)
            pysig = utils.pysignature(self.py_func)

            return call_template, pysig, args, kws

    # XXX: Delete this and call the class CUDADispatcher
    def __repr__(self):
        return f"numba.cuda.dispatcher.Dispatcher({self.py_func})"

    def compile_device(self, args):
        """Compile the device function for the given argument types.

        Each signature is compiled once by caching the compiled function inside
        this object.

        Returns the `CompileResult`.
        """
        if args not in self.overloads:

            debug = self.targetoptions.get('debug')
            inline = self.targetoptions.get('inline')

            nvvm_options = {
                'debug': debug,
                'opt': 3 if self.targetoptions.get('opt') else 0
            }

            cres = compile_cuda(self.py_func, None, args, debug=debug,
                                inline=inline, nvvm_options=nvvm_options)
            self.overloads[args] = cres

            cres.target_context.insert_user_function(cres.entry_point,
                                                     cres.fndesc,
                                                     [cres.library])
        else:
            cres = self.overloads[args]

        return cres

    def compile(self, sig):
        '''
        Compile and bind to the current context a version of this kernel
        specialized for the given signature.
        '''
        argtypes, return_type = sigutils.normalize_signature(sig)
        assert return_type is None or return_type == types.none
        if self.specialized:
            return next(iter(self.overloads.values()))
        else:
            kernel = self.overloads.get(argtypes)
        if kernel is None:
            if not self._can_compile:
                raise RuntimeError("Compilation disabled")
            kernel = _Kernel(self.py_func, argtypes, link=self.link,
                             **self.targetoptions)
            # Inspired by _DispatcherBase.add_overload, but differs slightly
            # because we're inserting a _Kernel object instead of a compiled
            # function.
            c_sig = [a._code for a in argtypes]
            self._insert(c_sig, kernel, cuda=True)
            self.overloads[argtypes] = kernel

            kernel.bind()
            self.sigs.append(sig)
        return kernel

    def inspect_llvm(self, signature=None):
        '''
        Return the LLVM IR for this kernel.

        :param signature: A tuple of argument types.
        :return: The LLVM IR for the given signature, or a dict of LLVM IR
                 for all previously-encountered signatures.

        '''
        device = self.targetoptions.get('device')
        if signature is not None:
            if device:
                return self.overloads[signature].library.get_llvm_str()
            else:
                return self.overloads[signature].inspect_llvm()
        else:
            if device:
                return {sig: overload.library.get_llvm_str()
                        for sig, overload in self.overloads.items()}
            else:
                return {sig: overload.inspect_llvm()
                        for sig, overload in self.overloads.items()}

    def inspect_asm(self, signature=None):
        '''
        Return this kernel's PTX assembly code for for the device in the
        current context.

        :param signature: A tuple of argument types.
        :return: The PTX code for the given signature, or a dict of PTX codes
                 for all previously-encountered signatures.
        '''
        cc = get_current_device().compute_capability
        device = self.targetoptions.get('device')
        if signature is not None:
            if device:
                return self.overloads[signature].library.get_asm_str(cc)
            else:
                return self.overloads[signature].inspect_asm(cc)
        else:
            if device:
                return {sig: overload.library.get_asm_str(cc)
                        for sig, overload in self.overloads.items()}
            else:
                return {sig: overload.inspect_asm(cc)
                        for sig, overload in self.overloads.items()}

    def inspect_sass(self, signature=None):
        '''
        Return this kernel's SASS assembly code for for the device in the
        current context.

        :param signature: A tuple of argument types.
        :return: The SASS code for the given signature, or a dict of SASS codes
                 for all previously-encountered signatures.

        SASS for the device in the current context is returned.

        Requires nvdisasm to be available on the PATH.
        '''
        if self.targetoptions.get('device'):
            raise RuntimeError('Cannot inspect SASS of a device function')

        if signature is not None:
            return self.overloads[signature].inspect_sass()
        else:
            return {sig: defn.inspect_sass()
                    for sig, defn in self.overloads.items()}

    def inspect_types(self, file=None):
        '''
        Produce a dump of the Python source of this function annotated with the
        corresponding Numba IR and type information. The dump is written to
        *file*, or *sys.stdout* if *file* is *None*.
        '''
        if file is None:
            file = sys.stdout

        for _, defn in self.overloads.items():
            defn.inspect_types(file=file)

    @property
    def ptx(self):
        return {sig: overload.ptx for sig, overload in self.overloads.items()}

    def bind(self):
        for defn in self.overloads.values():
            defn.bind()

    @classmethod
    def _rebuild(cls, py_func, sigs, targetoptions):
        """
        Rebuild an instance.
        """
        instance = cls(py_func, sigs, targetoptions)
        return instance

    def _reduce_states(self):
        """
        Reduce the instance for serialization.
        Compiled definitions are discarded.
        """
        return dict(py_func=self.py_func, sigs=self.sigs,
                    targetoptions=self.targetoptions)
