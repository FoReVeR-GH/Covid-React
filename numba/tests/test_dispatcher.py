from __future__ import print_function, division, absolute_import
from numba import unittest_support as unittest
from numba.special import typeof
from numba import vectorize, types, jit
import numpy
import sys

def dummy(x):
    return x


class TestDispatcher(unittest.TestCase):

    def test_typeof(self):
        self.assertEqual(typeof(numpy.int8(1)), types.int8)
        self.assertEqual(typeof(numpy.uint16(1)), types.uint16)
        self.assertEqual(typeof(numpy.float64(1)), types.float64)
        self.assertEqual(typeof(numpy.complex128(1)), types.complex128)

    def test_numba_interface(self):
        """
        Check that vectorize can accept a decorated object.
        """
        vectorize('f8(f8)')(jit(dummy))

    def test_no_argument(self):
        @jit
        def foo():
            return 1
        
        # Just make sure this doesn't crash
        foo()
    
    # test when a function parameters are jitted as unsigned types
    # when the function is called with negative parameters the Python error 
    # that it generates is correctly handled -- a Python error is returned to the user
    # For more info, see the comment in Include/longobject.h for _PyArray_AsByteArray 
    # which PyLong_AsUnsignedLongLong calls
    def test_negative_to_unsigned(self):
        def f(x):
            return x
        # TypeError is for 2.6
        if sys.version_info >= (2, 7)
            with self.assertRaises(OverflowError):
                jit('uintp(uintp)', nopython=True)(f)(-5)
        else:
            with self.assertRaises(TypeError):
                jit('uintp(uintp)', nopython=True)(f)(-5)

if __name__ == '__main__':
    unittest.main()
