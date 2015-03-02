from __future__ import print_function, absolute_import

from llvmlite import ir, binding as ll

from numba import types
from numba import unittest_support as unittest
from numba import datamodel
from numba.datamodel.testing import test_factory


class TestBool(test_factory()):
    fe_type = types.boolean


class TestPyObject(test_factory()):
    fe_type = types.pyobject


class TestInt8(test_factory()):
    fe_type = types.int8


class TestInt16(test_factory()):
    fe_type = types.int16


class TestInt32(test_factory()):
    fe_type = types.int32


class TestInt64(test_factory()):
    fe_type = types.int64


class TestUInt8(test_factory()):
    fe_type = types.uint8


class TestUInt16(test_factory()):
    fe_type = types.uint16


class TestUInt32(test_factory()):
    fe_type = types.uint32


class TestUInt64(test_factory()):
    fe_type = types.uint64


class TestFloat(test_factory()):
    fe_type = types.float32


class TestDouble(test_factory()):
    fe_type = types.float64


class TestComplex(test_factory()):
    fe_type = types.complex64


class TestDoubleComplex(test_factory()):
    fe_type = types.complex128


class TestPointerOfInt32(test_factory()):
    fe_type = types.CPointer(types.int32)


class TestUniTupleOf2xInt32(test_factory()):
    fe_type = types.UniTuple(types.int32, 2)


class TestUniTupleEmpty(test_factory()):
    fe_type = types.UniTuple(types.int32, 0)


class TestTupleInt32Float32(test_factory()):
    fe_type = types.Tuple([types.int32, types.float32])


class TestTupleEmpty(test_factory()):
    fe_type = types.Tuple([])


class Test1DArrayOfInt32(test_factory(support_as_data=False)):
    fe_type = types.Array(types.int32, 1, 'C')


class Test2DArrayOfComplex128(test_factory(support_as_data=False)):
    fe_type = types.Array(types.complex128, 2, 'C')


class Test0DArrayOfInt32(test_factory(support_as_data=False)):
    fe_type = types.Array(types.int32, 0, 'C')


class TestArgInfo(unittest.TestCase):
    def _test_as_arguments(self, fe_args):
        dmm = datamodel.default_manager
        fi = datamodel.ArgPacker(dmm, fe_args)

        module = ir.Module()
        fnty = ir.FunctionType(ir.VoidType(), [])
        function = ir.Function(module, fnty, name="test_arguments")
        builder = ir.IRBuilder()
        builder.position_at_end(function.append_basic_block())

        args = [ir.Constant(dmm.lookup(t).get_value_type(), None)
                for t in fe_args]

        values = fi.as_arguments(builder, args)
        asargs = fi.from_arguments(builder, values)

        self.assertEqual(len(asargs), len(fe_args))
        valtys = tuple([v.type for v in values])
        self.assertEqual(valtys, fi.argument_types)

        expect_types = [a.type for a in args]
        got_types = [a.type for a in asargs]

        self.assertEqual(expect_types, got_types)

        builder.ret_void()

        ll.parse_assembly(str(module))

    def test_int32_array_complex(self):
        fe_args = [types.int32,
                   types.Array(types.int32, 1, 'C'),
                   types.complex64]
        self._test_as_arguments(fe_args)

    def test_two_arrays(self):
        fe_args = [types.Array(types.int32, 1, 'C')] * 2
        self._test_as_arguments(fe_args)


if __name__ == '__main__':
    unittest.main()
