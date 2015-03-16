from __future__ import absolute_import, print_function
import numpy as np
from numba import cuda
from numba.cuda.testing import unittest


class TestCudaEvent(unittest.TestCase):
    def test_event_elapsed(self):
        N = 32
        dary = cuda.device_array(N, dtype=np.double)
        evtstart = cuda.event()
        evtend = cuda.event()

        evtstart.record()
        cuda.to_device(np.arange(N), to=dary)
        evtend.record()
        evtend.wait()
        evtend.synchronize()
        # Exercise the code path
        evtstart.elapsed_time(evtend)

    def test_event_elapsed_stream(self):
        N = 32
        stream = cuda.stream()
        dary = cuda.device_array(N, dtype=np.double)
        evtstart = cuda.event()
        evtend = cuda.event()

        evtstart.record(stream=stream)
        cuda.to_device(np.arange(N), to=dary, stream=stream)
        evtend.record(stream=stream)
        evtend.wait(stream=stream)
        evtend.synchronize()
        # Exercise the code path
        evtstart.elapsed_time(evtend)

if __name__ == '__main__':
    unittest.main()
