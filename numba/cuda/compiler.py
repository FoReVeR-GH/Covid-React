from numba.core.typing.templates import ConcreteTemplate
from numba.core import types, typing, funcdesc, config, compiler
from numba.core.compiler import (CompilerBase, DefaultPassBuilder,
                                 compile_result, Flags, Option)
from numba.core.compiler_lock import global_compiler_lock
from numba.core.compiler_machinery import (LoweringPass, AnalysisPass,
                                           PassManager, register_pass)
from numba.core.errors import NumbaInvalidConfigWarning, TypingError
from numba.core.typed_passes import (IRLegalization, NativeLowering,
                                     AnnotateTypes)
from warnings import warn
from .cudadrv.devices import get_context
from .api import get_current_device


def _nvvm_options_type(x):
    if x is None:
        return None

    else:
        assert isinstance(x, dict)
        return x


class CUDAFlags(Flags):
    nvvm_options = Option(
        type=_nvvm_options_type,
        default=None,
        doc="NVVM options",
    )


@register_pass(mutates_CFG=True, analysis_only=False)
class CUDABackend(LoweringPass):

    _name = "cuda_backend"

    def __init__(self):
        LoweringPass.__init__(self)

    def run_pass(self, state):
        """
        Back-end: Packages lowering output in a compile result
        """
        lowered = state['cr']
        signature = typing.signature(state.return_type, *state.args)

        state.cr = compile_result(
            typing_context=state.typingctx,
            target_context=state.targetctx,
            typing_error=state.status.fail_reason,
            type_annotation=state.type_annotation,
            library=state.library,
            call_helper=lowered.call_helper,
            signature=signature,
            fndesc=lowered.fndesc,
        )
        return True


@register_pass(mutates_CFG=False, analysis_only=False)
class CreateLibrary(LoweringPass):
    """
    Create a CUDACodeLibrary for the NativeLowering pass to populate. The
    NativeLowering pass will create a code library if none exists, but we need
    to set it up with nvvm_options from the flags if they are present.
    """

    _name = "create_library"

    def __init__(self):
        LoweringPass.__init__(self)

    def run_pass(self, state):
        codegen = state.targetctx.codegen()
        name = state.func_id.func_qualname
        nvvm_options = state.flags.nvvm_options
        state.library = codegen.create_library(name, nvvm_options=nvvm_options)
        # Enable object caching upfront so that the library can be serialized.
        state.library.enable_object_caching()

        return True


@register_pass(mutates_CFG=False, analysis_only=True)
class CUDALegalization(AnalysisPass):

    _name = "cuda_legalization"

    def __init__(self):
        AnalysisPass.__init__(self)

    def run_pass(self, state):
        # Early return if NVVM 7
        from numba.cuda.cudadrv.nvvm import NVVM
        if NVVM().is_nvvm70:
            return False
        # NVVM < 7, need to check for charseq
        typmap = state.typemap

        def check_dtype(dtype):
            if isinstance(dtype, (types.UnicodeCharSeq, types.CharSeq)):
                msg = (f"{k} is a char sequence type. This type is not "
                       "supported with CUDA toolkit versions < 11.2. To "
                       "use this type, you need to update your CUDA "
                       "toolkit - try 'conda install cudatoolkit=11' if "
                       "you are using conda to manage your environment.")
                raise TypingError(msg)
            elif isinstance(dtype, types.Record):
                for subdtype in dtype.fields.items():
                    # subdtype is a (name, _RecordField) pair
                    check_dtype(subdtype[1].type)

        for k, v in typmap.items():
            if isinstance(v, types.Array):
                check_dtype(v.dtype)
        return False


class CUDACompiler(CompilerBase):
    def define_pipelines(self):
        dpb = DefaultPassBuilder
        pm = PassManager('cuda')

        untyped_passes = dpb.define_untyped_pipeline(self.state)
        pm.passes.extend(untyped_passes.passes)

        typed_passes = dpb.define_typed_pipeline(self.state)
        pm.passes.extend(typed_passes.passes)
        pm.add_pass(CUDALegalization, "CUDA legalization")

        lowering_passes = self.define_cuda_lowering_pipeline(self.state)
        pm.passes.extend(lowering_passes.passes)

        pm.finalize()
        return [pm]

    def define_cuda_lowering_pipeline(self, state):
        pm = PassManager('cuda_lowering')
        # legalise
        pm.add_pass(IRLegalization,
                    "ensure IR is legal prior to lowering")
        pm.add_pass(AnnotateTypes, "annotate types")

        # lower
        pm.add_pass(CreateLibrary, "create library")
        pm.add_pass(NativeLowering, "native lowering")
        pm.add_pass(CUDABackend, "cuda backend")

        pm.finalize()
        return pm


