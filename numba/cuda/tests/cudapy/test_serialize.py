from __future__ import print_function
import pickle
import numpy as np
from numba import cuda
from numba import unittest_support as unittest
from numba.config import ENABLE_CUDASIM


@unittest.skipIf(ENABLE_CUDASIM, 'pickling not supported in CUDASIM')
class TestPickle(unittest.TestCase):

    def check_call(self, callee):
        arr = np.array([100])
        expected = callee(arr)

        # serialize and rebuild
        foo1 = pickle.loads(pickle.dumps(callee))
        del callee
        # call rebuild function
        got1 = foo1(arr)
        np.testing.assert_equal(got1, expected)
        del got1

        # test serialization of previously serialized object
        foo2 = pickle.loads(pickle.dumps(foo1))
        del foo1
        # call rebuild function
        got2 = foo2(arr)
        np.testing.assert_equal(got2, expected)
        del got2

        # test propagation of thread, block config
        foo3 = pickle.loads(pickle.dumps(foo2[5, 8]))
        del foo2
        self.assertEqual(foo3.griddim, (5, 1, 1))
        self.assertEqual(foo3.blockdim, (8, 1, 1))

    def test_pickling_jit(self):
        @cuda.jit(device=True)
        def inner(a):
            return a + 1

        @cuda.jit('void(intp[:])')
        def foo(arr):
            arr[0] = inner(arr[0])

        self.check_call(foo)

    def test_pickling_autojit(self):

        @cuda.jit(device=True)
        def inner(a):
            return a + 1

        @cuda.jit
        def foo(arr):
            arr[0] = inner(arr[0])


if __name__ == '__main__':
    unittest.main()
