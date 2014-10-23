from __future__ import division
from itertools import product
from numba import unittest_support as unittest
from numba import typeof
from numba.compiler import compile_isolated
import numpy as np


def array_sum(arr):
    return arr.sum()


def array_sum_global(arr):
    return np.sum(arr)


def array_prod(arr):
    return arr.prod()


def array_prod_global(arr):
    return np.prod(arr)


def array_flat(arr, out):
    for i, v in enumerate(arr.flat):
        out[i] = v


def array_mean(arr):
    return arr.mean()


def array_mean_global(arr):
    return np.mean(arr)


def array_var(arr):
    return arr.var()


def array_var_global(arr):
    return np.var(arr)


def array_std(arr):
    return arr.std()


def array_std_global(arr):
    return np.std(arr)


def array_min(arr):
    return arr.min()


def array_min_global(arr):
    return np.min(arr)


def array_max(arr):
    return arr.max()


def array_max_global(arr):
    return np.max(arr)


def array_argmin(arr):
    return arr.argmin()


def array_argmin_global(arr):
    return np.argmin(arr)


def array_argmax(arr):
    return arr.argmax()


def array_argmax_global(arr):
    return np.argmax(arr)

def base_test_arrays(dtype):
    a1 = np.arange(10, dtype=dtype) + 1
    a2 = np.arange(10, dtype=dtype).reshape(2, 5) + 1
    a3 = (np.arange(60, dtype=dtype))[::2].reshape((2, 5, 3), order='A')

    return [a1, a2, a3]


def yield_test_props():
    return [(1, 'C'), (2, 'C'), (3, 'A')]


def full_test_arrays(dtype):
    array_list = base_test_arrays(dtype)

    #Add floats with some mantissa
    if dtype == np.float32:
        array_list += [a / 10 for a in array_list]

    return array_list

def run_comparative(funcToCompare, testArray):
    arrty = typeof(testArray)
    cres = compile_isolated(funcToCompare, [arrty])
    numpyResult = funcToCompare(testArray)
    numbaResult = cres.entry_point(testArray)

    if numpyResult.dtype is np.float32:
        return np.allclose(numpyResult, numbaResult, rtol=1e-6)

    return np.all(numpyResult == numbaResult)


def array_prop(aray):
    arrty = typeof(aray)
    return (arrty.ndim, arrty.layout)
    

class TestArrayMethods(unittest.TestCase):
    def test_array_ndim_and_layout(self):
        for testArray, testArrayProps in zip(base_test_arrays(np.int32), yield_test_props()):
            self.assertEqual(array_prop(testArray), testArrayProps)

    def test_sum_basic(self):
        arr = np.arange(100)
        self.assertTrue(run_comparative(array_sum, arr))

    def test_mean_basic(self):
        arr = np.arange(100)
        self.assertTrue(run_comparative(array_mean, arr))

    def test_var_basic(self):
        arr = np.arange(100)
        self.assertTrue(run_comparative(array_var, arr))      

    def test_std_basic(self):
        arr = np.arange(100)
        self.assertTrue(run_comparative(array_std, arr))

    def test_min_basic(self):
        arr = np.arange(100)
        self.assertTrue(run_comparative(array_min, arr))

    def test_max_basic(self):
        arr = np.arange(100)
        self.assertTrue(run_comparative(array_max, arr))

    def test_argmin_basic(self):
        arr = np.arange(100)
        self.assertTrue(run_comparative(array_argmin, arr))

    def test_argmax_basic(self):
        arr = np.arange(100)
        self.assertTrue(run_comparative(array_argmax, arr))


    def check_array_flat(self, arr):
        out = np.zeros(arr.size, dtype=arr.dtype)
        nb_out = out.copy()

        cres = compile_isolated(array_flat, [typeof(arr), typeof(out)])
        cfunc = cres.entry_point

        array_flat(arr, out)
        cfunc(arr, nb_out)

        self.assertTrue(np.all(out == nb_out), (out, nb_out))

    def test_array_flat_3d(self):
        arr = np.arange(24).reshape(4, 2, 3)

        arrty = typeof(arr)
        self.assertEqual(arrty.ndim, 3)
        self.assertEqual(arrty.layout, 'C')
        self.assertTrue(arr.flags.c_contiguous)
        # Test with C-contiguous array
        self.check_array_flat(arr)
        # Test with Fortran-contiguous array
        arr = arr.transpose()
        self.assertFalse(arr.flags.c_contiguous)
        self.assertTrue(arr.flags.f_contiguous)
        self.assertEqual(typeof(arr).layout, 'F')
        self.check_array_flat(arr)
        # Test with non-contiguous array
        arr = arr[::2]
        self.assertFalse(arr.flags.c_contiguous)
        self.assertFalse(arr.flags.f_contiguous)
        self.assertEqual(typeof(arr).layout, 'A')
        self.check_array_flat(arr)

# These form a testing product where each of the combinations are tested
reduction_funcs = [array_sum, array_sum_global, 
                   array_prod, array_prod_global, 
                   array_mean, array_mean_global,
                   array_var, array_var_global,
                   array_std, array_std_global,
                   array_min, array_min_global,
                   array_max, array_max_global,
                   array_argmin, array_argmin_global,
                   array_argmax, array_argmax_global]
dtypes_to_test = [np.int32, np.float32]

# Install tests on class above
for dt in dtypes_to_test:
    for redFunc, testArray in product(reduction_funcs, full_test_arrays(dt)):
        def installedFunction(self):
            self.assertTrue(run_comparative(redFunc, testArray))

        # Create the name for the test function 
        testName = "test_{0}_{1}_{2}d".format(redFunc.__name__, testArray.dtype.name, testArray.ndim)

        # Install it into the class
        setattr(TestArrayMethods, testName, installedFunction)


if __name__ == '__main__':
    unittest.main()
