"""
Tests the parallel backend
"""
import threading
import multiprocessing
import random
import os
import sys
import subprocess
import signal

import numpy as np

from numba import config, utils
from numba import unittest_support as unittest
from numba import jit, vectorize, guvectorize, njit

from .support import temp_directory, override_config, TestCase

# Check which backends are available
# TODO: Put this in a subprocess so the address space is kept clean
try:
    from numba.npyufunc import tbbpool
    _HAVE_TBB_POOL = True
except ImportError:
    _HAVE_TBB_POOL = False

try:
    from numba.npyufunc import omppool
    _HAVE_OMP_POOL = True
except ImportError:
    _HAVE_OMP_POOL = False


# Switch this to True to run fork() based tests, unsupported at present
_DO_FORK_TESTS = True 

skip_no_omp = unittest.skipUnless(_HAVE_OMP_POOL, "OpenMP threadpool required")
skip_no_tbb = unittest.skipUnless(_HAVE_TBB_POOL, "TBB threadpool required")

_gnuomp = _HAVE_OMP_POOL and omppool.openmp_vendor == "GNU"
skip_unless_gnu_omp = unittest.skipUnless(_gnuomp, "GNU OpenMP only tests")

skip_unless_py3 = unittest.skipUnless(utils.PYVERSION >= (3, 0),
                                      "Test runs on Python 3 only")

# some functions to jit

def foo(n, v):
    return np.ones(n) + v


def linalg(n, v):
    np.random.seed(42)
    return np.linalg.cond(np.random.random((10, 10))) + np.ones(n) + v


def ufunc_foo(a, b):
    return a + b


def gufunc_foo(a, b, out):
    out[0] = a + b


class runnable(object):
    def __init__(self, **options):
        self._options = options


class jit_runner(runnable):

    def __call__(self):
        cfunc = jit(**self._options)(foo)
        a = 4
        b = 10
        expected = foo(a, b)
        got = cfunc(a, b)
        np.testing.assert_allclose(expected, got)


class linalg_runner(runnable):

    def __call__(self):
        cfunc = jit(**self._options)(linalg)
        a = 4
        b = 10
        expected = linalg(a, b)
        got = cfunc(a, b)
        # broken, fork safe?
        # np.testing.assert_allclose(expected, got)


class vectorize_runner(runnable):

    def __call__(self):
        cfunc = vectorize(['(f4, f4)'], **self._options)(ufunc_foo)
        a = b = np.random.random(10).astype(np.float32)
        expected = ufunc_foo(a, b)
        got = cfunc(a, b)
        np.testing.assert_allclose(expected, got)


class guvectorize_runner(runnable):

    def __call__(self):
        sig = ['(f4, f4, f4[:])']
        cfunc = guvectorize(sig, '(),()->()', **self._options)(gufunc_foo)
        a = b = np.random.random(10).astype(np.float32)
        expected = ufunc_foo(a, b)
        got = cfunc(a, b)
        np.testing.assert_allclose(expected, got)

def chooser(fnlist):
    for _ in range(10):
        fn = random.choice(fnlist)
        fn()


def compile_factory(parallel_class):
    def run_compile(fnlist):
        ths = [parallel_class(target=chooser, args=(fnlist,))
               for i in range(4)]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
    return run_compile


# workers
_thread_class = threading.Thread


class _proc_class_impl(object):

    def __init__(self, method):
        self._method = method

    def __call__(self, *args, **kwargs):
        if utils.PYVERSION < (3, 0):
            return multiprocessing.Process(*args, **kwargs)
        else:
            ctx = multiprocessing.get_context(self._method)
            return ctx.Process(*args, **kwargs)


thread_impl = compile_factory(_thread_class)
spawn_proc_impl = compile_factory(_proc_class_impl('spawn'))
fork_proc_impl = compile_factory(_proc_class_impl('fork'))

# this is duplication as Py27, linux uses fork, windows uses spawn, it however
# is kept like this so that when tests fail it's less confusing!
default_proc_impl = compile_factory(_proc_class_impl('default'))


