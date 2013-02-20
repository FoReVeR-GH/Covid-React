from numba import *
import numba as nb

@autojit
def index(type):
    """
    >>> index(int_)
    ['[0, 1, 2]', '0', '1', '2']
    >>> assert index(int_) == index.py_func(int_)

    >>> index(float_)
    ['[0.0, 1.0, 2.0]', '0.0', '1.0', '2.0']
    >>> assert index(float_) == index.py_func(float_)

    >>> index(complex128)
    ['[0j, (1+0j), (2+0j)]', '0j', '(1+0j)', '(2+0j)']
    >>> assert index(complex128) == index.py_func(complex128)
    """
    tlist = nb.typedlist(type)
    tlist.append(0)
    tlist.append(1)
    tlist.append(2)
    return map(str, [tlist, tlist[0], tlist[1], tlist[2]])

@autojit
def index_error(type):
    """
    >>> index_error(int_)
    Traceback (most recent call last):
        ...
    IndexError: list index out of range

    >>> index_error(float_)
    Traceback (most recent call last):
        ...
    IndexError: list index out of range
    """
    tlist = nb.typedlist(type)
    tlist.append(0)
    tlist.append(1)
    tlist.append(2)
    return tlist[4]

@autojit
def append(type):
    """
    >>> append(int_)
    (0L, 1L, 2L, 3L)
    """
    tlist = nb.typedlist(type)
    l1 = len(tlist)
    tlist.append(0)
    l2 = len(tlist)
    tlist.append(1)
    l3 = len(tlist)
    tlist.append(2)
    l4 = len(tlist)
    return l1, l2, l3, l4

@autojit
def append_many(type):
    """
    >>> append_many(int_)
    1000L
    """
    tlist = nb.typedlist(type)
    for i in range(1000):
        tlist.append(i)
    return len(tlist)

@autojit
def pop(type):
    """
    >>> pop(int_)
    (3L, 2L, 1L, 0L)
    """
    tlist = nb.typedlist(type)
    for i in range(3):
        tlist.append(i)

    l1 = len(tlist)
    tlist.pop()
    l2 = len(tlist)
    tlist.pop()
    l3 = len(tlist)
    tlist.pop()
    l4 = len(tlist)
    return l1, l2, l3, l4

@autojit
def pop_many(type):
    """
    >>> pop_many(int_)
    (1000L, 0L)
    """
    tlist = nb.typedlist(type)
    for i in range(1000):
        tlist.append(i)

    initial_length = len(tlist)

    for i in range(1000):
        tlist.pop()

    return initial_length, len(tlist)

if __name__ == "__main__":
    nb.testmod()
