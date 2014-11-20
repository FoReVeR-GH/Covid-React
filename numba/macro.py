"""
Macro handling passes

Macros are expanded on block-by-block
"""
from __future__ import absolute_import, print_function, division
from numba import ir


class MacroError(Exception):
    pass


def expand_macros(blocks):
    constants = {}
    for blk in blocks.values():
        module_getattr_folding(constants, blk)
        expand_macros_in_block(constants, blk)


def module_getattr_folding(constants, block):
    for inst in block.body:
        if isinstance(inst, ir.Assign):
            rhs = inst.value

            if isinstance(rhs, ir.Global):
                constants[inst.target.name] = rhs.value

            elif isinstance(rhs, ir.Expr):
                if rhs.op == 'getattr':
                    if rhs.value.name in constants:
                        base = constants[rhs.value.name]
                        constants[inst.target.name] = getattr(base, rhs.attr)

                elif rhs.op == 'build_tuple':
                    if all(i.name in constants for i in rhs.items):
                        tupk = tuple(constants[i.name] for i in rhs.items)
                        constants[inst.target.name] = tupk

                elif rhs.op == 'build_list':
                    if all(i.name in constants for i in rhs.items):
                        tupk = list(constants[i.name] for i in rhs.items)
                        constants[inst.target.name] = tupk

            elif isinstance(rhs, ir.Const):
                constants[inst.target.name] = rhs.value

            elif isinstance(rhs, ir.Var) and rhs.name in constants:
                constants[inst.target.name] = constants[rhs.name]

            elif isinstance(rhs, ir.FreeVar):
                constants[inst.target.name] = rhs.value

def expand_macros_in_block(constants, block):
    calls = []
    for inst in block.body:
        if isinstance(inst, ir.Assign):
            rhs = inst.value
            if isinstance(rhs, ir.Expr) and rhs.op == 'call':
                callee = rhs.func
                macro = constants.get(callee.name)
                if isinstance(macro, Macro):
                    # Rewrite calling macro
                    assert macro.callable
                    calls.append((inst, macro))
                    args = [constants[arg.name] for arg in rhs.args]

                    kws = {}
                    for k, v in rhs.kws:
                        if v.name in constants:
                            kws[k] = constants[v.name]

                        else:
                            msg = "Argument {name!r} must be a " \
                                  "constant at {loc}".format(name=k,
                                                             loc=inst.loc)
                            raise ValueError(msg)

                    try:
                        result = macro.func(*args, **kws)
                    except BaseException as e:
                        msg = str(e)
                        headfmt = "Macro expansion failed at {line}"
                        head = headfmt.format(line=inst.loc)
                        newmsg = "{0}:\n{1}".format(head, msg)
                        raise MacroError(newmsg)
                    if result:
                        # Insert a new function
                        result.loc = rhs.loc
                        inst.value = ir.Expr.call(func=result, args=rhs.args,
                                                  kws=rhs.kws, loc=rhs.loc)
            elif isinstance(rhs, ir.Expr) and rhs.op == 'getattr':
                # Rewrite get attribute to macro call
                # Non-calling macro must be triggered by get attribute
                base = constants.get(rhs.value.name)
                if base is not None:
                    value = getattr(base, rhs.attr)
                    if isinstance(value, Macro):
                        macro = value
                        if not macro.callable:
                            intr = ir.Intrinsic(macro.name, macro.func, args=())
                            inst.value = ir.Expr.call(func=intr, args=(),
                                                      kws=(), loc=rhs.loc)


class Macro(object):
    """A macro object is expanded to a function call
    """
    __slots__ = 'name', 'func', 'callable', 'argnames'

    def __init__(self, name, func, callable=False, argnames=None):
        self.name = name
        self.func = func
        self.callable = callable
        self.argnames = argnames

    def __repr__(self):
        return '<macro %s -> %s>' % (self.name, self.func)

