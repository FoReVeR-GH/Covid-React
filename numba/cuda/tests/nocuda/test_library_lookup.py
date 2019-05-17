from __future__ import absolute_import

import sys
import os
import multiprocessing as mp

from numba.config import IS_WIN32
from numba.cuda.testing import unittest
from numba.cuda.testing import skip_on_cudasim
from numba.cuda.cudadrv.libs import (
    _get_libdevice_path_decision,
    _get_nvvm_path_decision,
    _get_cudalib_dir_path_decision,
    get_system_ctk,
)


has_mp_get_context = hasattr(mp, 'get_context')


class LibraryLookupBase(unittest.TestCase):
    def setUp(self):
        ctx = mp.get_context('spawn')

        qrecv = ctx.Queue()
        qsend = ctx.Queue()
        self.qsend = qsend
        self.qrecv = qrecv
        self.child_process = ctx.Process(
            target=check_lib_lookup,
            args=(qrecv, qsend),
            daemon=True,
        )
        self.child_process.start()

    def tearDown(self):
        self.qsend.put(self.do_terminate)
        self.child_process.join(3)
        # Ensure the process is terminated
        self.assertIsNotNone(self.child_process)

    def remote_fail(self, action):
        self.qsend.put(action)
        out = self.qrecv.get()
        self.assertIsInstance(out, BaseException)
        return out

    def remote_do(self, action):
        self.qsend.put(action)
        out = self.qrecv.get()
        self.assertNotIsInstance(out, BaseException)
        return out

    @staticmethod
    def do_terminate():
        return False, None


def remove_env(name):
    try:
        del os.environ[name]
    except KeyError:
        return False
    else:
        return True


def check_lib_lookup(qout, qin):
    status = True
    while status:
        try:
            action = qin.get()
        except BaseException as e:
            qout.put(e)
            status = False
        else:
            try:
                status, result = action()
                qout.put(result)
            except BaseException as e:
                qout.put(e)
                status = False


@skip_on_cudasim('Library detection unsupported in the simulator')
@unittest.skipUnless(has_mp_get_context, 'mp.get_context not available')
class TestLibDeviceLookUp(LibraryLookupBase):
    def test_libdevice_path_decision(self):
        # Check that the default is using conda environment
        by, _ = self.remote_do(self.do_clear_envs)
        self.assertEqual(by, 'Conda environment')
        # Check that CUDA_HOME works by removing conda-env
        by, info = self.remote_do(self.do_set_cuda_home)
        self.assertEqual(by, 'CUDA_HOME')
        self.assertEqual(info, os.path.join('mycudahome', 'nvvm', 'libdevice'))
        # Check that NUMBAPRO_LIBDEVICE override works
        by, info = self.remote_do(self.do_set_libdevice)
        self.assertEqual(by, 'NUMBAPRO_LIBDEVICE')
        self.assertEqual(info, os.path.join('nbp_libdevice'))
        if get_system_ctk() is None:
            # Fake remove conda environment so no cudatoolkit is available
            exc = self.remote_fail(self.do_clear_envs)
            self.assertIsInstance(exc, RuntimeError)
            self.assertEqual(str(exc), "cuda libraries not found")
        else:
            # Use system available cudatoolkit
            exc = self.remote_do(self.do_clear_envs)
            self.assertEqual(by, 'System')

    @staticmethod
    def do_clear_envs():
        remove_env('CUDA_HOME')
        remove_env('NUMBAPRO_LIBDEVICE')
        remove_env('NUMBAPRO_CUDALIB')
        return True, _get_libdevice_path_decision()

    @staticmethod
    def do_set_cuda_home():
        os.environ['CUDA_HOME'] = os.path.join('mycudahome')
        _fake_non_conda_env()
        return True, _get_libdevice_path_decision()

    @staticmethod
    def do_set_libdevice():
        os.environ['NUMBAPRO_LIBDEVICE'] = 'nbp_libdevice'
        return True, _get_libdevice_path_decision()


