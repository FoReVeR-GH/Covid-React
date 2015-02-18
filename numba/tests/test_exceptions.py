
import numpy as np

from numba.compiler import compile_isolated, Flags
from numba import jit, types
from numba import unittest_support as unittest
from numba.pythonapi import NativeError
from .support import TestCase


force_pyobj_flags = Flags()
force_pyobj_flags.set("force_pyobject")

no_pyobj_flags = Flags()


class MyError(Exception):
    pass

class OtherError(Exception):
    pass


def raise_class(exc):
    def raiser(i):
        if i == 1:
            raise exc
        elif i == 2:
            raise ValueError
        return i
    return raiser

def raise_instance(exc, arg):
    def raiser(i):
        if i == 1:
            raise exc(arg, 1)
        elif i == 2:
            raise ValueError(arg, 2)
        return i
    return raiser

def reraise():
    raise

def outer_function(inner):
    def outer(i):
        if i == 3:
            raise OtherError("bar", 3)
        return inner(i)
    return outer


class TestRaising(TestCase):

    def test_unituple_index_error(self):
        def pyfunc(a, i):
            return a.shape[i]

        cres = compile_isolated(pyfunc, (types.Array(types.int32, 1, 'A'),
                                         types.int32))

        cfunc = cres.entry_point
        a = np.empty(2)

        self.assertEqual(cfunc(a, 0), pyfunc(a, 0))

        with self.assertRaises(NativeError):
            cfunc(a, 2)

    def check_raise_class(self, flags):
        pyfunc = raise_class(MyError)
        cres = compile_isolated(pyfunc, (types.int32,), flags=flags)
        cfunc = cres.entry_point
        self.assertEqual(cfunc(0), 0)

        with self.assertRaises(MyError) as cm:
            cfunc(1)
        self.assertEqual(cm.exception.args, ())
        with self.assertRaises(ValueError) as cm:
            cfunc(2)
        self.assertEqual(cm.exception.args, ())

    def test_raise_class_nopython(self):
        self.check_raise_class(flags=no_pyobj_flags)

    def test_raise_class_objmode(self):
        self.check_raise_class(flags=force_pyobj_flags)

    def check_raise_instance(self, flags):
        pyfunc = raise_instance(MyError, "some message")
        cres = compile_isolated(pyfunc, (types.int32,), flags=flags)
        cfunc = cres.entry_point
        self.assertEqual(cfunc(0), 0)

        with self.assertRaises(MyError) as cm:
            cfunc(1)
        self.assertEqual(cm.exception.args, ("some message", 1))
        with self.assertRaises(ValueError) as cm:
            cfunc(2)
        self.assertEqual(cm.exception.args, ("some message", 2))

    def test_raise_instance_objmode(self):
        self.check_raise_instance(flags=force_pyobj_flags)

    def test_raise_instance_nopython(self):
        self.check_raise_instance(flags=no_pyobj_flags)

    def check_raise_nested(self, **jit_args):
        """
        Check exception propagation from nested functions.
        """
        inner_pyfunc = raise_instance(MyError, "some message")
        inner_cfunc = jit(**jit_args)(inner_pyfunc)
        cfunc = jit(**jit_args)(outer_function(inner_cfunc))

        with self.assertRaises(MyError) as cm:
            cfunc(1)
        self.assertEqual(cm.exception.args, ("some message", 1))
        with self.assertRaises(ValueError) as cm:
            cfunc(2)
        self.assertEqual(cm.exception.args, ("some message", 2))
        with self.assertRaises(OtherError) as cm:
            cfunc(3)
        self.assertEqual(cm.exception.args, ("bar", 3))

    def test_raise_nested(self):
        self.check_raise_nested(forceobj=True)

    def test_raise_nested_npm(self):
        self.check_raise_nested(nopython=True)

    def check_reraise(self, flags):
        pyfunc = reraise
        cres = compile_isolated(pyfunc, (), flags=flags)
        with self.assertRaises(ZeroDivisionError):
            try:
                1/0
            except ZeroDivisionError as e:
                pyfunc()

    def test_reraise_objmode(self):
        self.check_reraise(flags=force_pyobj_flags)

    def test_reraise_nopython(self):
        self.check_reraise(flags=no_pyobj_flags)

    def check_raise_invalid_class(self, cls, flags):
        pyfunc = raise_class(cls)
        cres = compile_isolated(pyfunc, (types.int32,), flags=flags)
        cfunc = cres.entry_point
        with self.assertRaises(TypeError) as cm:
            cfunc(1)
        self.assertEqual(str(cm.exception),
                         "exceptions must derive from BaseException")

    def test_raise_invalid_class_objmode(self):
        self.check_raise_invalid_class(int, flags=force_pyobj_flags)
        self.check_raise_invalid_class(1, flags=force_pyobj_flags)

    def test_raise_invalid_class_nopython(self):
        with self.assertTypingError():
            self.check_raise_invalid_class(int, flags=no_pyobj_flags)
        with self.assertTypingError():
            self.check_raise_invalid_class(1, flags=no_pyobj_flags)


if __name__ == '__main__':
    unittest.main()
