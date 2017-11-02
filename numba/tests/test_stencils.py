#
# Copyright (c) 2017 Intel Corporation
# SPDX-License-Identifier: BSD-2-Clause
#

from __future__ import print_function, division, absolute_import

import sys
import numpy as np

import numba
from numba import unittest_support as unittest
from numba import njit, stencil, types
from numba.compiler import compile_extra, Flags
from numba.targets import registry
from .support import tag

# for decorating tests, marking that Windows with Python 2.7 is not supported
_windows_py27 = (sys.platform.startswith('win32') and
                 sys.version_info[:2] == (2, 7))
_32bit = sys.maxsize <= 2 ** 32
_reason = 'parfors not supported'
skip_unsupported = unittest.skipIf(_32bit or _windows_py27, _reason)

@stencil
def stencil1_kernel(a):
    return 0.25 * (a[0,1] + a[1,0] + a[0,-1] + a[-1,0])

@stencil(neighborhood=((-5, 0), ))
def stencil2_kernel(a):
    cum = a[-5]
    for i in range(-4,1):
        cum += a[i]
    return 0.3 * cum

@stencil(cval=1.0)
def stencil3_kernel(a):
    return 0.25 * a[-2,2]

@stencil
def stencil_multiple_input_kernel(a, b):
    return 0.25 * (a[0,1] + a[1,0] + a[0,-1] + a[-1,0] +
                   b[0,1] + b[1,0] + b[0,-1] + b[-1,0])

@stencil
def stencil_multiple_input_kernel_var(a, b, w):
    return w * (a[0,1] + a[1,0] + a[0,-1] + a[-1,0] +
                b[0,1] + b[1,0] + b[0,-1] + b[-1,0])

@stencil(standard_indexing=("b",))
def stencil_with_standard_indexing_1d(a, b):
    return a[-1] * b[0] + a[0] * b[1]

@stencil(standard_indexing=("b",))
def stencil_with_standard_indexing_2d(a, b):
    return a[0,1] * b[0,1] + a[1,0] * b[1,0] + a[0,-1] * b[0,-1] + a[-1,0] * b[-1,0]

