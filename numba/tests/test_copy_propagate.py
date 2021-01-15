#
# Copyright (c) 2017 Intel Corporation
# SPDX-License-Identifier: BSD-2-Clause
#

from numba.core import types, typing, ir, config, compiler, cpu
from numba.core.registry import cpu_target
from numba.core.annotations import type_annotations
from numba.core.ir_utils import (copy_propagate, apply_copy_propagate,
                            get_name_var_table)
from numba.core.typed_passes import type_inference_stage
import unittest

def test_will_propagate(b, z, w):
    x = 3
    x1 = x
    if b > 0:
        y = z + w
    else:
        y = 0
    a = 2 * x1
    return a < b

def test_wont_propagate(b, z, w):
    x = 3
    if b > 0:
        y = z + w
        x = 1
    else:
        y = 0
    a = 2 * x
    return a < b

def null_func(a,b,c,d):
    False

def inListVar(list_var, var):
    for i in list_var:
        if i.name == var:
            return True
    return False

def findAssign(func_ir, var):
    for label, block in func_ir.blocks.items():
        for i, inst in enumerate(block.body):
            if isinstance(inst, ir.Assign) and inst.target.name!=var:
                all_var = inst.list_vars()
                if inListVar(all_var, var):
                    return True

    return False

class TestCopyPropagate(unittest.TestCase):
    def test1(self):
        typingctx = typing.Context()
        targetctx = cpu.CPUContext(typingctx)
        test_ir = compiler.run_frontend(test_will_propagate)
        with cpu_target.nested_context(typingctx, targetctx):
            typingctx.refresh()
            targetctx.refresh()
            args = (types.int64, types.int64, types.int64)
            typemap, return_type, calltypes, _ = type_inference_stage(typingctx, test_ir, args, None)
            type_annotation = type_annotations.TypeAnnotation(
                func_ir=test_ir,
                typemap=typemap,
                calltypes=calltypes,
                lifted=(),
                lifted_from=None,
                args=args,
                return_type=return_type,
                html_output=config.HTML)
            in_cps, out_cps = copy_propagate(test_ir.blocks, typemap)
            apply_copy_propagate(test_ir.blocks, in_cps, get_name_var_table(test_ir.blocks), typemap, calltypes)

            self.assertFalse(findAssign(test_ir, "x1"))

    def test2(self):
        typingctx = typing.Context()
        targetctx = cpu.CPUContext(typingctx)
        test_ir = compiler.run_frontend(test_wont_propagate)
        with cpu_target.nested_context(typingctx, targetctx):
            typingctx.refresh()
            targetctx.refresh()
            args = (types.int64, types.int64, types.int64)
            typemap, return_type, calltypes, _ = type_inference_stage(typingctx, test_ir, args, None)
            type_annotation = type_annotations.TypeAnnotation(
                func_ir=test_ir,
                typemap=typemap,
                calltypes=calltypes,
                lifted=(),
                lifted_from=None,
                args=args,
                return_type=return_type,
                html_output=config.HTML)
            in_cps, out_cps = copy_propagate(test_ir.blocks, typemap)
            apply_copy_propagate(test_ir.blocks, in_cps, get_name_var_table(test_ir.blocks), typemap, calltypes)

            self.assertTrue(findAssign(test_ir, "x"))


if __name__ == "__main__":
    unittest.main()
