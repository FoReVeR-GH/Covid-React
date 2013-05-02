# -*- coding: utf-8 -*-
from __future__ import print_function, division, absolute_import

from numba.typesystem.kinds import *

#------------------------------------------------------------------------
# Promotion on kind
#------------------------------------------------------------------------

table = {
    (KIND_POINTER, KIND_INT):   KIND_POINTER,
    (KIND_INT, KIND_POINTER):   KIND_POINTER,
    (KIND_POINTER, KIND_NULL):  KIND_POINTER,
    (KIND_NULL, KIND_POINTER):  KIND_POINTER,
    (KIND_OBJECT, KIND_OBJECT): KIND_OBJECT,
    (KIND_BOOL, KIND_BOOL):     KIND_BOOL,
}

def promote_from_table(table, u, promote, type1, type2):
    result = table.get((type1.kind, type2.kind))
    if result is not None:
        return { type1.kind: type1, type2.kind: type2}[result]
    return None

#------------------------------------------------------------------------
# Numeric promotion
#------------------------------------------------------------------------

def find_type_of_size(size, typelist):
    for type in typelist:
        if type.itemsize == size:
            return type

    assert False, "Type of size %d not found: %s" % (size, typelist)

def promote_numeric(u, promote, type1, type2):
    "Promote two numeric types"
    type = max([type1, type2], key=lambda type: type.rank)
    if type1.kind != type2.kind:
        def itemsize(type):
            size = u.itemsize(type)
            return size // 2 if type.is_complex else size

        size = max(itemsize(type1), itemsize(type2))
        if type.is_complex:
            type = find_type_of_size(size * 2, complextypes)
        elif type.is_float:
            type = find_type_of_size(size, floating)
        else:
            assert type.is_int
            type = find_type_of_size(size, integral)

    return type

#------------------------------------------------------------------------
# Array promotion
#------------------------------------------------------------------------

def promote_arrays(u, promote, type1, type2):
    "Promote two array types in an expression to a new array type"
    equal_ndim = type1.ndim == type2.ndim
    return u.array(promote(type1.dtype, type2.dtype),
                   ndim=max((type1.ndim, type2.ndim)),
                   is_c_contig=(equal_ndim and type1.is_c_contig and
                                type2.is_c_contig),
                   is_f_contig=(equal_ndim and type1.is_f_contig and
                                type2.is_f_contig))

def promote_array_and_other(u, promote, type1, type2):
    if type1.is_array:
        array_type = type1
        other_type = type2
    else:
        array_type = type2
        other_type = type1

    if other_type.is_object and not array_type.dtype.is_object:
        # Make sure that (double[:], object_) -> object_
        return u.object

    dtype = promote(array_type.dtype, other_type)
    return u.array(dtype, array_type.ndim)

#------------------------------------------------------------------------
# Default type promotion
#------------------------------------------------------------------------

class DefaultPromoter(object):

    def __init__(self, universe, promotion_table):
        self.universe = universe
        self.promotion_table = promotion_table

    def promote(self, type1, type2):
        "Promote two arbitrary types"
        u = self.universe
        args = u, self.promote, type1, type2
        result = promote_from_table(self.promotion_table, *args)

        if result is not None:
            return result
        elif type1.is_numeric and type2.is_numeric:
            return promote_numeric(*args)
        elif type1.is_array and type2.is_array:
            return promote_arrays(*args)
        elif type1.is_array or type2.is_array:
            return promote_array_and_other(*args)
        elif type1 == char.pointer():
            return u.c_string_type
        elif type1.is_object or type2.is_object:
            return u.object_
        else:
            raise TypeError((type1, type2))

def have_properties(type1, type2, property1, property2):
    """
    Return whether the two types satisfy the two properties:

    >>> have_properties(int32, int32.pointer(), "is_pointer", "is_int")
    True
    """
    type1_p1 = getattr(type1, property1)
    type1_p2 = getattr(type1, property2)
    type2_p1 = getattr(type2, property1)
    type2_p2 = getattr(type2, property2)

    if (type1_p1 and type2_p2) or (type1_p2 and type2_p1):
        if type1_p1:
            return type1
        else:
            return type2
    else:
        return None

#------------------------------------------------------------------------
# Promote
#------------------------------------------------------------------------

def get_default_promoter(universe):
    return DefaultPromoter(universe, table).promote