class TestStencils(unittest.TestCase):

    def __init__(self, *args):
        # flags for njit()
        self.cflags = Flags()
        self.cflags.set('nrt')

        # flags for njit(parallel=True)
        self.pflags = Flags()
        self.pflags.set('auto_parallel')
        self.pflags.set('nrt')
        super(TestStencils, self).__init__(*args)

    def _compile_this(self, func, sig, flags):
        return compile_extra(registry.cpu_target.typing_context,
                registry.cpu_target.target_context, func, sig, None,
                         flags, {})

    def compile_parallel(self, func, sig):
        return self._compile_this(func, sig, flags=self.pflags)

    def compile_njit(self, func, sig):
        return self._compile_this(func, sig, flags=self.cflags)

    def compile_all(self, pyfunc, *args, **kwargs):
        sig = tuple([numba.typeof(x) for x in args])
        # compile with parallel=True
        cpfunc = self.compile_parallel(pyfunc, sig)
        # compile a standard njit of the original function
        cfunc = self.compile_njit(pyfunc, sig)
        return cfunc, cpfunc

    def check(self, no_stencil_func, pyfunc, *args):
        cfunc, cpfunc = self.compile_all(pyfunc, *args)
        # results without stencil macro
        expected = no_stencil_func(*args)
        # python result
        py_output = pyfunc(*args)

        # njit result
        njit_output = cfunc.entry_point(*args)

        # parfor result
        parfor_output = cpfunc.entry_point(*args)

        np.testing.assert_almost_equal(py_output, expected, decimal=3)
        np.testing.assert_almost_equal(njit_output, expected, decimal=3)
        np.testing.assert_almost_equal(parfor_output, expected, decimal=3)

        # make sure parfor set up scheduling
        self.assertIn('@do_scheduling', cpfunc.library.get_llvm_str())


    @skip_unsupported
    @tag('important')
    def test_stencil1(self):
        """Tests whether the optional out argument to stencil calls works.
        """
        def test_with_out(n):
            A = np.arange(n**2).reshape((n, n))
            B = np.zeros(n**2).reshape((n, n))
            B = stencil1_kernel(A, out=B)
            return B

        def test_without_out(n):
            A = np.arange(n**2).reshape((n, n))
            B = stencil1_kernel(A)
            return B

        def test_impl_seq(n):
            A = np.arange(n**2).reshape((n, n))
            B = np.zeros(n**2).reshape((n, n))
            for i in range(1, n-1):
                for j in range(1, n-1):
                    B[i,j] = 0.25 * (A[i,j+1] + A[i+1,j] + A[i,j-1] + A[i-1,j])
            return B

        n = 100
        self.check(test_impl_seq, test_with_out, n)
        self.check(test_impl_seq, test_without_out, n)

    @skip_unsupported
    @tag('important')
    def test_stencil2(self):
        """Tests whether the optional neighborhood argument to the stencil
        decorate works.
        """
        def test_seq(n):
            A = np.arange(n)
            B = stencil2_kernel(A)
            return B

        def test_impl_seq(n):
            A = np.arange(n)
            B = np.zeros(n)
            for i in range(5, len(A)):
                B[i] = 0.3 * sum(A[i-5:i+1])
            return B

        n = 100
        self.check(test_impl_seq, test_seq, n)
        # variable length neighborhood in numba.stencil call
        # only supported in parallel path
        def test_seq(n, w):
            A = np.arange(n)
            def stencil2_kernel(a, w):
                cum = a[-w]
                for i in range(-w+1, w+1):
                    cum += a[i]
                return 0.3 * cum
            B = numba.stencil(stencil2_kernel, neighborhood=((-w, w), ))(A, w)
            return B

        def test_impl_seq(n, w):
            A = np.arange(n)
            B = np.zeros(n)
            for i in range(w, len(A)-w):
                B[i] = 0.3 * sum(A[i-w:i+w+1])
            return B
        n = 100
        w = 5
        cpfunc = self.compile_parallel(test_seq, (types.intp, types.intp))
        expected = test_impl_seq(n, w)
        # parfor result
        parfor_output = cpfunc.entry_point(n, w)
        np.testing.assert_almost_equal(parfor_output, expected, decimal=3)
        self.assertIn('@do_scheduling', cpfunc.library.get_llvm_str())
        # test index_offsets
        def test_seq(n, w, offset):
            A = np.arange(n)
            def stencil2_kernel(a, w):
                cum = a[-w+1]
                for i in range(-w+1, w+1):
                    cum += a[i+1]
                return 0.3 * cum
            B = numba.stencil(stencil2_kernel, neighborhood=((-w, w), ),
                    index_offsets=(-offset, ))(A, w)
            return B

        offset = 1
        cpfunc = self.compile_parallel(test_seq, (types.intp, types.intp,
                                                                    types.intp))
        parfor_output = cpfunc.entry_point(n, w, offset)
        np.testing.assert_almost_equal(parfor_output, expected, decimal=3)
        self.assertIn('@do_scheduling', cpfunc.library.get_llvm_str())
        # test slice in kernel
        def test_seq(n, w, offset):
            A = np.arange(n)
            def stencil2_kernel(a, w):
                return 0.3 * np.sum(a[-w+1:w+2])
            B = numba.stencil(stencil2_kernel, neighborhood=((-w, w), ),
                    index_offsets=(-offset, ))(A, w)
            return B

        offset = 1
        cpfunc = self.compile_parallel(test_seq, (types.intp, types.intp,
                                                                    types.intp))
        parfor_output = cpfunc.entry_point(n, w, offset)
        np.testing.assert_almost_equal(parfor_output, expected, decimal=3)
        self.assertIn('@do_scheduling', cpfunc.library.get_llvm_str())

    @skip_unsupported
    @tag('important')
    def test_stencil3(self):
        """Tests whether a non-zero optional cval argument to the stencil
        decorator works.  Also tests integer result type.
        """
        def test_seq(n):
            A = np.arange(n**2).reshape((n, n))
            B = stencil3_kernel(A)
            return B

        test_njit = njit(test_seq)
        test_par = njit(test_seq, parallel=True)

        n = 5
        seq_res = test_seq(n)
        njit_res = test_njit(n)
        par_res = test_par(n)

        self.assertTrue(seq_res[0,0] == 1.0 and seq_res[4,4] == 1.0)
        self.assertTrue(njit_res[0,0] == 1.0 and njit_res[4,4] == 1.0)
        self.assertTrue(par_res[0,0] == 1.0 and par_res[4,4] == 1.0)

    @skip_unsupported
    @tag('important')
    def test_stencil_standard_indexing_1d(self):
        """Tests standard indexing with a 1d array.
        """
        def test_seq(n):
            A = np.arange(n)
            B = [3.0, 7.0]
            C = stencil_with_standard_indexing_1d(A, B)
            return C

        def test_impl_seq(n):
            A = np.arange(n)
            B = [3.0, 7.0]
            C = np.zeros(n)

            for i in range(1, n):
                C[i] = A[i-1] * B[0] + A[i] * B[1]
            return C

        n = 100
        self.check(test_impl_seq, test_seq, n)

    @skip_unsupported
    @tag('important')
    def test_stencil_standard_indexing_2d(self):
        """Tests standard indexing with a 2d array and multiple stencil calls.
        """
        def test_seq(n):
            A = np.arange(n**2).reshape((n, n))
            B = np.ones((3,3))
            C = stencil_with_standard_indexing_2d(A, B)
            D = stencil_with_standard_indexing_2d(C, B)
            return D

        def test_impl_seq(n):
            A = np.arange(n**2).reshape((n, n))
            B = np.ones((3,3))
            C = np.zeros(n**2).reshape((n, n))
            D = np.zeros(n**2).reshape((n, n))

            for i in range(1, n-1):
                for j in range(1, n-1):
                    C[i,j] = (A[i,j+1] * B[0,1]  + A[i+1,j] * B[1,0] +
                              A[i,j-1] * B[0,-1] + A[i-1,j] * B[-1,0])
            for i in range(1, n-1):
                for j in range(1, n-1):
                    D[i,j] = (C[i,j+1] * B[0,1]  + C[i+1,j] * B[1,0] +
                              C[i,j-1] * B[0,-1] + C[i-1,j] * B[-1,0])
            return D

        n = 5
        self.check(test_impl_seq, test_seq, n)

    @skip_unsupported
    @tag('important')
    def test_stencil_multiple_inputs(self):
        """Tests whether multiple inputs of the same size work.
        """
        def test_seq(n):
            A = np.arange(n**2).reshape((n, n))
            B = np.arange(n**2).reshape((n, n))
            C = stencil_multiple_input_kernel(A, B)
            return C

        def test_impl_seq(n):
            A = np.arange(n**2).reshape((n, n))
            B = np.arange(n**2).reshape((n, n))
            C = np.zeros(n**2).reshape((n, n))
            for i in range(1, n-1):
                for j in range(1, n-1):
                    C[i,j] = 0.25 * (A[i,j+1] + A[i+1,j] + A[i,j-1] + A[i-1,j] +
                                     B[i,j+1] + B[i+1,j] + B[i,j-1] + B[i-1,j])
            return C

        n = 3
        self.check(test_impl_seq, test_seq, n)
        # test stencil with a non-array input
        def test_seq(n):
            A = np.arange(n**2).reshape((n, n))
            B = np.arange(n**2).reshape((n, n))
            w = 0.25
            C = stencil_multiple_input_kernel_var(A, B, w)
            return C
        self.check(test_impl_seq, test_seq, n)

    @skip_unsupported
    @tag('important')
    def test_stencil_call(self):
        """Tests 2D numba.stencil calls.
        """
        def test_impl1(n):
            A = np.arange(n**2).reshape((n, n))
            B = np.zeros(n**2).reshape((n, n))
            numba.stencil(lambda a: 0.25 * (a[0,1] + a[1,0] + a[0,-1]
                                + a[-1,0]))(A, out=B)
            return B

        def test_impl2(n):
            A = np.arange(n**2).reshape((n, n))
            B = np.zeros(n**2).reshape((n, n))
            def sf(a):
                return 0.25 * (a[0,1] + a[1,0] + a[0,-1] + a[-1,0])
            B = numba.stencil(sf)(A)
            return B

        def test_impl_seq(n):
            A = np.arange(n**2).reshape((n, n))
            B = np.zeros(n**2).reshape((n, n))
            for i in range(1, n-1):
                for j in range(1, n-1):
                    B[i,j] = 0.25 * (A[i,j+1] + A[i+1,j] + A[i,j-1] + A[i-1,j])
            return B

        n = 100
        self.check(test_impl_seq, test_impl1, n)
        self.check(test_impl_seq, test_impl2, n)

    @skip_unsupported
    @tag('important')
    def test_stencil_call_1D(self):
        """Tests 1D numba.stencil calls.
        """
        def test_impl(n):
            A = np.arange(n)
            B = np.zeros(n)
            numba.stencil(lambda a: 0.3 * (a[-1] + a[0] + a[1]))(A, out=B)
            return B

        def test_impl_seq(n):
            A = np.arange(n)
            B = np.zeros(n)
            for i in range(1, n-1):
                B[i] = 0.3 * (A[i-1] + A[i] + A[i+1])
            return B

        n = 100
        self.check(test_impl_seq, test_impl, n)

if __name__ == "__main__":
    unittest.main()