class TestParallelBackendBase(TestCase):

    all_impls = [jit_runner(nopython=True),
                 jit_runner(nopython=True, cache=True),
                 jit_runner(nopython=True, nogil=True),
                 jit_runner(nopython=True, parallel=True),
                 linalg_runner(nopython=True),
                 linalg_runner(nopython=True, nogil=True),
                 linalg_runner(nopython=True, parallel=True),
                 vectorize_runner(nopython=True),
                 vectorize_runner(nopython=True, target='parallel'),
                 guvectorize_runner(nopython=True),
                 guvectorize_runner(nopython=True, target='parallel'),
                 ]
    parallelism = ['threading', 'random']
    if utils.PYVERSION > (3, 0):
        if _DO_FORK_TESTS and (not sys.platform.startswith('win')):
            parallelism.append('multiprocessing_fork')


    runners = {'concurrent_jit': [jit_runner(nopython=True, parallel=True)],
               'concurrect_vectorize': [vectorize_runner(nopython=True, target='parallel')],
               'concurrent_guvectorize': [guvectorize_runner(nopython=True, target='parallel')],
               'concurrent_mix_use': all_impls}

    safe_backends = {'omppool', 'tbbpool'}

    def run_compile(self, fnlist, parallelism='threading'):
        self._cache_dir = temp_directory(self.__class__.__name__)
        with override_config('CACHE_DIR', self._cache_dir):
            if parallelism == 'threading':
                thread_impl(fnlist)
            elif parallelism == 'multiprocessing_fork':
                fork_proc_impl(fnlist)
            elif parallelism == 'multiprocessing_spawn':
                spawn_proc_impl(fnlist)
            elif parallelism == 'multiprocessing_default':
                default_proc_impl(fnlist)
            elif parallelism == 'random':
                if utils.PYVERSION < (3, 0):
                    ps = [thread_impl]
                    if _DO_FORK_TESTS: # Py2.7 linux can only fork() so disable for all
                        ps.append(default_proc_impl)
                else:
                    ps = [thread_impl, spawn_proc_impl]
                    if _DO_FORK_TESTS:
                        ps.append(fork_proc_impl)

                for _ in range(10):  # 10 is arbitrary
                    impl = random.choice(ps)
                    impl(fnlist)
            else:
                raise ValueError(
                    'Unknown parallelism supplied %s' % parallelism)


_threadsafe_backends = config.THREADING_LAYER in ('omppool', 'tbbpool')


@unittest.skipUnless(_threadsafe_backends, "Threading layer not threadsafe")
class TestParallelBackend(TestParallelBackendBase):
    """ These are like the numba.tests.test_threadsafety tests but designed
    instead to torture the parallel backend.
    If a suitable backend is supplied via NUMBA_THREADING_LAYER these tests
    can be run directly.
    """

    @classmethod
    def generate(cls):
        for p in cls.parallelism:
            for name, impl in cls.runners.items():
                def test_method(self):
                    self.run_compile(impl, parallelism=p)
                methname = "test_" + p + '_' + name
                setattr(cls, methname, test_method)


TestParallelBackend.generate()


class TestSpecificBackend(TestParallelBackendBase):
    """
    This is quite contrived, for each test in the TestParallelBackend tests it
    generates a test that will run the TestParallelBackend test in a new python
    process with an environment modified to ensure a specific threadsafe backend
    is used. This is with view of testing the backends independently and in an
    isolated manner such that if they hang/crash/have issues, it doesn't kill
    the test suite.
    """

    backends = {'tbbpool': skip_no_tbb,
                'omppool': skip_no_omp}

    def run_cmd(self, cmdline, env):
        popen = subprocess.Popen(cmdline,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 env=env)
        # finish in 5 minutes or kill it
        timeout = threading.Timer(5 * 60., popen.kill)
        try:
            timeout.start()
            out, err = popen.communicate()
            if popen.returncode != 0:
                raise AssertionError("process failed with code %s: stderr follows\n%s\n"
                                     % (popen.returncode, err.decode()))
        finally:
            timeout.cancel()
        return out.decode(), err.decode()

    def run_test_in_separate_process(self, test, threading_layer):
        env_copy = os.environ.copy()
        env_copy['NUMBA_THREADING_LAYER'] = str(threading_layer)
        print("Running %s with backend: %s" % (test, threading_layer))
        cmdline = [sys.executable, "-m", "numba.runtests", test]
        return self.run_cmd(cmdline, env_copy)

    @classmethod
    def _inject(cls, p, name, backend, backend_guard):
        themod = cls.__module__
        thecls = TestParallelBackend.__name__
        methname = "test_" + p + '_' + name
        injected_method = '%s.%s.%s' % (themod, thecls, methname)

        def test_template(self):
            self.run_test_in_separate_process(injected_method, backend)
        injected_test = "test_%s_%s_%s" % (p, name, backend)
        setattr(cls, injected_test, backend_guard(test_template))

    @classmethod
    def generate(cls):
        for backend, backend_guard in cls.backends.items():
            for p in cls.parallelism:
                for name in cls.runners.keys():
                    cls._inject(p, name, backend, backend_guard)