@skip_on_cudasim('Library detection unsupported in the simulator')
@unittest.skipUnless(has_mp_get_context, 'mp.get_context not available')
class TestNvvmLookUp(LibraryLookupBase):
    def test_nvvm_path_decision(self):
        # Check that the default is using conda environment
        by, _ = self.remote_do(self.do_clear_envs)
        self.assertEqual(by, 'Conda environment')
        # Check that CUDA_HOME works by removing conda-env
        by, info = self.remote_do(self.do_set_cuda_home)
        self.assertEqual(by, 'CUDA_HOME')
        if IS_WIN32:
            self.assertEqual(info, os.path.join('mycudahome', 'nvvm', 'bin'))
        else:
            self.assertEqual(info, os.path.join('mycudahome', 'nvvm', 'lib'))
        # Check that NUMBAPRO_CUDALIB override works
        by, info = self.remote_do(self.do_set_cuda_lib)
        self.assertEqual(by, 'NUMBAPRO_CUDALIB')
        self.assertEqual(info, os.path.join('nbp_cudalib'))
        # Check that NUMBAPRO_NVVM override works
        by, info = self.remote_do(self.do_set_nvvm)
        self.assertEqual(by, 'NUMBAPRO_NVVM')
        self.assertEqual(info, os.path.join('nbp_nvvm'))
        if get_system_ctk() is None:
            # Fake remove conda environment so no cudatoolkit is available
            exc = self.remote_fail(self.do_clear_envs)
            self.assertIsInstance(exc, RuntimeError)
            self.assertEqual(str(exc), "cuda libraries not found")
        else:
            # Use system available cudatoolkit
            exc = self.remote_do(self.do_clear_envs)
            self.assertEqual(by, 'System')

    @staticmethod
    def do_clear_envs():
        remove_env('CUDA_HOME')
        remove_env('NUMBAPRO_CUDALIB')
        remove_env('NUMBAPRO_NVVM')
        return True, _get_nvvm_path_decision()

    @staticmethod
    def do_set_cuda_home():
        os.environ['CUDA_HOME'] = os.path.join('mycudahome')
        _fake_non_conda_env()
        return True, _get_nvvm_path_decision()

    @staticmethod
    def do_set_cuda_lib():
        os.environ['NUMBAPRO_CUDALIB'] = 'nbp_cudalib'
        return True, _get_nvvm_path_decision()

    @staticmethod
    def do_set_nvvm():
        os.environ['NUMBAPRO_NVVM'] = 'nbp_nvvm'
        return True, _get_nvvm_path_decision()


@skip_on_cudasim('Library detection unsupported in the simulator')
@unittest.skipUnless(has_mp_get_context, 'mp.get_context not available')
class TestCudaLibLookUp(LibraryLookupBase):
    def test_cudalib_path_decision(self):
        # Check that the default is using conda environment
        by, _ = self.remote_do(self.do_clear_envs)
        self.assertEqual(by, 'Conda environment')
        # Check that CUDA_HOME works by removing conda-env
        by, info = self.remote_do(self.do_set_cuda_home)
        self.assertEqual(by, 'CUDA_HOME')
        if IS_WIN32:
            self.assertEqual(info, os.path.join('mycudahome', 'bin'))
        else:
            self.assertEqual(info, os.path.join('mycudahome', 'lib'))
        # Check that NUMBAPRO_CUDALIB override works
        by, info = self.remote_do(self.do_set_cuda_lib)
        self.assertEqual(by, 'NUMBAPRO_CUDALIB')
        self.assertEqual(info, os.path.join('nbp_cudalib'))
        if get_system_ctk() is None:
            # Fake remove conda environment so no cudatoolkit is available
            exc = self.remote_fail(self.do_clear_envs)
            self.assertIsInstance(exc, RuntimeError)
            self.assertEqual(str(exc), "cuda libraries not found")
        else:
            # Use system available cudatoolkit
            exc = self.remote_do(self.do_clear_envs)
            self.assertEqual(by, 'System')

    @staticmethod
    def do_clear_envs():
        remove_env('CUDA_HOME')
        remove_env('NUMBAPRO_CUDALIB')
        return True, _get_cudalib_dir_path_decision()

    @staticmethod
    def do_set_cuda_home():
        os.environ['CUDA_HOME'] = os.path.join('mycudahome')
        _fake_non_conda_env()
        return True, _get_cudalib_dir_path_decision()

    @staticmethod
    def do_set_cuda_lib():
        os.environ['NUMBAPRO_CUDALIB'] = 'nbp_cudalib'
        return True, _get_cudalib_dir_path_decision()


def _fake_non_conda_env():
    """
    Monkeypatch sys.prefix to hide the fact we are in a conda-env
    """
    sys.prefix = ''