@global_compiler_lock
def compile_cuda(pyfunc, return_type, args, debug=False, lineinfo=False,
                 inline=False, fastmath=False, nvvm_options=None):
    from .descriptor import cuda_target
    typingctx = cuda_target.typing_context
    targetctx = cuda_target.target_context

    flags = CUDAFlags()
    # Do not compile (generate native code), just lower (to LLVM)
    flags.no_compile = True
    flags.no_cpython_wrapper = True
    flags.no_cfunc_wrapper = True
    if debug or lineinfo:
        # Note both debug and lineinfo turn on debug information in the
        # compiled code, but we keep them separate arguments in case we
        # later want to overload some other behavior on the debug flag.
        # In particular, -opt=3 is not supported with -g.
        flags.debuginfo = True
    if inline:
        flags.forceinline = True
    if fastmath:
        flags.fastmath = True
    if nvvm_options:
        flags.nvvm_options = nvvm_options

    # Run compilation pipeline
    cres = compiler.compile_extra(typingctx=typingctx,
                                  targetctx=targetctx,
                                  func=pyfunc,
                                  args=args,
                                  return_type=return_type,
                                  flags=flags,
                                  locals={},
                                  pipeline_class=CUDACompiler)

    library = cres.library
    library.finalize()

    return cres


@global_compiler_lock
def compile_ptx(pyfunc, args, debug=False, lineinfo=False, device=False,
                fastmath=False, cc=None, opt=True):
    """Compile a Python function to PTX for a given set of argument types.

    :param pyfunc: The Python function to compile.
    :param args: A tuple of argument types to compile for.
    :param debug: Whether to include debug info in the generated PTX.
    :type debug: bool
    :param lineinfo: Whether to include a line mapping from the generated PTX
                     to the source code. Usually this is used with optimized
                     code (since debug mode would automatically include this),
                     so we want debug info in the LLVM but only the line
                     mapping in the final PTX.
    :type lineinfo: bool
    :param device: Whether to compile a device function. Defaults to ``False``,
                   to compile global kernel functions.
    :type device: bool
    :param fastmath: Whether to enable fast math flags (ftz=1, prec_sqrt=0,
                     prec_div=, and fma=1)
    :type fastmath: bool
    :param cc: Compute capability to compile for, as a tuple ``(MAJOR, MINOR)``.
               Defaults to ``(5, 2)``.
    :type cc: tuple
    :param opt: Enable optimizations. Defaults to ``True``.
    :type opt: bool
    :return: (ptx, resty): The PTX code and inferred return type
    :rtype: tuple
    """
    if debug and opt:
        msg = ("debug=True with opt=True (the default) "
               "is not supported by CUDA. This may result in a crash"
               " - set debug=False or opt=False.")
        warn(NumbaInvalidConfigWarning(msg))

    nvvm_options = {
        'debug': debug,
        'lineinfo': lineinfo,
        'fastmath': fastmath,
        'opt': 3 if opt else 0
    }

    cres = compile_cuda(pyfunc, None, args, debug=debug, lineinfo=lineinfo,
                        nvvm_options=nvvm_options)
    resty = cres.signature.return_type
    if device:
        lib = cres.library
    else:
        tgt = cres.target_context
        filename = cres.type_annotation.filename
        linenum = int(cres.type_annotation.linenum)
        lib, kernel = tgt.prepare_cuda_kernel(cres.library, cres.fndesc, debug,
                                              nvvm_options, filename, linenum)

    cc = cc or config.CUDA_DEFAULT_PTX_CC
    ptx = lib.get_asm_str(cc=cc)
    return ptx, resty


def compile_ptx_for_current_device(pyfunc, args, debug=False, lineinfo=False,
                                   device=False, fastmath=False, opt=True):
    """Compile a Python function to PTX for a given set of argument types for
    the current device's compute capabilility. This calls :func:`compile_ptx`
    with an appropriate ``cc`` value for the current device."""
    cc = get_current_device().compute_capability
    return compile_ptx(pyfunc, args, debug=debug, lineinfo=lineinfo,
                       device=device, fastmath=fastmath, cc=cc, opt=True)


def declare_device_function(name, restype, argtypes):
    from .descriptor import cuda_target
    typingctx = cuda_target.typing_context
    targetctx = cuda_target.target_context
    sig = typing.signature(restype, *argtypes)
    extfn = ExternFunction(name, sig)

    class device_function_template(ConcreteTemplate):
        key = extfn
        cases = [sig]

    fndesc = funcdesc.ExternalFunctionDescriptor(
        name=name, restype=restype, argtypes=argtypes)
    typingctx.insert_user_function(extfn, device_function_template)
    targetctx.insert_user_function(extfn, fndesc)
    return extfn


class ExternFunction(object):
    def __init__(self, name, sig):
        self.name = name
        self.sig = sig


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
