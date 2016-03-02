from __future__ import print_function, absolute_import

from collections import Mapping, defaultdict, OrderedDict
from contextlib import closing
import inspect
import os
import re
import sys
import textwrap

from numba.io_support import StringIO
from numba import ir
import numba.dispatcher


class SourceLines(Mapping):
    def __init__(self, func):

        try:
            lines, startno = inspect.getsourcelines(func)
        except IOError:
            self.lines = ()
            self.startno = 0
        else:
            self.lines = textwrap.dedent(''.join(lines)).splitlines()
            self.startno = startno

    def __getitem__(self, lineno):
        try:
            return self.lines[lineno - self.startno].rstrip()
        except IndexError:
            return ''

    def __iter__(self):
        return iter((self.startno + i) for i in range(len(self.lines)))

    def __len__(self):
        return len(self.lines)

    @property
    def avail(self):
        return bool(self.lines)


class TypeAnnotation(object):

    # func_data dict stores annotation data for all functions that are
    # compiled. We store the data in the TypeAnnotation class since a new
    # TypeAnnotation instance is created for each function that is compiled.
    # For every function that is compiled, we add the type annotation data to
    # this dict and write the html annotation file to disk (rewrite the html
    # file for every function since we don't know if this is the last function
    # to be compiled).
    func_data = OrderedDict()

    def __init__(self, interp, typemap, calltypes, lifted, lifted_from, args, return_type,
                 func_attr, html_output=None):
        self.filename = interp.bytecode.filename
        self.func = interp.bytecode.func
        self.blocks = interp.blocks
        self.typemap = typemap
        self.calltypes = calltypes
        if html_output is None:
            self.html_output = None
        else:
            self.html_output = os.path.join(os.getcwd(), html_output)
        self.filename = interp.loc.filename
        self.linenum = str(interp.loc.line)
        self.signature = str(args) + ' -> ' + str(return_type)
        self.func_attr = func_attr

        # lifted loop information
        self.lifted = lifted
        self.num_lifted_loops = len(lifted)

        # If this is a lifted loop function that is being compiled, lifted_from
        # points to annotation data from function that this loop lifted function
        # was lifted from. This is used to stick lifted loop annotations back
        # into original function.
        self.lifted_from = lifted_from

    def prepare_annotations(self):
        # Prepare annotations
        groupedinst = defaultdict(list)
        found_lifted_loop = False
        #for blkid, blk in self.blocks.items():
        for blkid in sorted(self.blocks.keys()):
            blk = self.blocks[blkid]
            groupedinst[blk.loc.line].append("label %s" % blkid)
            for inst in blk.body:
                lineno = inst.loc.line

                if isinstance(inst, ir.Assign):
                    if found_lifted_loop:
                        atype = 'XXX Lifted Loop XXX'
                        found_lifted_loop = False
                    elif (isinstance(inst.value, ir.Expr) and
                            inst.value.op ==  'call'):
                        atype = self.calltypes[inst.value]
                    elif (isinstance(inst.value, ir.Const) and
                            isinstance(inst.value.value, numba.dispatcher.LiftedLoop)):
                        atype = 'XXX Lifted Loop XXX'
                        found_lifted_loop = True
                    else:
                        atype = self.typemap[inst.target.name]

                    aline = "%s = %s  :: %s" % (inst.target, inst.value, atype)
                elif isinstance(inst, ir.SetItem):
                    atype = self.calltypes[inst]
                    aline = "%s  :: %s" % (inst, atype)
                else:
                    aline = "%s" % inst
                groupedinst[lineno].append("  %s" % aline)
        return groupedinst

    def annotate(self):
        source = SourceLines(self.func)
        # if not source.avail:
        #     return "Source code unavailable"

        groupedinst = self.prepare_annotations()

        # Format annotations
        io = StringIO()
        with closing(io):
            if source.avail:
                print("# File: %s" % self.filename, file=io)
                for num in source:
                    srcline = source[num]
                    ind = _getindent(srcline)
                    print("%s# --- LINE %d --- " % (ind, num), file=io)
                    for inst in groupedinst[num]:
                        print('%s# %s' % (ind, inst), file=io)
                    print(file=io)
                    print(srcline, file=io)
                    print(file=io)
                if self.lifted:
                    print("# The function contains lifted loops", file=io)
                    for loop in self.lifted:
                        print("# Loop at line %d" % loop.bytecode.firstlineno,
                              file=io)
                        print("# Has %d overloads" % len(loop.overloads),
                              file=io)
                        for cres in loop.overloads.values():
                            print(cres.type_annotation, file=io)
            else:
                print("# Source code unavailable", file=io)
                for num in groupedinst:
                    for inst in groupedinst[num]:
                        print('%s' % (inst,), file=io)
                    print(file=io)

            return io.getvalue()

    def html_annotate(self, outfile=None):
        python_source = SourceLines(self.func)
        ir_lines = self.prepare_annotations()
        line_nums = [num for num in python_source]
        lifted_lines = [l.bytecode.firstlineno for l in self.lifted]

        def add_ir_line(func_data, line):
            line_str = line.strip()
            line_type = ''
            if line_str.endswith('pyobject'):
                line_str = line_str.replace('pyobject', '')
                line_type = 'pyobject'
            func_data['ir_lines'][num].append((line_str, line_type))
            indent_len = len(_getindent(line))
            func_data['ir_indent'][num].append('&nbsp;' * indent_len)

        func_key = (self.func_attr.filename + ':' + str(self.func_attr.lineno + 1),
                    self.signature)
        if self.lifted_from is not None and self.lifted_from[1]['num_lifted_loops'] > 0:
            # This is a lifted loop function that is being compiled. Get the
            # numba ir for lines in loop function to use for annotating
            # original python function that the loop was lifted from.
            func_data = self.lifted_from[1]
            for num in line_nums:
                if num not in ir_lines.keys():
                    continue
                func_data['ir_lines'][num] = []
                func_data['ir_indent'][num] = []
                for line in ir_lines[num]:
                    add_ir_line(func_data, line)
                    if line.strip().endswith('pyobject'):
                        func_data['python_tags'][num] = 'object_tag'
                        # If any pyobject line is found, make sure original python
                        # line that was marked as a lifted loop start line is tagged
                        # as an object line instead. Lifted loop start lines should
                        # only be marked as lifted loop lines if the lifted loop
                        # was successfully compiled in nopython mode.
                        func_data['python_tags'][self.lifted_from[0]] = 'object_tag'

            # We're done with this lifted loop, so decrement lfited loop counter.
            # When lifted loop counter hits zero, that means we're ready to write
            # out annotations to html file.
            self.lifted_from[1]['num_lifted_loops'] -= 1

        elif func_key not in TypeAnnotation.func_data.keys():
            TypeAnnotation.func_data[func_key] = {}
            func_data = TypeAnnotation.func_data[func_key]

            for i, loop in enumerate(self.lifted):
                # Make sure that when we process each lifted loop function later,
                # we'll know where it originally came from.
                loop.lifted_from = (lifted_lines[i], func_data)
            func_data['num_lifted_loops'] = self.num_lifted_loops

            func_data['filename'] = self.filename
            func_data['funcname'] = self.func_attr.name
            func_data['python_lines'] = []
            func_data['python_indent'] = {}
            func_data['python_tags'] = {}
            func_data['ir_lines'] = {}
            func_data['ir_indent'] = {}

            for num in line_nums:
                func_data['python_lines'].append((num, python_source[num].strip()))
                indent_len = len(_getindent(python_source[num]))
                func_data['python_indent'][num] = '&nbsp;' * indent_len
                func_data['python_tags'][num] = ''
                func_data['ir_lines'][num] = []
                func_data['ir_indent'][num] = []

                for line in ir_lines[num]:
                    add_ir_line(func_data, line)
                    if num in lifted_lines:
                        func_data['python_tags'][num] = 'lifted_tag'
                    elif line.strip().endswith('pyobject'):
                        func_data['python_tags'][num] = 'object_tag'

        # If there are no lifted loops to compile, or if there are lifted loops
        # to compiled and they've all been compiled, then write annotations
        # for current function.
        if ((len(self.lifted) == 0 and self.lifted_from is None) or
                (self.lifted_from is not None and
                 self.lifted_from[1]['num_lifted_loops'] == 0)):

            # If jinja2 module is not installed we should never get here,
            # but just in case...
            try:
                from jinja2 import Template
            except ImportError:
                raise ImportError("please install the 'jinja2' package")

            root = os.path.join(os.path.dirname(__file__))
            template_filename = os.path.join(root, 'template.html')
            with open(template_filename, 'r') as template:
                html = template.read()

            template = Template(html)
            rendered = template.render(func_data=TypeAnnotation.func_data)
            if outfile is None:
                with open(self.html_output, 'w') as output:
                    output.write(rendered)
            else:
                outfile.write(rendered)

    def __str__(self):
        return self.annotate()


re_longest_white_prefix = re.compile('^\s*')


def _getindent(text):
    m = re_longest_white_prefix.match(text)
    if not m:
        return ''
    else:
        return ' ' * len(m.group(0))
