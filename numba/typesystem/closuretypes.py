# -*- coding: utf-8 -*-

"""
Types for closures and inner functions.
"""

from __future__ import print_function, division, absolute_import

from numba.typesystem import NumbaType
from numba.exttypes.types.extensiontype import ExtensionType

class ClosureType(NumbaType):
    """
    Type of closures and inner functions.
    """

    typename = "closure"
    args = ["signature", "closure"]
    default = { "closure": None}
    flags = ["object"]
    mutable = True

    def add_scope_arg(self, scope_type):
        self.signature.args = (scope_type,) + self.signature.args

    def __repr__(self):
        return "<closure(%s)>" % self.signature

class ClosureScopeType(ExtensionType):
    """
    Type of the enclosing scope for closures. This is always passed in as
    first argument to the function.
    """

    typename = "closure_scope"
    mutable = True
    is_final = True

    def __init__(self, py_class, parent_scope):
        super(ClosureScopeType, self).__init__()
        self.parent_scope = parent_scope
        self.unmangled_symtab = None

        if self.parent_scope is not None:
            self.scope_prefix = ""
        else:
            self.scope_prefix = self.parent_scope.scope_prefix + "0"