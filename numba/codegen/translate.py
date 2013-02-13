import ast
import ctypes

import llvm
import llvm.core as lc
import llvm.passes as lp
import llvm.ee as le

from numba.llvm_types import _int1, _int32, _LLVMCaster
from numba.multiarray_api import MultiarrayAPI # not used
from numba.symtab import Variable
from numba import typesystem

from numba import *

from numba.codegen.debug import *
from numba.codegen.codeutils import llvm_alloca
from numba.codegen import (coerce, complexsupport, refcounting, llvmwrapper,
                           llvmcontext)
from numba.codegen.llvmcontext import LLVMContextManager

from numba import visitors, nodes, llvm_types, utils, function_util
from numba.minivect import minitypes, llvm_codegen
from numba import ndarray_helpers, error, extension_types
from numba.typesystem import is_obj, promote_to_native
from numba.utils import dump
from numba import naming, metadata
from numba.functions import keep_alive
from numba.control_flow import ssa

_int32_zero = lc.Constant.int(_int32, 0)


_compare_mapping_float = {'>':lc.FCMP_OGT,
                           '<':lc.FCMP_OLT,
                           '==':lc.FCMP_OEQ,
                           '>=':lc.FCMP_OGE,
                           '<=':lc.FCMP_OLE,
                           '!=':lc.FCMP_ONE}

_compare_mapping_sint = {'>':lc.ICMP_SGT,
                          '<':lc.ICMP_SLT,
                          '==':lc.ICMP_EQ,
                          '>=':lc.ICMP_SGE,
                          '<=':lc.ICMP_SLE,
                          '!=':lc.ICMP_NE}

_compare_mapping_uint = {'>':lc.ICMP_UGT,
                          '<':lc.ICMP_ULT,
                          '==':lc.ICMP_EQ,
                          '>=':lc.ICMP_UGE,
                          '<=':lc.ICMP_ULE,
                          '!=':lc.ICMP_NE}


# TODO: use composition instead of mixins

class LLVMCodeGenerator(visitors.NumbaVisitor,
                        complexsupport.ComplexSupportMixin,
                        refcounting.RefcountingMixin,
                        visitors.NoPythonContextMixin):
    """
    Translate a Python AST to LLVM. Each visit_* method should directly
    return an LLVM value.
    """

    multiarray_api = MultiarrayAPI()

    def __init__(self, context, func, ast, func_signature, symtab,
                 optimize=True, nopython=False,
                 llvm_module=None, refcount_args=True, **kwds):

        super(LLVMCodeGenerator, self).__init__(
                    context, func, ast, func_signature=func_signature,
                    nopython=nopython, symtab=symtab,
                    llvm_module=llvm_module,
                    **kwds)

        # FIXME: Change mangled_name to some other attribute,
        # optionally read in the environment.  What we really want to
        # distiguish between is the name of the LLVM function being
        # generated and the name of the Python function being
        # translated.
        if 'mangled_name' in kwds and kwds['mangled_name'] is not None:
            self.mangled_name = kwds['mangled_name']
        else:
            self.mangled_name = naming.specialized_mangle(
                        self.qualified_name, func_signature.args)

        self.func_signature = func_signature
        self.blocks = {} # stores id => basic-block

        self.refcount_args = refcount_args

        # self.ma_obj = None # What is this?
        self.optimize = optimize
        self.flags = kwds

        # internal states
        self._nodes = []  # for tracking parent nodes

    # ________________________ visitors __________________________

    @property
    def current_node(self):
        return self._nodes[-1]

    def visit(self, node):
        # logger.debug('visiting %s', ast.dump(node))
        try:
            fn = getattr(self, 'visit_%s' % type(node).__name__)
        except AttributeError as e:
            # logger.exception(e)
            logger.error('Unhandled visit to %s', ast.dump(node))
            raise
            #raise compiler_errors.InternalError(node, 'Not yet implemented.')
        else:
            try:
                self._nodes.append(node) # push current node
                return fn(node)
            except Exception as e:
                # logger.exception(e)
                raise
            finally:
                self._nodes.pop() # pop current node

    # _________________________________________________________________________

    def _load_arg_by_ref(self, argtype, larg):
        if (minitypes.pass_by_ref(argtype) and
                self.func_signature.struct_by_reference):
            larg = self.builder.load(larg)

        return larg

    def _allocate_arg_local(self, name, argtype, larg):
        """
        Allocate a local variable on the stack.
        """
        stackspace = self.alloca(argtype)
        stackspace.name = name
        self.builder.store(larg, stackspace)
        return stackspace

    def renameable(self, variable):
        renameable = self.have_cfg and (not variable or variable.renameable)
        return renameable

    def incref_arg(self, argname, argtype, larg, variable):
        # TODO: incref objects in structs
        if not (self.nopython or argtype.is_closure_scope):
            if is_obj(variable.type) and self.refcount_args:
                if self.renameable(variable):
                    lvalue = self._allocate_arg_local(argname, argtype,
                                                      variable.lvalue)
                else:
                    lvalue = variable.lvalue

                self.object_local_temps[argname] = lvalue
                self.incref(larg)

    def _init_constants(self):
        pass
        # self.symtab["None"]

    def _init_args(self):
        """
        Unpack arguments:

            1) Intialize SSA variables
            2) Handle variables declared in the 'locals' dict
        """
        for larg, argname, argtype in zip(self.lfunc.args, self.argnames,
                                          self.func_signature.args):
            larg.name = argname
            variable = self.symtab.get(argname, None)

            if self.renameable(variable):
                if argtype.is_struct:
                    larg = self._allocate_arg_local(argname, argtype, larg)

                # Set value on first definition of the variable
                if argtype.is_closure_scope:
                    variable = self.symtab[argname]
                else:
                    variable = self.symtab.lookup_renamed(argname, 0)

                variable.lvalue = self._load_arg_by_ref(argtype, larg)
            elif argname in self.locals or variable.is_cellvar:
                # Allocate on stack
                variable = self.symtab[argname]
                variable.lvalue = self._allocate_arg_local(argname, argtype,
                                                           larg)

            #else:
            #    raise error.InternalError(argname, argtype)

            self.incref_arg(argname, argtype, larg, variable)

            if variable.type.is_array:
                self.preload_attributes(variable, variable.lvalue)

    def c_array_to_pointer(self, name, stackspace, var):
        "Decay a C array to a pointer to allow pointer access"
        ltype = var.type.base_type.pointer().to_llvm(self.context)
        pointer = self.builder.alloca(ltype, name=name + "_p")
        p = self.builder.gep(stackspace, [llvm_types.constant_int(0),
                                          llvm_types.constant_int(0)])
        self.builder.store(p, pointer)
        stackspace = pointer
        return stackspace

    def _allocate_locals(self):
        """
        Allocate local variables:

            1) Intialize SSA variables
            2) Handle variables declared in the 'locals' dict
        """
        for name, var in self.symtab.items():
            # FIXME: 'None' should be handled as a special case (probably).
            if var.type.is_uninitialized and var.cf_references:
                assert var.uninitialized_value, var
                var.lvalue = self.visit(var.uninitialized_value)
            elif name in self.locals and not name in self.argnames:
                # var = self.symtab.lookup_renamed(name, 0)
                name = 'var_%s' % var.name
                if is_obj(var.type):
                    lvalue = self._null_obj_temp(name, type=var.ltype)
                else:
                    lvalue = self.builder.alloca(var.ltype, name=name)

                if var.type.is_struct:
                    # TODO: memset struct to 0
                    pass
                elif var.type.is_carray:
                    lvalue = self.c_array_to_pointer(name, lvalue, var)