TestSpecificBackend.generate()


@skip_unless_gnu_omp
class TestForkSafetyIssues(TestCase):

    # sys path injection and separate usecase module to make sure everything
    # is importable by children of multiprocessing
    _here = os.path.dirname(__file__)

    template = """if 1:
    import sys
    sys.path.insert(0, "%s")
    import multiprocessing
    import numpy as np
    from numba import njit
    import threading_backend_usecases
    import os

    sigterm_handler = threading_backend_usecases.sigterm_handler
    busy_func = threading_backend_usecases.busy_func

    def the_test():
        %%s

    if __name__ == "__main__":
        the_test()
    """ % _here

    def run_cmd(self, cmdline, env=None):
        if env is None:
            env = os.environ.copy()
            env['NUMBA_THREADING_LAYER'] = str("omppool")
        popen = subprocess.Popen(cmdline,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 env=env)
        # finish in 5 minutes or kill it
        timeout = threading.Timer(5 * 60., popen.kill)
        try:
            timeout.start()
            out, err = popen.communicate()
            if popen.returncode != 0:
                raise AssertionError("process failed with code %s: stderr follows\n%s\n"
                                     % (popen.returncode, err.decode()))
        finally:
            timeout.cancel()
        return out.decode(), err.decode()

    def test_check_threading_layer_is_gnu(self):
        runme = """if 1:
            from numba.npyufunc import omppool
            assert omppool.openmp_vendor == 'GNU'
            """
        cmdline = [sys.executable, '-c', runme]
        out, err = self.run_cmd(cmdline)

    def test_par_parent_os_fork_par_child(self):
        """
        Whilst normally valid, this actually isn't for Numba invariant of OpenMP
        Checks SIGABRT is received.
        """
        body = """if 1:
            X = np.arange(1000000.)
            Y = np.arange(1000000.)
            Z = busy_func(X, Y)
            pid = os.fork()
            if pid  == 0:
                Z = busy_func(X, Y)
            else:
                os.wait()
        """
        runme = self.template % body
        cmdline = [sys.executable, '-c', runme]
        try:
            out, err = self.run_cmd(cmdline)
        except AssertionError as e:
            self.assertIn("failed with code -6", str(e))

    def test_par_parent_implicit_mp_fork_par_child(self):
        """
        Implicit use of multiprocessing fork context.
        Does this:
        1. Start with OpenMP
        2. Fork to processes using OpenMP (this is invalid)
        3. Joins fork
        4. Check the exception pushed onto the queue that is a result of
           catching SIGTERM coming from the C++ aborting on illegal fork
           pattern for GNU OpenMP
        """
        body = """if 1:
            X = np.arange(1000000.)
            Y = np.arange(1000000.)
            q = multiprocessing.Queue()

            # Start OpenMP runtime on parent via parallel function
            Z = busy_func(X, Y, q)

            # fork() underneath with no exec, will abort
            proc = multiprocessing.Process(target = busy_func, args=(X, Y, q))
            proc.start()

            err = q.get()
            assert "Caught SIGTERM" in str(err)
        """
        runme = self.template % body
        cmdline = [sys.executable, '-c', runme]
        out, err = self.run_cmd(cmdline)

    @skip_unless_py3
    def test_par_parent_explicit_mp_fork_par_child(self):
        """
        Explicit use of multiprocessing fork context.
        Does this:
        1. Start with OpenMP
        2. Fork to processes using OpenMP (this is invalid)
        3. Joins fork
        4. Check the exception pushed onto the queue that is a result of
           catching SIGTERM coming from the C++ aborting on illegal fork
           pattern for GNU OpenMP
        """
        body = """if 1:
            X = np.arange(1000000.)
            Y = np.arange(1000000.)
            q = multiprocessing.Queue()

            # Start OpenMP runtime on parent via parallel function
            Z = busy_func(X, Y, q)

            # fork() underneath with no exec, will abort
            ctx = multiprocessing.get_context('fork')
            proc = ctx.Process(target = busy_func, args=(X, Y, q))
            proc.start()

            err = q.get()
            assert "Caught SIGTERM" in str(err)
        """
        runme = self.template % body
        cmdline = [sys.executable, '-c', runme]
        out, err = self.run_cmd(cmdline)

    @skip_unless_py3
    def test_par_parent_mp_spawn_par_child_par_parent(self):
        """
        Explicit use of multiprocessing spawn, this is safe.
        Does this:
        1. Start with OpenMP
        2. Spawn to processes using OpenMP
        3. Join spawns
        4. Run some more OpenMP
        """
        body = """if 1:
            X = np.arange(1000000.)
            Y = np.arange(1000000.)
            q = multiprocessing.Queue()

            # Start OpenMP runtime and run on parent via parallel function
            Z = busy_func(X, Y, q)
            procs = []
            ctx = multiprocessing.get_context('spawn')
            for x in range(20): # start a lot to try and get overlap
                ## fork() + exec() to run some OpenMP on children
                proc = ctx.Process(target = busy_func, args=(X, Y, q))
                procs.append(proc)
                sys.stdout.flush()
                sys.stderr.flush()
                proc.start()

            [p.join() for p in procs]

            try:
                q.get(False)
            except multiprocessing.queues.Empty:
                pass
            else:
                raise RuntimeError("Queue was not empty")

            # Run some more OpenMP on parent
            Z = busy_func(X, Y, q)
        """
        runme = self.template % body
        cmdline = [sys.executable, '-c', runme]
        out, err = self.run_cmd(cmdline)
        print(out, err)

    def test_serial_parent_implicit_mp_fork_par_child_then_par_parent(self):
        """
        Implicit use of multiprocessing (will be fork, but cannot declare that
        in Py2.7 as there's no process launch context).
        Does this:
        1. Start with no OpenMP
        2. Fork to processes using OpenMP
        3. Join forks
        4. Run some OpenMP
        """
        body = """if 1:
            X = np.arange(1000000.)
            Y = np.arange(1000000.)
            q = multiprocessing.Queue()

            # this is ok
            procs = []
            for x in range(10):
                # fork() underneath with but no OpenMP in parent, this is ok
                proc = multiprocessing.Process(target = busy_func,
                                               args=(X, Y, q))
                procs.append(proc)
                proc.start()

            [p.join() for p in procs]

            # and this is still ok as the OpenMP happened in forks
            Z = busy_func(X, Y, q)
            try:
                q.get(False)
            except multiprocessing.queues.Empty:
                pass
            else:
                raise RuntimeError("Queue was not empty")
        """
        runme = self.template % body
        cmdline = [sys.executable, '-c', runme]
        out, err = self.run_cmd(cmdline)

    @skip_unless_py3
    def test_serial_parent_explicit_mp_fork_par_child_then_par_parent(self):
        """
        Explicit use of multiprocessing 'fork'.
        Does this:
        1. Start with no OpenMP
        2. Fork to processes using OpenMP
        3. Join forks
        4. Run some OpenMP
        """
        body = """if 1:
            X = np.arange(1000000.)
            Y = np.arange(1000000.)
            q = multiprocessing.Queue()

            # this is ok
            procs = []
            ctx = multiprocessing.get_context('fork')
            for x in range(10):
                # fork() underneath with but no OpenMP in parent, this is ok
                proc = ctx.Process(target = busy_func, args=(X, Y, q))
                procs.append(proc)
                proc.start()

            [p.join() for p in procs]

            # and this is still ok as the OpenMP happened in forks
            Z = busy_func(X, Y, q)
            try:
                q.get(False)
            except multiprocessing.queues.Empty:
                pass
            else:
                raise RuntimeError("Queue was not empty")
        """
        runme = self.template % body
        cmdline = [sys.executable, '-c', runme]
        out, err = self.run_cmd(cmdline)


if __name__ == '__main__':
    unittest.main()
