"""
This file fixes portability issues for ctypes
"""
from __future__ import absolute_import
import ctypes
from numba import types


CTYPES_MAP = {
    ctypes.c_int8: types.int8,
    ctypes.c_int16: types.int16,
    ctypes.c_int32: types.int32,
    ctypes.c_int64: types.int64,

    ctypes.c_uint8: types.uint8,
    ctypes.c_uint16: types.uint16,
    ctypes.c_uint32: types.uint32,
    ctypes.c_uint64: types.uint64,

    ctypes.c_float: types.float32,
    ctypes.c_double: types.float64,
}


def convert_ctypes(ctypeobj):
    try:
        return CTYPES_MAP[ctypeobj]
    except KeyError:
        raise TypeError("unhandled ctypes type: %s" % ctypeobj)


def is_ctypes_funcptr(obj):
    try:
        # Is it something of which we can get the address
        ctypes.cast(obj, ctypes.c_void_p)
    except ctypes.ArgumentError:
        return False
    else:
        # Does it define argtypes and restype
        return hasattr(obj, 'argtypes') and hasattr(obj, 'restype')
