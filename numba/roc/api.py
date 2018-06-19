from __future__ import absolute_import, print_function

import numpy as np

from numba.cuda.api import _prepare_shape_strides_dtype
from numba.cuda.cudadrv.driver import memory_size_from_info as \
agnostic_memory_size_from_info
from numba.roc.hsadrv.devices import get_context

from .stubs import (
    get_global_id,
    get_global_size,
    get_local_id,
    get_local_size,
    get_group_id,
    get_work_dim,
    get_num_groups,
    barrier,
    mem_fence,
    shared,
    wavebarrier,
    activelanepermute_wavewidth,
    ds_permute,
    ds_bpermute,
)

from .decorators import (
    jit,
)

from .enums import (
    CLK_LOCAL_MEM_FENCE,
    CLK_GLOBAL_MEM_FENCE
)

from .hsadrv.driver import hsa as _hsadrv
from .hsadrv import devicearray


class _AutoDeregister(object):
    def __init__(self, args):
        self.args = args

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        deregister(*self.args)


def register(*args):
    """Register data into the HSA system

    Returns a contextmanager for use in with-context for auto deregistration.

    Use in context:

        with hsa.register(array):
            do_work_on_HSA(array)

    """
    for data in args:
        if isinstance(data, np.ndarray):
            _hsadrv.hsa_memory_register(data.ctypes.data, data.nbytes)
        else:
            raise TypeError(type(data))
    return _AutoDeregister(args)


def deregister(*args):
    """Deregister data form the HSA system
    """
    for data in args:
        if isinstance(data, np.ndarray):
            _hsadrv.hsa_memory_deregister(data.ctypes.data, data.nbytes)
        else:
            raise TypeError(type(data))

def device_array(shape, dtype=np.float, strides=None, order='C'):
    """device_array(shape, dtype=np.float, strides=None, order='C')

    Allocate an empty device ndarray. Similar to :meth:`numpy.empty`.
    """
    shape, strides, dtype = _prepare_shape_strides_dtype(shape, strides, dtype,
                                                         order)
    return devicearray.DeviceNDArray(shape=shape, strides=strides, dtype=dtype)


def device_array_like(ary):
    """Call hsa.devicearray() with information from the array.
    """
    return device_array(shape=ary.shape, dtype=ary.dtype, strides=ary.strides)


def to_device(obj, stream=None, context=None, copy=True, to=None):
    """to_device(obj, context, copy=True, to=None)

    Allocate and transfer a numpy ndarray or structured scalar to the device.

    To copy host->device a numpy array::

        ary = numpy.arange(10)
        d_ary = hsa.to_device(ary)

    The resulting ``d_ary`` is a ``DeviceNDArray``.

    To copy device->host::

        hary = d_ary.copy_to_host()

    To copy device->host to an existing array::

        ary = numpy.empty(shape=d_ary.shape, dtype=d_ary.dtype)
        d_ary.copy_to_host(ary)

    """
    context = context or get_context()

    if to is None:
        to = devicearray.from_array_like(obj)

    if copy:
        to.copy_to_device(obj, stream=stream, context=context)
    return to


def stream():
    return _hsadrv.create_stream()


def _host_array(finegrain, shape, dtype, strides, order):
    from .hsadrv import devices
    shape, strides, dtype = _prepare_shape_strides_dtype(shape, strides, dtype,
                                                         order)
    bytesize = agnostic_memory_size_from_info(shape, strides, dtype.itemsize)
    # TODO does allowing access by all dGPUs really work in a multiGPU system?
    agents = [c._agent for c in devices.get_all_contexts()]
    buf = devices.get_cpu_context().memhostalloc(bytesize, finegrain=finegrain,
                                                 allow_access_to=agents)
    arr = np.ndarray(shape=shape, strides=strides, dtype=dtype, order=order,
                     buffer=buf)
    return arr.view(type=devicearray.HostArray)


def coarsegrain_array(shape, dtype=np.float, strides=None, order='C'):
    """coarsegrain_array(shape, dtype=np.float, strides=None, order='C')
    Similar to np.empty().
    """
    return _host_array(finegrain=False, shape=shape, dtype=dtype,
                       strides=strides, order=order)


def finegrain_array(shape, dtype=np.float, strides=None, order='C'):
    """finegrain_array(shape, dtype=np.float, strides=None, order='C')

    Similar to np.empty().
    """
    return _host_array(finegrain=False, shape=shape, dtype=dtype,
                       strides=strides, order=order)