#                if is_obj(var.type):
#                    lvalue = llvm.core.Constant.null(var.ltype)
#                elif var.type.is_struct:
#                    lvalue = self.builder.alloca(var.ltype, name=name)
#                elif var.type.is_carray:
#                    lvalue = self.c_array_to_pointer(name, stackspace, var)
#                else:
#                    lvalue = llvm.core.Constant.undef(var.ltype)

                var.lvalue = lvalue

    def setup_func(self):
        have_return = getattr(self.ast, 'have_return', None)
        if have_return is not None:
            if not have_return and not self.func_signature.return_type.is_void:
                self.error(self.ast, "Function with non-void return does "
                                     "not return a value")

        self.lfunc_type = self.to_llvm(self.func_signature)

        self.lfunc = self.llvm_module.add_function(self.lfunc_type,
                                                   self.mangled_name)
        if not isinstance(self.ast, nodes.FunctionWrapperNode):
            assert self.mangled_name == self.lfunc.name, \
                   "Redefinition of function %s (%s, %s)" % (self.func_name,
                                                             self.mangled_name,
                                                             self.lfunc.name)

        entry = self.append_basic_block('entry')

        self.builder = lc.Builder.new(entry)
        self.caster = _LLVMCaster(self.builder)
        self.object_coercer = coerce.ObjectCoercer(self)
        self.multiarray_api.set_PyArray_API(self.llvm_module)
        self.tbaa = metadata.TBAAMetadata(self.llvm_module)

        self.object_local_temps = {}
        self._init_constants()
        self._init_args()
        self._allocate_locals()

        # TODO: Put current function into symbol table for recursive call
        self.setup_return()

        if self.have_cfg:
            block0 = self.ast.flow.blocks[0]
            block0.entry_block = entry
            self.visitlist(block0.body)
            block0.exit_block = self.builder.basic_block
            self.flow_block = None
            # self.visitlist(block0.body) # uninitialize constants for variables
            self.flow_block = self.ast.flow.blocks[1]
        else:
            self.flow_block = None

        self.in_loop = 0
        self.loop_beginnings = []
        self.loop_exits = []

    def to_llvm(self, type):
        return type.to_llvm(self.context)

    def translate(self):
        self.lfunc = None
        try:
            self.setup_func()
            if isinstance(self.ast, ast.FunctionDef):
                # Handle the doc string for the function
                # FIXME: Ignoring it for now
                if (isinstance(self.ast.body[0], ast.Expr) and
                    isinstance(self.ast.body[0].value, ast.Str)):
                    # Python doc string
                    logger.info('Ignoring python doc string.')
                    statements = self.ast.body[1:]
                else:
                    statements = self.ast.body

                for node in statements: # do codegen for each statement
                    self.visit(node)
            else:
                self.visit(self.ast)

            if not self.is_block_terminated():
                # self.builder.ret_void()
                self.builder.branch(self.cleanup_label)

            self.handle_phis()
            self.terminate_cleanup_blocks()

            # Done code generation
            del self.builder  # release the builder to make GC happy

            if logger.level >= logging.DEBUG:
                # logger.debug("ast translated function: %s" % self.lfunc)
                logger.debug(self.llvm_module)

            # Verify code generation
            self.llvm_module.verify()  # only Module level verification checks everything.

            # Reove reference to self.llvm_module
            # This may be destroyed later due to linkage
            del self.llvm_module

        except:
            # Delete the function to prevent an invalid function from living in the module
            if self.lfunc is not None:
                self.lfunc.delete()
            raise

    def link(self):
        '''Link the lfunc into the execution context
        '''
        self.lfunc = LLVMContextManager().link(self.lfunc)

    def handle_phis(self):
        """
        Update all our phi nodes after translation is done and all Variables
        have their llvm values set.
        """
        if not self.have_cfg:
            return

        # Initialize uninitialized incoming values to bad values
        for phi in ssa.iter_phi_vars(self.ast.flow):
            if phi.type.is_uninitialized:
                #print incoming_var.cf_references
                #print phi_node.variable.cf_references
                #print "incoming", phi_node.incoming, block

                assert phi.uninitialized_value, phi
                assert phi.lvalue is None
                phi.lvalue = self.visit(phi.uninitialized_value)

        # Add all incoming values to all our phi values
        ssa.handle_phis(self.ast.flow)

    def get_ctypes_func(self, llvm=True):
        import ctypes
        sig = self.func_signature
        restype = typesystem.convert_to_ctypes(sig.return_type)

        # FIXME: Switch to PYFUNCTYPE so it does not release the GIL.
        #
        #    prototype = ctypes.CFUNCTYPE(restype,
        #                                 *[_types.convert_to_ctypes(x)
        #                                       for x in sig.args])
        prototype = ctypes.PYFUNCTYPE(restype,
                                     *[typesystem.convert_to_ctypes(x)
                                           for x in sig.args])


        if hasattr(restype, 'make_ctypes_prototype_wrapper'):
            # See numba.utils.ComplexMixin for an example of
            # make_ctypes_prototype_wrapper().
            prototype = restype.make_ctypes_prototype_wrapper(prototype)

        if llvm:
            # July 10, 2012: PY_CALL_TO_LLVM_CALL_MAP is removed recent commit.
            #
            #    PY_CALL_TO_LLVM_CALL_MAP[self.func] = \
            #        self.build_call_to_translated_function
            return prototype(self.lfunc_pointer)
        else:
            return prototype(self.func)

    def _build_wrapper_function_ast(self, fake_pyfunc, llvm_module = None):
        if llvm_module is None:
            lfunc = self.lfunc
        else:
            lfunc = llvm_module.get_or_insert_function(
                self.func_signature.to_llvm(self.context), self.lfunc.name)
        wrapper = nodes.FunctionWrapperNode(lfunc,
                                            self.func_signature,
                                            self.func,
                                            fake_pyfunc,
                                            self.func_name)
        wrapper.pipeline = self.ast.pipeline
        wrapper.cellvars = []
        return wrapper

    def _build_wrapper_translation(self, llvm_module=None):
        # PyObject *(*)(PyObject *self, PyObject *args)
        def func(self, args):
            pass
        # func.live_objects = self.func.live_objects

        if llvm_module:
            wrapper_module = llvm_module
        else:
            wrapper_module = LLVMContextManager().module

        # Create wrapper code generator and wrapper AST
        func_name = '__numba_wrapper_%s' % self.func_name
        signature = minitypes.FunctionType(return_type=object_,
                                           args=[void.pointer(), object_])
        symtab = dict(self=Variable(object_, is_local=True),
                      args=Variable(object_, is_local=True))
        wrapper_call = self._build_wrapper_function_ast(
            func, llvm_module=wrapper_module)
        error_return = ast.Return(nodes.CoercionNode(nodes.NULL_obj,
                                                     object_))
        wrapper_call.error_return = error_return

        t = LLVMCodeGenerator(self.context, func, wrapper_call, signature,
                              symtab, llvm_module=wrapper_module,
                              locals={}, refcount_args=False,
                              func_name=func_name)
        t.translate()
        # func.live_objects.append(t.lfunc)
        keep_alive(func, t.lfunc)
        return t

    def build_wrapper_function(self, get_lfunc=False):
        '''
        Build a wrapper function for the currently translated function.

        By default, or if get_lfunc is false, return the
        interpreter-level wrapper function.

        If the optional get_lfunc argument is true, return the
        interpreter-level wrapper function, the LLVM wrapper function,
        and the method definition record.
        '''
        t = self._build_wrapper_translation()

        # Return a PyCFunctionObject holding the wrapper
        func_pointer = t.lfunc_pointer
        methoddef, wrapper = llvmwrapper.numbafunction_new(
                self.func, self.func_name, self.func_doc, self.module_name,
                func_pointer, self.lfunc_pointer, self.func_signature)

        if get_lfunc:
            return wrapper, t.lfunc, methoddef

        return wrapper

    def build_wrapper_module(self):
        '''
        Build a wrapper function for the currently translated
        function, and return a tuple containing the separate LLVM
        module, and the LLVM wrapper function.
        '''
        llvm_module = lc.Module.new('%s_wrapper_module' % self.mangled_name)
        t = self._build_wrapper_translation(llvm_module=llvm_module)
        logger.debug('Wrapper module: %s' % llvm_module)
        return llvm_module, t.lfunc

    def insert_closure_scope_arg(self, args, node):
        """
        Retrieve the closure from the NumbaFunction passed in as m_self
        """
        closure_scope_type = node.signature.args[0]
        offset = extension_types.numbafunc_closure_field_offset
        closure = nodes.LLVMValueRefNode(void.pointer(), self.lfunc.args[0])
        closure = nodes.CoercionNode(closure, char.pointer())
        closure = nodes.pointer_add(closure, nodes.const(offset, size_t))
        closure = nodes.CoercionNode(closure, closure_scope_type.pointer())
        closure = nodes.DereferenceNode(closure)
        args.insert(0, closure)

    def visit_FunctionWrapperNode(self, node):
        global debug_conversion
        from numba import  pipeline

        was_debug_conversion = debug_conversion
        if debug_conversion:
            debug_conversion = False

        args_tuple = self.lfunc.args[1]
        arg_types = [object_] * len(node.signature.args)

        if self.is_closure_signature(node.signature):
            # Wrapper expects closure scope as first argument, which we
            # have as the m_self attribute
            arg_types.pop()

        # Unpack tuple into arguments
        types, lstr = self.object_coercer.lstr(arg_types)
        #if debug_conversion:
        #    self.puts("args tuple:")
        #    nodes.print_llvm(self, object_, args_tuple)

        largs = self.object_coercer.parse_tuple(lstr, args_tuple, arg_types)

        # Call wrapped function
        args = [nodes.LLVMValueRefNode(arg_type, larg)
                    for arg_type, larg in zip(arg_types, largs)]

        if self.is_closure_signature(node.signature):
            # Insert m_self as scope argument type
            self.insert_closure_scope_arg(args, node)

        func_call = nodes.NativeCallNode(node.signature, args,
                                         node.wrapped_function)

        if not is_obj(node.signature.return_type):
            # Check for error using PyErr_Occurred()
            func_call = nodes.PyErr_OccurredNode(func_call)

        # Coerce and return result
        if node.signature.return_type.is_void:
            node.body = func_call
            result_node = nodes.ObjectInjectNode(None)
        else:
            node.body = None
            result_node = func_call

        # self.puts("calling wrapped function %r" % node.orig_py_func.__name__)
        node.return_result = ast.Return(
                    value=nodes.CoercionNode(result_node, object_))

        # We need to specialize the return statement, since it may be a
        # non-trivial coercion (e.g. call a ctypes pointer type for pointer
        # return types, etc)
        pipeline_ = pipeline.Pipeline(self.context, node.fake_pyfunc,
                                      node, self.func_signature,
                                      order=['late_specializer',
                                             'specialize_funccalls',
                                             'specialize_exceptions'])
        sig, symtab, return_stmt_ast = pipeline_.run_pipeline()
        self.generic_visit(return_stmt_ast)

        debug_conversion = was_debug_conversion

    @property
    def lfunc_pointer(self):
        return LLVMContextManager().get_pointer_to_function(self.lfunc)

    def _null_obj_temp(self, name, type=None, change_bb=False):
        if change_bb:
            bb = self.builder.basic_block
        lhs = self.llvm_alloca(type or llvm_types._pyobject_head_struct_p,
                               name=name, change_bb=False)
        self.generate_assign_stack(self.visit(nodes.NULL_obj), lhs,
                                   tbaa_type=object_)
        if change_bb:
            self.builder.position_at_end(bb)
        return lhs

    def load_tbaa(self, ptr, tbaa_type, name=''):
        """
        Load a pointer and annotate with Type Based Alias Analysis
        metadata.
        """
        instr = self.builder.load(ptr, name='')
        self.tbaa.set_tbaa(instr, tbaa_type)
        return instr

    def store_tbaa(self, value, ptr, tbaa_type):
        """
        Load a pointer and annotate with Type Based Alias Analysis
        metadata.
        """
        instr = self.builder.store(value, ptr)
        if metadata.is_tbaa_type(tbaa_type):
            self.tbaa.set_tbaa(instr, tbaa_type)

    def puts(self, msg):
        const = nodes.ConstNode(msg, c_string_type)
        self.visit(function_util.external_call(self.context,
                                               self.llvm_module,
                                               'puts',
                                               args=[const]))

    def puts_llvm(self, llvm_string):
        const = nodes.LLVMValueRefNode(c_string_type, llvm_string)
        self.visit(function_util.external_call(self.context,
                                               self.llvm_module,
                                               'puts',
                                               args=[const]))

    def setup_return(self):
        # Assign to this value which will be returned
        self.is_void_return = \
                self.func_signature.actual_signature.return_type.is_void
        if self.func_signature.struct_by_reference:
            self.return_value = self.lfunc.args[-1]
        elif not self.is_void_return:
            llvm_ret_type = self.func_signature.return_type.to_llvm(self.context)
            self.return_value = self.builder.alloca(llvm_ret_type,
                                                    "return_value")

        # All non-NULL object emporaries are DECREFed here
        self.cleanup_label = self.append_basic_block('cleanup_label')
        self.current_cleanup_bb = self.cleanup_label

        bb = self.builder.basic_block
        # Jump here in case of an error
        self.error_label = self.append_basic_block("error_label")
        self.builder.position_at_end(self.error_label)
        # Set error return value and jump to cleanup
        self.visit(self.ast.error_return)
        self.builder.position_at_end(bb)

    def terminate_cleanup_blocks(self):
        self.builder.position_at_end(self.current_cleanup_bb)

        # Decref local variables
        for name, stackspace in self.object_local_temps.iteritems():
            self.xdecref_temp(stackspace)

        if self.is_void_return:
            self.builder.ret_void()
        else:
            ret_type = self.func_signature.return_type
            self.builder.ret(self.builder.load(self.return_value))

    # __________________________________________________________________________

    def visit_Expr(self, node):
        return self.visit(node.value)

    def visit_Pass(self, node):
        pass

    def visit_ConstNode(self, node):
        return node.value(self)

    def visit_Attribute(self, node):
        raise error.NumbaError("This node should have been replaced")

    def visit_ComplexAttributeNode(self, node):
        result = self.visit(node.value)
        if node.value.type.is_complex:
            if node.attr == 'real':
                return self.builder.extract_value(result, 0)
            elif node.attr == 'imag':
                return self.builder.extract_value(result, 1)

    def struct_field(self, node, value):
        value = self.builder.gep(
            value, [llvm_types.constant_int(0),
                    llvm_types.constant_int(node.field_idx)])
        return value

    def visit_StructAttribute(self, node):
        result = self.visit(node.value)
        value_is_reference = node.value.type.is_reference

        # TODO: TBAA for loads
        if isinstance(node.ctx, ast.Load):
            if value_is_reference:
                # Struct reference, load result
                result = self.struct_field(node, result)
                result = self.builder.load(result)
            else:
                result = self.builder.extract_value(result, node.field_idx)
        else:
            if value_is_reference:
                # Load alloca-ed struct pointer
                result = self.builder.load(result)
            result = self.struct_field(node, result)
            #result = self.builder.insert_value(result, self.rhs_lvalue,
            #                                   node.field_idx)

        return result

    def visit_StructVariable(self, node):
        return self.visit(node.node)

    def generate_assign_stack(self, lvalue, ltarget,
                              tbaa_node=None, tbaa_type=None,
                              decref=False, incref=False):
        """
        Generate assignment operation and automatically cast value to
        match the target type.
        """
        if lvalue.type != ltarget.type.pointee:
            lvalue = self.caster.cast(lvalue, ltarget.type.pointee)

        if incref:
            self.incref(lvalue)
        if decref:
            # Py_XDECREF any previous object
            self.xdecref_temp(ltarget)

        instr = self.builder.store(lvalue, ltarget)

        # Set TBAA on store instruction
        if tbaa_node is None:
            assert tbaa_type is not None
            tbaa_node = self.tbaa.get_metadata(tbaa_type)

        self.tbaa.set_metadata(instr, tbaa_node)

    def visit_Assign(self, node):
        target_node = node.targets[0]
        # print target_node
        is_object = is_obj(target_node.type)
        value = self.visit(node.value)

        incref = is_object
        decref = is_object

        if (isinstance(target_node, ast.Name) and
                self.renameable(target_node.variable)):
            # phi nodes are in place for the variable
            target_node.variable.lvalue = value
            if not is_object:
                # No worries about refcounting, we are done
                return

            # Refcount SSA variables
            # TODO: use ObjectTempNode?
            if target_node.id not in self.object_local_temps:
                target = self._null_obj_temp(target_node.id, change_bb=True)
                self.object_local_temps[target_node.id] = target
                decref = bool(self.loop_beginnings)
            else:
                target = self.object_local_temps[target_node.id]

            tbaa_node = self.tbaa.get_metadata(target_node.type)
        else:
            target = self.visit(target_node)

            if isinstance(target_node, nodes.TempStoreNode):
                # Use TBAA specific to only this temporary
                tbaa_node = target_node.temp.get_tbaa_node(self.tbaa)
            else:
                # ast.Attribute | ast.Subscript  store.
                # These types don't have pointer types but rather
                # scalar values
                if is_obj(target_node.type):
                    target_type = object_
                else:
                    target_type = target_node.type
                tbaa_type = target_type.pointer()
                tbaa_node = self.tbaa.get_metadata(tbaa_type)

        # INCREF RHS. Note that new references are always in temporaries, and
        # hence we can only borrow references, and have to make it an owned
        # reference
        self.generate_assign_stack(value, target, tbaa_node,
                                   decref=decref, incref=incref)

        self.preload_attributes(target_node.variable, value)

    def preload_attributes(self, var, value):
        """
        Pre-load ndarray attributes data/shape/strides.
        """
        if not (var.preload_data or var.preload_shape or var.preload_strides):
            return

        if not var.renameable:
            # Stack allocated variable
            var = self.builder.load(value)

        acc = self.pyarray_accessor(value, var.type.dtype)

        if var.preload_data:
            var.preloaded_data = acc.data

        if var.preload_shape:
            shape = nodes.get_strides(self.builder, self.tbaa,
                                      acc.shape, var.type.ndim)
            var.preloaded_shape = tuple(shape)

        if var.preload_strides:
            strides = nodes.get_strides(self.builder, self.tbaa,
                                        acc.strides, var.type.ndim)
            var.preloaded_strides = tuple(strides)

    def visit_Num(self, node):
        if node.type.is_int:
            return self.generate_constant_int(node.n)
        elif node.type.is_float:
            return self.generate_constant_real(node.n)
        else:
            assert node.type.is_complex
            return self.generate_constant_complex(node.n)

    def check_unbound_local(self, node, var):
        if getattr(node, 'check_unbound', None):
            # Path the LLVMValueRefNode, we don't want a Name since it would
            # check for unbound variables recursively
            int_type = Py_uintptr_t.to_llvm(self.context)
            value_p = self.builder.ptrtoint(var.lvalue, int_type)
            node.loaded_name.llvm_value = value_p
            self.visit(node.check_unbound)

    def visit_Name(self, node):
        var = node.variable
        if (var.lvalue is None and not var.renameable and
                self.symtab[node.id].is_cellvar):
            var = self.symtab[node.id]

        assert var.lvalue, var

        self.check_unbound_local(node, var)

        should_load = (not var.renameable or
                       var.type.is_struct) and not var.is_constant
        if should_load and isinstance(node.ctx, ast.Load):
            # Not a renamed but an alloca'd variable
            return self.load_tbaa(var.lvalue, var.type)
        else:
            return var.lvalue

    def _handle_ctx(self, node, lptr, tbaa_type, name=''):
        if isinstance(node.ctx, ast.Load):
            return self.load_tbaa(lptr, tbaa_type,
                                  name=name and 'load_' + name)
        else:
            return lptr

    def _generate_test(self, llval):
        return self.builder.icmp(lc.ICMP_NE, llval,
                                 lc.Constant.null(llval.type))

    def visit_BoolOp(self, node):
        # NOTE: Can have >2 values
        assert len(node.values) >= 2
        assert isinstance(node.op, ast.And) or isinstance(node.op, ast.Or)

        count = len(node.values)

        if isinstance(node.op, ast.And):
            bb_true = self.append_basic_block('and.true')
            bb_false = self.append_basic_block('and.false')
            bb_next = [self.append_basic_block('and.rhs')
                       for i in range(count - 1)] + [bb_true]
            bb_done = self.append_basic_block('and.done')

            for i in range(count):
                value = self.visit(node.values[i])
                if value.type != _int1:
                    value = self._generate_test(value)
                self.builder.cbranch(value, bb_next[i], bb_false)
                self.builder.position_at_end(bb_next[i])

            assert self.builder.basic_block is bb_true
            self.builder.branch(bb_done)

            self.builder.position_at_end(bb_false)
            self.builder.branch(bb_done)

            self.builder.position_at_end(bb_done)
        elif isinstance(node.op, ast.Or):
            bb_true = self.append_basic_block('or.true')
            bb_false = self.append_basic_block('or.false')
            bb_next = [self.append_basic_block('or.rhs')
                       for i in range(count - 1)] + [bb_false]
            bb_done = self.append_basic_block('or.done')

            for i in range(count):
                value = self.visit(node.values[i])
                if value.type != _int1:
                    value = self._generate_test(value)
                self.builder.cbranch(value, bb_true, bb_next[i])
                self.builder.position_at_end(bb_next[i])

            assert self.builder.basic_block is bb_false
            self.builder.branch(bb_done)

            self.builder.position_at_end(bb_true)
            self.builder.branch(bb_done)

            self.builder.position_at_end(bb_done)
        else:
            raise Exception("internal erorr")

        booltype = _int1
        phi = self.builder.phi(booltype)
        phi.add_incoming(lc.Constant.int(booltype, 1), bb_true)
        phi.add_incoming(lc.Constant.int(booltype, 0), bb_false)

        return phi

    def visit_UnaryOp(self, node):
        operand_type = node.operand.type
        operand = self.visit(node.operand)
        operand_ltype = operand.type
        op = node.op
        if isinstance(op, ast.Not) and (operand_type.is_bool or
                                        operand_type.is_int_like):
            bb_false = self.builder.basic_block
            bb_true = self.append_basic_block('not.true')
            bb_done = self.append_basic_block('not.done')
            self.builder.cbranch(
                self.builder.icmp(lc.ICMP_NE, operand,
                                  lc.Constant.null(operand_ltype)),
                bb_true, bb_done)
            self.builder.position_at_end(bb_true)
            self.builder.branch(bb_done)
            self.builder.position_at_end(bb_done)
            phi = self.builder.phi(operand_ltype)
            phi.add_incoming(lc.Constant.int(operand_ltype, 1), bb_false)
            phi.add_incoming(lc.Constant.int(operand_ltype, 0), bb_true)
            return phi
        elif isinstance(op, ast.USub) and operand_type.is_numeric:
            if operand_type.is_float:
                return self.builder.fsub(lc.Constant.null(operand_ltype),
                                         operand)
            elif operand_type.is_int_like and operand_type.signed:
                return self.builder.sub(lc.Constant.null(operand_ltype),
                                        operand)
        elif isinstance(op, ast.UAdd) and operand_type.is_numeric:
            return operand
        elif isinstance(op, ast.Invert) and operand_type.is_int_like:
            return self.builder.xor(lc.Constant.int(operand_ltype, -1), operand)
        raise error.NumbaError(node, "Unary operator %s" % node.op)

    def visit_Compare(self, node):
        op = node.ops[0]

        lhs = node.left
        rhs = node.comparators[0]

        lhs_lvalue = self.visit(lhs)
        rhs_lvalue = self.visit(rhs)

        op_map = {
            ast.Gt    : '>',
            ast.Lt    : '<',
            ast.GtE   : '>=',
            ast.LtE   : '<=',
            ast.Eq    : '==',
            ast.NotEq : '!=',
        }

        op = op_map[type(op)]

        if lhs.type.is_float and rhs.type.is_float:
            lfunc = self.builder.fcmp
            lop = _compare_mapping_float[op]
        elif lhs.type.is_int_like and rhs.type.is_int_like:
            lfunc = self.builder.icmp
            if lhs.type.signed:
                mapping = _compare_mapping_sint
            else:
                mapping = _compare_mapping_uint
            lop = mapping[op]
        else:
            raise error.NumbaError(node, lhs.type)

        return lfunc(lop, lhs_lvalue, rhs_lvalue)

    def _init_phis(self, node):
        "Set basic block and initialize LLVM phis"
        for phi_node in node.phi_nodes:
            ltype = phi_node.variable.type.to_llvm(self.context)
            phi = self.builder.phi(ltype, phi_node.variable.unmangled_name)
            phi_node.variable.lvalue = phi

            if phi_node.variable.type.is_array:
                nodes.make_preload_phi(self.context, self.builder, phi_node)

    def setblock(self, cfg_basic_block):
        if cfg_basic_block.is_fabricated:
            return

        old = self.flow_block
        if old and not old.exit_block:
            if old.id == 1:
                # Handle promotions from the entry block. This is somewhat
                # of a hack, and needed since the CFG isn't properly merged
                # in the AST
                self.visitlist(old.body)
            old.exit_block = self.builder.basic_block

        self.flow_block = cfg_basic_block

    def visit_PromotionNode(self, node):
        lvalue = self.visit(node.node)
        node.variable.lvalue = lvalue
        # Update basic block in case the promotion created a new block
        self.flow_block.exit_block = self.builder.basic_block

    def visit_ControlBlock(self, node):
        """
        Return a new basic block and handle phis and promotions. Promotions
        are needed at merge (phi) points to have a consistent type.
        """
        #
        ### Create entry basic block
        #
        if node is None:
            # Fabricated If statement
            label = 'fabricated_basic_block'
        else:
            label = node.label

        self.setblock(node)
        node.prev_block = self.builder.basic_block
        node.entry_block = node.create_block(self, label)
        if node.branch_here and not self.is_block_terminated():
            self.builder.branch(node.entry_block)

        self.builder.position_at_end(node.entry_block)
        self._init_phis(node)

        if len(node.body) == 1:
            lbody = self.visit(node.body[0])
        else:
            self.visitlist(node.body)
            lbody = None

        if not node.exit_block:
            node.exit_block = self.builder.basic_block

        return lbody

    def append_basic_block(self, name='unamed'):
        idx = len(self.blocks)
        #bb = self.lfunc.append_basic_block('%s_%d'%(name, idx))
        bb = self.lfunc.append_basic_block(name)
        self.blocks[idx] = bb
        return bb

    def visit_LowLevelBasicBlockNode(self, node):
        llvm_block = node.create_block(self)
        if not self.is_block_terminated():
            self.builder.branch(llvm_block)
        self.builder.position_at_end(llvm_block)
        return self.visit(node.body)

    def visit_If(self, node, is_while=False):
        if not hasattr(node, 'cond_block'):
            # We have a synthetic 'if' without a cfg, fabricate fake blocks
            node = nodes.build_if(**vars(node))

        # Visit condition
        test = self.visit(node.test)

        bb_cond = node.cond_block.entry_block
        # test = self.visit(node.test)
        if test.type != _int1:
            test = self._generate_test(test)

        # Create exit block
        self.visit_ControlBlock(node.exit_block)
        bb_endif = node.exit_block.entry_block
        if is_while:
            self.setup_loop(node.continue_block, bb_cond, bb_endif)

        # Visit if clauses
        self.visitlist(node.body)
        #if self.have_cfg:
        #    self.flow_block.exit_block = self.builder.basic_block

        bb_true = node.if_block.entry_block
        if is_while:
            if not self.is_block_terminated():
                self.builder.branch(bb_cond)
            self.teardown_loop()
        else:
            self.term_block(bb_endif)

        if node.orelse:
            self.visitlist(node.orelse)
            bb_false = node.else_block.entry_block
            self.term_block(bb_endif)
        else:
            bb_false = bb_endif

        # Mark current basic block and the exit block of the body
        self.setblock(node.exit_block)

        # Branch to block from condition
        self.builder.position_at_end(node.cond_block.prev_block)
        self.builder.branch(bb_cond)

        self.builder.position_at_end(node.cond_block.exit_block)
        # assert not self.is_block_terminated()
        self.builder.cbranch(test, bb_true, bb_false)
        self.builder.position_at_end(node.exit_block.exit_block)

        # Swallow statements following the branch
        node.exit_block.exit_block = None

    def visit_IfExp(self, node):
        test = self.visit(node.test)
        if test.type != _int1:
            test = self._generate_test(test)

        then_block = self.append_basic_block('ifexp.then')
        else_block = self.append_basic_block('ifexp.else')
        merge_block = self.append_basic_block('ifexp.merge')

        self.builder.cbranch(test, then_block, else_block)

        self.builder.position_at_end(then_block)
        then_value = self.visit(node.body)
        self.builder.branch(merge_block)

        self.builder.position_at_end(else_block)
        else_value = self.visit(node.orelse)
        self.builder.branch(merge_block)

        self.builder.position_at_end(merge_block)
        phi = self.builder.phi(then_value.type)
        phi.add_incoming(then_value, then_block)
        phi.add_incoming(else_value, else_block)
        return phi

    def visit_While(self, node):
        self.visit_If(node, is_while=True)

    def visit_Continue(self, node):
        assert self.loop_beginnings # Python syntax should ensure this
        self.builder.branch(self.loop_beginnings[-1])

    def visit_Break(self, node):
        assert self.loop_exits # Python syntax should ensure this
        self.builder.branch(self.loop_exits[-1])

    def term_block(self, end_block):
        if not self.is_block_terminated():
            self.terminate_block(self.builder.basic_block, end_block)

    def append_basic_block(self, name='unamed'):
        idx = len(self.blocks)
        #bb = self.lfunc.append_basic_block('%s_%d'%(name, idx))
        bb = self.lfunc.append_basic_block(name)
        self.blocks[idx] = bb
        return bb

    @property
    def cur_bb(self):
        return self.builder.basic_block

    def is_block_terminated(self, basic_block=None):
        '''
        Check if the current basicblock is properly terminated.
        That means the basicblock is ended with a branch or return
        '''
        basic_block = basic_block or self.cur_bb
        instructions = basic_block.instructions
        return instructions and instructions[-1].is_terminator

    def terminate_block(self, block, end_block):
        if not self.is_block_terminated(block):
            bb = self.cur_bb
            self.builder.position_at_end(block)
            self.builder.branch(end_block)
            self.builder.position_at_end(bb)

    def setup_loop(self, continue_block, bb_cond, bb_exit):
        if continue_block:
            # Jump to target index increment block instead of while condition
            # block for 'for i in range(...):' loops
            bb_cond = continue_block.create_block(self)

        self.loop_beginnings.append(bb_cond)
        self.loop_exits.append(bb_exit)
        self.in_loop += 1

    def teardown_loop(self):
        self.loop_beginnings.pop()
        self.loop_exits.pop()
        self.in_loop -= 1

    def visit_For(self, node):
        raise error.NumbaError(node, "This node should have been replaced")

    def visit_Return(self, node):
        if node.value is not None:
            value_type = node.value.type
            rettype = self.func_signature.return_type

            retval = self.visit(node.value)
            if is_obj(rettype) or rettype.is_pointer:
                retval = self.builder.bitcast(retval,
                                              self.return_value.type.pointee)

            if not retval.type == self.return_value.type.pointee:
                dump(node)
                logger.debug('%s != %s (in node %s)' % (
                        self.return_value.type.pointee, retval.type,
                        utils.pformat_ast(node)))
                raise error.NumbaError(
                    node, 'Expected %s type in return, got %s!' %
                    (self.return_value.type.pointee, retval.type))

            self.builder.store(retval, self.return_value)

            ret_type = self.func_signature.return_type
            if is_obj(rettype):
                self.xincref_temp(self.return_value)

        if not self.is_block_terminated():
            self.builder.branch(self.cleanup_label)

        # if node.value is not None:
        #     self.builder.ret(self.visit(node.value))
        # else:
        #     self.builder.ret_void()

    def visit_Suite(self, node):
        self.visitlist(node.body)
        return None

    def generate_constant_int(self, val, ty=typesystem.int_):
        lconstant = lc.Constant.int(ty.to_llvm(self.context), val)
        return lconstant

    _binops = {
        ast.Add:    ('fadd', ('add', 'add')),
        ast.Sub:    ('fsub', ('sub', 'sub')),
        ast.Mult:   ('fmul', ('mul', 'mul')),
        ast.Div:    ('fdiv', ('udiv', 'sdiv')),
        ast.BitAnd: ('and_', ('and_', 'and_')),
        ast.BitOr:  ('or_',  ('or_', 'or_')),
        ast.BitXor: ('xor',  ('xor', 'xor')),
        ast.LShift: ('shl',  ('shl',  'shl')),   # shift left
        ast.RShift: ('ashr', ('ashr',  'ashr')), # arithmetic shift right
    }

    _opnames = {
        ast.Mult: 'mul',
    }

    def opname(self, op):
        if op in self._opnames:
            return self._opnames[op]
        else:
            return op.__name__.lower()

    def _handle_mod(self, node, lhs, rhs):
        _, func = self.context.intrinsic_library.declare(self.llvm_module,
                                                        'PyModulo',
                                                        arg_types = (node.type,
                                                                     node.type),
                                                        return_type = node.type)
        return self.builder.call(func, (lhs, rhs))

    def _handle_complex_binop(self, lhs, op, rhs):
        opname = self.opname(op)
        if opname in ('add', 'sub', 'mul', 'div', 'floordiv'):
            m = getattr(self, '_complex_' + opname)
            result = self._generate_complex_op(m, lhs, rhs)
        else:
            raise error.NumbaError("Unsupported binary operation "
                                   "for complex numbers: %s" % opname)
        return result

    def _handle_numeric_binop(self, lhs, node, op, rhs):
        llvm_method_name = self._binops[op][node.type.is_int]
        if node.type.is_int:
            llvm_method_name = llvm_method_name[node.type.signed]

        meth = getattr(self.builder, llvm_method_name)
        if not lhs.type == rhs.type:
            print lhs.type, rhs.type
            assert False, ast.dump(node)

        result = meth(lhs, rhs)
        return result

    def visit_BinOp(self, node):
        lhs = self.visit(node.left)
        rhs = self.visit(node.right)
        op = type(node.op)

        pointer_type = self.have(node.left.type, node.right.type,
                                 "is_pointer", "is_int")

        if (node.type.is_int or node.type.is_float) and op in self._binops:
            result = self._handle_numeric_binop(lhs, node, op, rhs)
        elif (node.type.is_int or node.type.is_float) and op == ast.Mod:
            return self._handle_mod(node, lhs, rhs)
        elif node.type.is_complex:
            result = self._handle_complex_binop(lhs, op, rhs)
        elif pointer_type:
            if not node.left.type.is_pointer:
                lhs, rhs = rhs, lhs
            result = self.builder.gep(lhs, [rhs])
        else:
            logger.debug('Unrecognized node type "%s"' % node.type)
            logger.debug(ast.dump(node))
            raise error.NumbaError(
                    node, "Binary operations %s on values typed %s and %s "
                          "not (yet) supported" % (self.opname(op),
                                                   node.left.type,
                                                   node.right.type))

        return result

    def visit_CoercionNode(self, node, val=None):
        if val is None:
            val = self.visit(node.node)

        if node.type == node.node.type:
            return val

        # logger.debug('Coerce %s --> %s', node.node.type, node.dst_type)
        node_type = node.node.type
        dst_type = node.dst_type
        ldst_type = dst_type.to_llvm(self.context)
        if node_type.is_pointer and dst_type.is_int:
            val = self.builder.ptrtoint(val, ldst_type)
        elif node_type.is_int and dst_type.is_pointer:
            val = self.builder.inttoptr(val, ldst_type)
        elif (dst_type.is_pointer or
              dst_type.is_reference) and node_type.is_pointer:
            val = self.builder.bitcast(val, ldst_type)
        elif dst_type.is_complex and node_type.is_complex:
            val = self._promote_complex(node_type, dst_type, val)
        elif dst_type.is_complex and node_type.is_numeric:
            ldst_base_type = dst_type.base_type.to_llvm(self.context)
            real = val
            if node_type != dst_type.base_type:
                real = self.caster.cast(real, ldst_base_type)
            imag = llvm.core.Constant.real(ldst_base_type, 0.0)
            val = self._create_complex(real, imag)
        else:
            val = self.caster.cast(val, node.dst_type.to_llvm(self.context))

        if debug_conversion:
            self.puts("Coercing %s to %s" % (node_type, dst_type))

        return val

    def visit_CoerceToObject(self, node):
        from_type = node.node.type
        result = self.visit(node.node)
        if not is_obj(from_type):
            result = self.object_coercer.convert_single(from_type, result,
                                                        name=node.name)
        return result

    def visit_CoerceToNative(self, node):
        assert node.node.type.is_tuple
        val = self.visit(node.node)
        return self.object_coercer.to_native(node.dst_type, val,
                                             name=node.name)

    def visit_DereferenceNode(self, node):
        result = self.visit(node.pointer)
        return self.load_tbaa(result, node.type.pointer())

    def visit_PointerFromObject(self, node):
        return self.visit(node.node)

    def visit_ComplexNode(self, node):
        real = self.visit(node.real)
        imag = self.visit(node.imag)
        return self._create_complex(real, imag)

    def visit_Subscript(self, node):
        value_type = node.value.type
        if not (value_type.is_carray or value_type.is_c_string or
                    value_type.is_pointer):
            raise error.InternalError(node, "Unsupported type:", node.value.type)

        value = self.visit(node.value)
        index = self.visit(node.slice)
        indices = [index]

        lptr = self.builder.gep(value, indices)
        if node.slice.type.is_int:
            lptr = self._handle_ctx(node, lptr, node.value.type)

        return lptr

    def visit_DataPointerNode(self, node):
        assert node.node.type.is_array
        lvalue = self.visit(node.node)
        lindices = self.visit(node.slice)
        lptr = node.subscript(self, self.tbaa, lvalue, lindices)
        return self._handle_ctx(node, lptr, node.type.pointer())

    #def visit_Index(self, node):
    #    return self.visit(node.value)

    def visit_ExtSlice(self, node):
        return self.visitlist(node.dims)

    def visit_Call(self, node):
        raise error.InternalError(node, "This node should have been replaced")

    def visit_List(self, node):
        types = [n.type for n in node.elts]
        largs = self.visitlist(node.elts)
        return self.object_coercer.build_list(types, largs)

    def visit_Tuple(self, node):
        raise error.InternalError(node, "This node should have been replaced")

    def visit_Dict(self, node):
        key_types = [k.type for k in node.keys]
        value_types = [v.type for v in node.values]
        llvm_keys = self.visitlist(node.keys)
        llvm_values = self.visitlist(node.values)
        result = self.object_coercer.build_dict(key_types, value_types,
                                                llvm_keys, llvm_values)
        return result

    def visit_ObjectInjectNode(self, node):
        # FIXME: Currently uses the runtime address of the python function.
        #        Sounds like a hack.
        self.keep_alive(node.object)
        addr = id(node.object)
        obj_addr_int = self.generate_constant_int(addr, typesystem.Py_ssize_t)
        obj = self.builder.inttoptr(obj_addr_int,
                                    typesystem.object_.to_llvm(self.context))
        return obj

    def visit_ObjectCallNode(self, node):
        args_tuple = self.visit(node.args_tuple)
        kwargs_dict = self.visit(node.kwargs_dict)

        if node.function is None:
            node.function = nodes.ObjectInjectNode(node.py_func)
        lfunc_addr = self.visit(node.function)

        # call PyObject_Call
        largs = [lfunc_addr, args_tuple, kwargs_dict]
        _, pyobject_call = self.context.external_library.declare(
                                        self.llvm_module, 'PyObject_Call')

        res = self.builder.call(pyobject_call, largs, name=node.name)
        return self.caster.cast(res, node.variable.type.to_llvm(self.context))

    def visit_NoneNode(self, node):
        try:
            self.llvm_module.add_global_variable(object_.to_llvm(self.context),
                                         "Py_None")
        except llvm.LLVMException:
            pass

        return self.llvm_module.get_global_variable_named("Py_None")

    def visit_NativeCallNode(self, node, largs=None):
        if largs is None:
            largs = self.visitlist(node.args)

        return_value = llvm_codegen.handle_struct_passing(
                            self.builder, self.alloca, largs, node.signature)

        if hasattr(node.llvm_func, 'module') and node.llvm_func.module != self.llvm_module:
            lfunc = self.llvm_module.get_or_insert_function(node.llvm_func.type.pointee,
                                                    node.llvm_func.name)
        else:
            lfunc = node.llvm_func


        if node.signature.return_type.is_void or return_value is not None:
            kwargs = {}
        else:
            kwargs = dict(name=node.name + '_result')

        result = self.builder.call(lfunc, largs, **kwargs)

        if node.signature.struct_by_reference:
            if minitypes.pass_by_ref(node.signature.return_type):
                # TODO: TBAA
                result = self.builder.load(return_value)

        return result

    def visit_NativeFunctionCallNode(self, node):
        lfunc = self.visit(node.function)
        node.llvm_func = lfunc
        return self.visit_NativeCallNode(node)

    def visit_LLMacroNode (self, node):
        return node.macro(self.context, self.builder,
                          *self.visitlist(node.args))

    def visit_LLVMExternalFunctionNode(self, node):
        lfunc_type = node.signature.to_llvm(self.context)
        return self.llvm_module.get_or_insert_function(lfunc_type, node.fname)

    def visit_LLVMIntrinsicNode(self, node):
        intr = getattr(llvm.core, 'INTR_' + node.func_name)
        largs = self.visitlist(node.args)
        if largs:
            ltypes = [largs[0].type]
        else:
            ltypes = []
        node.llvm_func = llvm.core.Function.intrinsic(self.llvm_module,
                                                      intr,
                                                      ltypes)
        return self.visit_NativeCallNode(node, largs=largs)

    def visit_MathCallNode(self, node):
        lfunc_type = node.signature.to_llvm(self.context)
        lfunc = self.llvm_module.get_or_insert_function(lfunc_type, node.name)

        node.llvm_func = lfunc
        return self.visit_NativeCallNode(node)

    def visit_CTypesCallNode(self, node):
        node.llvm_func = self.visit(node.function)
        return self.visit_NativeCallNode(node)

    def visit_ClosureCallNode(self, node):
        lfunc = node.closure_type.closure.lfunc
        assert lfunc is not None
        assert len(node.args) == node.expected_nargs + node.need_closure_scope
        self.visit(node.func)
        node.llvm_func = lfunc
        return self.visit_NativeCallNode(node)

    def visit_ComplexConjugateNode(self, node):
        lcomplex = self.visit(node.complex_node)

        elem_ltyp = node.type.base_type.to_llvm(self.context)
        zero = llvm.core.Constant.real(elem_ltyp, 0)
        imag = self.builder.extract_value(lcomplex, 1)
        new_imag_lval = self.builder.fsub(zero, imag)

        assert hasattr(self.builder, 'insert_value'), (
            "llvm-py support for LLVMBuildInsertValue() required to build "
            "code for complex conjugates.")

        return self.builder.insert_value(lcomplex, new_imag_lval, 1)

    def visit_MultiArrayAPINode(self, node):
        meth = getattr(self.multiarray_api, 'load_' + node.func_name)
        lfunc = meth(self.llvm_module, self.builder)
        lsignature = node.signature.pointer().to_llvm(self.context)
        node.llvm_func = self.builder.bitcast(lfunc, lsignature)
        result = self.visit_NativeCallNode(node)
        return result

    def alloca(self, type, name='', change_bb=True):
        return self.llvm_alloca(self.to_llvm(type), name, change_bb)

    def llvm_alloca(self, ltype, name='', change_bb=True):
        return llvm_alloca(self.lfunc, self.builder, ltype, name, change_bb)

    def visit_TempNode(self, node):
        if node.llvm_temp is None:
            kwds = {}
            if node.name:
                kwds['name'] = node.name
            value = self.alloca(node.type, **kwds)
            node.llvm_temp = value

        return node.llvm_temp

    def visit_TempLoadNode(self, node):
        # TODO: use unique type for each temporary load and store pair
        temp = self.visit(node.temp)
        instr = self.builder.load(temp, invariant=node.invariant)
        self.tbaa.set_metadata(instr, node.temp.get_tbaa_node(self.tbaa))
        return instr

    def visit_TempStoreNode(self, node):
        return self.visit(node.temp)

    def visit_ObjectTempNode(self, node):
        if isinstance(node.node, nodes.ObjectTempNode):
            return self.visit(node.node)

        bb = self.builder.basic_block
        # Initialize temp to NULL at beginning of function
        self.builder.position_at_beginning(self.lfunc.get_entry_basic_block())
        name = getattr(node.node, 'name', 'object') + '_temp'
        lhs = self._null_obj_temp(name)
        node.llvm_temp = lhs

        # Assign value
        self.builder.position_at_end(bb)
        rhs = self.visit(node.node)
        self.generate_assign_stack(rhs, lhs, tbaa_type=object_,
                                   decref=self.in_loop)

        # goto error if NULL
        # self.puts("checking error... %s" % error.format_pos(node))
        self.object_coercer.check_err(rhs, pos_node=node.node)
        # self.puts("all good at %s" % error.format_pos(node))

        if node.incref:
            self.incref(self.load_tbaa(lhs, object_))

        # Generate Py_XDECREF(temp) at end-of-function cleanup path
        self.xdecref_temp_cleanup(lhs)
        result = self.load_tbaa(lhs, object_, name=name + '_load')

        if not node.type == object_:
            dst_type = node.type.to_llvm(self.context)
            result = self.builder.bitcast(result, dst_type)

        return result

    def visit_PropagateNode(self, node):
        # self.puts("ERROR! %s" % error.format_pos(node))
        self.builder.branch(self.error_label)

    def visit_ObjectTempRefNode(self, node):
        return node.obj_temp_node.llvm_temp

    def visit_LLVMValueRefNode(self, node):
        assert node.llvm_value
        return node.llvm_value

    def visit_BadValue(self, node):
        ltype = node.type.to_llvm(self.context)
        node.llvm_value = llvm.core.Constant.undef(ltype)
        return node.llvm_value

    def visit_CloneNode(self, node):
        return node.llvm_value

    def visit_CloneableNode(self, node):
        llvm_value = self.visit(node.node)
        for clone_node in node.clone_nodes:
            clone_node.llvm_value = llvm_value

        return llvm_value

    def pyarray_accessor(self, llvm_array_ptr, dtype):
        acc = ndarray_helpers.PyArrayAccessor(self.builder, llvm_array_ptr,
                                              self.tbaa, dtype)
        return acc

    def visit_ArrayAttributeNode(self, node):
        array = self.visit(node.array)
        acc = self.pyarray_accessor(array, node.array_type.dtype)

        attr_name = node.attr_name
        if attr_name == 'shape':
            attr_name = 'dimensions'

        result = getattr(acc, attr_name)
        ltype = node.type.to_llvm(self.context)
        if node.attr_name == 'data':
            result = self.builder.bitcast(result, ltype)

        return result

    def visit_ShapeAttributeNode(self, node):
        # hurgh, no dispatch on superclasses?
        return self.visit_ArrayAttributeNode(node)

    def visit_IncrefNode(self, node):
        obj = self.visit(node.value)
        self.incref(obj)
        return obj

    def visit_DecrefNode(self, node):
        obj = self.visit(node.value)
        self.decref(obj)
        return obj

    def visit_ExpressionNode(self, node):
        self.visitlist(node.stmts)
        return self.visit(node.expr)

    #------------------------------------------------------------------------
    # User nodes
    #------------------------------------------------------------------------

    def visit_UserNode(self, node):
        return node.codegen(self)
