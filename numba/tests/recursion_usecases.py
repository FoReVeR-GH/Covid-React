"""
Usecases of recursive functions.

Some functions are compiled at import time, hence a separate module.
"""

from numba import jit


@jit("i8(i8)", nopython=True)
def fib1(n):
    if n < 2:
        return n
    # Note the second call uses a named argument
    return fib1(n - 1) + fib1(n=n - 2)


def make_fib2():
    @jit("i8(i8)", nopython=True)
    def fib2(n):
        if n < 2:
            return n
        return fib2(n - 1) + fib2(n=n - 2)

    return fib2

fib2 = make_fib2()


# Implicit signature
@jit(nopython=True)
def fib3(n):
    if n < 2:
        return n
    return fib3(n - 1) + fib3(n - 2)


# Run-away self recursion
@jit(nopython=True)
def runaway_self(x):
    return runaway_self(x)


# Mutual recursion
@jit(nopython=True)
def outer_fac(n):
    if n < 1:
        return 1
    return n * inner_fac(n - 1)


@jit(nopython=True)
def inner_fac(n):
    if n < 1:
        return 1
    return n * outer_fac(n - 1)


# Mutual recursion with different arg names
def make_mutual2(jit=lambda x: x):
    @jit
    def foo(x):
        if x > 0:
            return 2 * bar(z=1, y=x)
        return 1 + x

    @jit
    def bar(y, z):
        return foo(x=y - z)

    return foo, bar


# Mutual runaway recursion

@jit(nopython=True)
def runaway_mutual(x):
    return runaway_mutual_inner(x)


@jit(nopython=True)
def runaway_mutual_inner(x):
    return runaway_mutual(x)