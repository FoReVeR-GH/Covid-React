#!/usr/bin/env python

import unittest
import subprocess

def check_output(*popenargs, **kwargs):
    # Provide this for backward-compatibility until we drop Python 2.6 support.
    process = subprocess.Popen(stdout=subprocess.PIPE, *popenargs, **kwargs)
    output, unused_err = process.communicate()
    retcode = process.poll()
    if retcode:
        cmd = kwargs.get("args")
        if cmd is None:
            cmd = popenargs[0]
        raise subprocess.CalledProcessError(retcode, cmd)
    return output


class TestCase(unittest.TestCase):
    """These test cases are meant to test the Numba test infrastructure itself.
    Therefore, the logic used here shouldn't use numba.testing, but only the upstream
    unittest, and run the numba test suite only in a subprocess."""


    def check_testsuite_size(self, args, minsize, maxsize=None):
        """
        Check that the reported numbers of tests are in the
        (minsize, maxsize) range, or are equal to minsize if maxsize is None.
        """

        cmd = ['python', '-m', 'numba.runtests', '-l'] + list(args)
        lines = check_output(cmd).decode().splitlines()
        lines = [line for line in lines if line.strip()]
        last_line = lines[-1]
        self.assertTrue(last_line.endswith('tests found'))
        number = int(last_line.split(' ')[0])
        # There may be some "skipped" messages at the beginning,
        # so do an approximate check.
        self.assertIn(len(lines), range(number + 1, number + 10))
        if maxsize is None:
            self.assertEqual(number, minsize)
        else:
            self.assertGreaterEqual(number, minsize)
            self.assertLessEqual(number, maxsize)
        return lines

    def check_all(self, ids):
        lines = self.check_testsuite_size(ids, 6000, 8000)
        # CUDA should be included by default
        self.assertTrue(any('numba.cuda.tests.' in line for line in lines))
        # As well as subpackage
        self.assertTrue(any('numba.tests.npyufunc.test_' in line for line in lines))

    def test_default(self):
        self.check_all([])

    def test_all(self):
        self.check_all(['numba.tests'])

    def test_cuda(self):
        # Even without CUDA enabled, there is at least one test
        # (in numba.cuda.tests.nocuda)
        self.check_testsuite_size(['numba.cuda.tests'], 1, 400)

    def test_module(self):
        self.check_testsuite_size(['numba.tests.test_utils'], 5, 15)
        self.check_testsuite_size(['numba.tests.test_nested_calls'], 5, 15)
        # Several modules
        self.check_testsuite_size(['numba.tests.test_nested_calls',
                                   'numba.tests.test_utils'], 15, 30)

    def test_subpackage(self):
        self.check_testsuite_size(['numba.tests.npyufunc'], 50, 200)

    def test_random(self):
        self.check_testsuite_size(['--random', '0.1', 'numba.tests.npyufunc'],
                                  5, 20)

    def test_tags(self):
        self.check_testsuite_size(['--tags', 'important', 'numba.tests.npyufunc'],
                                  20, 50)


if __name__ == '__main__':
    unittest.main()
