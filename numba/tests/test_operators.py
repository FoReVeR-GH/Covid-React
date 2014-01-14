from __future__ import print_function
import unittest
import numpy as np
from numba.compiler import compile_isolated, Flags
from numba import types, utils
from numba.tests import usecases
import itertools

Noflags = Flags()

enable_pyobj_flags = Flags()
enable_pyobj_flags.set("enable_pyobject")

force_pyobj_flags = Flags()
force_pyobj_flags.set("force_pyobject")


def add_usecase(x, y):
    return x + y

def sub_usecase(x, y):
    return x - y

def mul_usecase(x, y):
    return x * y

def div_usecase(x, y):
    return x / y

def mod_usecase(x, y):
    return x % y

def pow_usecase(x, y):
    return x ** y


class TestOperators(unittest.TestCase):

    def run_test_ints(self, pyfunc, x_operands, y_operands, types_list,
                      flags=Noflags):
        for arg_types in types_list:
            if types.pyobject in arg_types:
                flags = enable_pyobj_flags

            cr = compile_isolated(pyfunc, arg_types, flags=flags)
            cfunc = cr.entry_point

            for x, y in itertools.product(x_operands, y_operands):
                self.assertTrue(np.all(pyfunc(x, y) == cfunc(x, y)))

    def run_test_floats(self, pyfunc, x_operands, y_operands, types_list,
                        flags=Noflags):
        for arg_types in types_list:
            if types.pyobject in arg_types:
                flags = enable_pyobj_flags

            cr = compile_isolated(pyfunc, arg_types, flags=flags)
            cfunc = cr.entry_point

            for x, y in itertools.product(x_operands, y_operands):
                self.assertTrue(np.allclose(pyfunc(x, y), cfunc(x, y)))

    @unittest.SkipTest
    def test_add_ints(self):

        pyfunc = add_usecase

        x_operands = [-1, 0, 1]
        y_operands = [-1, 0, 1]

        types_list = [(types.int32, types.int32),
                      (types.int64, types.int64),
                      (types.pyobject, types.pyobject)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list)

        x_operands = [0, 1]
        y_operands = [0, 1]

        types_list = [(types.byte, types.byte),
                      (types.uint32, types.uint32),
                      (types.uint64, types.uint64)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list)

        array = np.arange(-10, 10, dtype=np.int32)

        x_operands = [array]
        y_operands = [array]

        arraytype = types.Array(types.int32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)

    @unittest.SkipTest
    def test_add_floats(self):

        pyfunc = add_usecase

        x_operands = [-1.1, 0.0, 1.1]
        y_operands = [-1.1, 0.0, 1.1]

        types_list = [(types.float32, types.float32),
                      (types.float64, types.float64)]

        self.run_test_floats(pyfunc, x_operands, y_operands, types_list)

        array = np.arange(-1, 1, 0.1, dtype=np.float32)

        x_operands = [array]
        y_operands = [array]

        arraytype = types.Array(types.float32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)

    def test_sub_ints(self):

        pyfunc = sub_usecase

        x_operands = [-1, 0, 1]
        y_operands = [-1, 0, 1]

        types_list = [(types.int32, types.int32),
                      (types.int64, types.int64),
                      (types.pyobject, types.pyobject)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list)

        # Unsigned version will overflow and wraparound
        x_operands = [1, 2]
        y_operands = [0, 1]

        types_list = [(types.byte, types.byte),
                      (types.uint32, types.uint32),
                      (types.uint64, types.uint64)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list)

        array = np.arange(-10, 10, dtype=np.int32)

        x_operands = [array]
        y_operands = [array]

        arraytype = types.Array(types.int32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)

    def test_sub_floats(self):

        pyfunc = sub_usecase

        x_operands = [-1.1, 0.0, 1.1]
        y_operands = [-1.1, 0.0, 1.1]

        types_list = [(types.float32, types.float32),
                      (types.float64, types.float64)]

        self.run_test_floats(pyfunc, x_operands, y_operands, types_list)

        array = np.arange(-1, 1, 0.1, dtype=np.float32)

        x_operands = [array]
        y_operands = [array]

        arraytype = types.Array(types.float32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)

    @unittest.SkipTest
    def test_mul_ints(self):

        pyfunc = mul_usecase

        x_operands = [-1, 0, 1]
        y_operands = [-1, 0, 1]

        types_list = [(types.int32, types.int32),
                      (types.int64, types.int64),
                      (types.pyobject, types.pyobject)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list)

        x_operands = [0, 1]
        y_operands = [0, 1]

        types_list = [(types.byte, types.byte),
                      (types.uint32, types.uint32),
                      (types.uint64, types.uint64)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list)

        array = np.arange(-10, 10, dtype=np.int32)

        x_operands = [array]
        y_operands = [array]

        arraytype = types.Array(types.int32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)

    @unittest.SkipTest
    def test_mul_floats(self):

        pyfunc = mul_usecase

        x_operands = [-111.111, 0.0, 111.111]
        y_operands = [-111.111, 0.0, 111.111]

        types_list = [(types.float32, types.float32),
                      (types.float64, types.float64)]

        self.run_test_floats(pyfunc, x_operands, y_operands, types_list)

        array = np.arange(-1, 1, 0.1, dtype=np.float32)

        x_operands = [array]
        y_operands = [array]

        arraytype = types.Array(types.float32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)

    @unittest.SkipTest
    def test_div_ints(self):

        pyfunc = div_usecase

        x_operands = [-1, 0, 1, 2, 3]
        y_operands = [-3, -2, -1, 1]

        types_list = [(types.int32, types.int32),
                      (types.int64, types.int64),
                      (types.pyobject, types.pyobject)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list)

        x_operands = [0, 1, 2, 3]
        y_operands = [1, 2, 3]

        types_list = [(types.byte, types.byte),
                      (types.uint32, types.uint32),
                      (types.uint64, types.uint64)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list)

        array = np.arange(-10, 10, dtype=np.int32)

        x_operands = [array]
        y_operands = [array]

        arraytype = types.Array(types.int32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)

    @unittest.SkipTest
    def test_div_floats(self):

        pyfunc = div_usecase

        x_operands = [-111.111, 0.0, 2.2]
        y_operands = [-2.2, 1.0, 111.111]

        types_list = [(types.float32, types.float32),
                      (types.float64, types.float64)]

        self.run_test_floats(pyfunc, x_operands, y_operands, types_list)

        array = np.arange(-1, 1, 0.1, dtype=np.float32)

        x_operands = [array]
        y_operands = [array]

        arraytype = types.Array(types.float32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)

    @unittest.SkipTest
    def test_mod_ints(self):

        pyfunc = mod_usecase

        x_operands = [-1, 0, 1, 2, 3]
        y_operands = [-3, -2, -1, 1]

        types_list = [(types.int32, types.int32),
                      (types.int64, types.int64),
                      (types.pyobject, types.pyobject)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list)

        x_operands = [0, 1, 2, 3]
        y_operands = [1, 2, 3]

        types_list = [(types.byte, types.byte),
                      (types.uint32, types.uint32),
                      (types.uint64, types.uint64)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list)

        array = np.arange(-10, 10, dtype=np.int32)

        x_operands = [array]
        y_operands = [array]

        arraytype = types.Array(types.int32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)

    @unittest.SkipTest
    def test_mod_floats(self):

        pyfunc = mod_usecase

        x_operands = [-111.111, 0.0, 2.2]
        y_operands = [-2.2, 1.0, 111.111]

        types_list = [(types.float32, types.float32),
                      (types.float64, types.float64)]

        self.run_test_floats(pyfunc, x_operands, y_operands, types_list)

        array = np.arange(-1, 1, 0.1, dtype=np.float32)

        x_operands = [array]
        y_operands = [array]

        arraytype = types.Array(types.float32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)

    def test_pow_ints(self):

        pyfunc = pow_usecase

        x_operands = [-2, -1, 0, 1, 2]
        y_operands = [0, 1, 2]

        types_list = [(types.int32, types.int32),
                      (types.int64, types.int64),
                      (types.pyobject, types.pyobject)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list)

        x_operands = [0, 1, 2]
        y_operands = [0, 1, 2]

        types_list = [(types.byte, types.byte),
                      (types.uint32, types.uint32),
                      (types.uint64, types.uint64)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list)

        array = np.arange(-10, 10, dtype=np.int32)

        x_operands = [array]
        y_operands = [array]

        arraytype = types.Array(types.int32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)

    @unittest.SkipTest
    def test_pow_floats(self):

        pyfunc = pow_usecase

        x_operands = [-222.222, -111.111, 111.111, 222.222]
        y_operands = [-2, -1, 0, 1, 2]

        types_list = [(types.float32, types.float32),
                      (types.float64, types.float64)]

        self.run_test_floats(pyfunc, x_operands, y_operands, types_list)

        x_operands = [0.0]
        y_operands = [0, 1, 2]  # TODO native handling of 0 ** negative power

        types_list = [(types.float32, types.float32),
                      (types.float64, types.float64)]

        self.run_test_floats(pyfunc, x_operands, y_operands, types_list)

        # NOTE
        # If x is finite negative and y is finite but not an integer,
        # it causes a domain error
        array = np.arange(0.1, 1, 0.1, dtype=np.float32)

        x_operands = [array]
        y_operands = [array]

        arraytype = types.Array(types.float32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)


        x_array = np.arange(-1, 0.1, 0.1, dtype=np.float32)
        y_array = np.arange(len(x_array), dtype=np.float32)

        x_operands = [x_array]
        y_operands = [y_array]

        arraytype = types.Array(types.float32, 1, 'C')
        types_list = [(arraytype, arraytype)]

        self.run_test_ints(pyfunc, x_operands, y_operands, types_list,
                           flags=enable_pyobj_flags)

    @unittest.SkipTest
    def test_add_complex(self):
        pyfunc = add_usecase

        x_operands = [1+0j, 1j, -1-1j]
        y_operands = x_operands

        types_list = [(types.complex64, types.complex64),
                      (types.complex128, types.complex128),]

        self.run_test_floats(pyfunc, x_operands, y_operands, types_list)

    # @unittest.SkipTest
    def test_sub_complex(self):
        pyfunc = sub_usecase

        x_operands = [1+0j, 1j, -1-1j]
        y_operands = [1, 2, 3]

        types_list = [(types.complex64, types.complex64),
                      (types.complex128, types.complex128),]

        self.run_test_floats(pyfunc, x_operands, y_operands, types_list)


if __name__ == '__main__':
    unittest.main()

