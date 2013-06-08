#!/usr/bin/env python
# -*- coding: UTF-8 -*-
from __future__ import print_function, division, absolute_import

import sys
import argparse
from os.path import dirname, splitext

sys.path.pop(0) # remove the bin directory so can import numba

from numba import environment

# ______________________________________________________________________

def run(filename, cmdopts):
    numba_env = environment.NumbaEnvironment.get_environment()
    numba_env.cmdopts = cmdopts
    modname, ext = splitext(dirname(filename))
    globals = { '__file__': '__main__', '__name__': modname }
    code = compile(open(filename).read(), filename, 'exec', dont_inherit=True)
    eval(code, globals)

# ______________________________________________________________________

def make_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--annotate',  help='Annotate source',
                        action='store_true')
    parser.add_argument('--dump-llvm', action="store_true",
                        help='Print generated llvm assembly')
    parser.add_argument('--dump-optimized', action='store_true',
                        help='Dump the optimized llvm assembly')
    parser.add_argument('--dump-cfg', action="store_true",
                        help='Dump the control flow graph')
    parser.add_argument('--time-compile', action="store_true",
                        help='Time the compilation process')
    parser.add_argument('filename', help='Python source filename')
    return parser

if __name__ == "__main__":
    parser = make_parser()
    args = parser.parse_args()
    cmdopts = {
        'dump-llvm': args.dump_llvm,
        'dump-optimized': args.dump_optimized,
        'dump_cfg': args.dump_cfg,
        'time_compile': args.time_compile,
    }
    run(args.filename, cmdopts)