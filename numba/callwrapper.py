from llvm.core import Type, Builder, Constant
import llvm.core as lc
from numba import types, cgutils


class PyCallWrapper(object):
    def __init__(self, context, module, func, fndesc):
        self.context = context
        self.module = module
        self.func = func
        self.fndesc = fndesc

    def build(self):
        wrapname = "wrapper.%s" % self.func.name

        pyobj = self.context.get_argument_type(types.pyobject)
        fnty = Type.function(pyobj, [pyobj, pyobj, pyobj])
        wrapper = self.module.add_function(fnty, name=wrapname)

        builder = Builder.new(wrapper.append_basic_block('entry'))

        self.build_wrapper(builder, wrapper.args[1], wrapper.args[2])

        wrapper.verify()
        return wrapper

    def build_wrapper(self, builder, args, kws):
        api = self.context.get_python_api(builder)
        nargs = len(self.fndesc.args)
        keywords = self.make_keywords(self.fndesc.args)
        argfmt = "O" * nargs
        fmt = self.make_const_string(argfmt)

        objs = [api.alloca_obj() for _ in range(nargs)]
        parseok = api.parse_tuple_and_keywords(args, kws, fmt, keywords, *objs)

        pred = builder.icmp(lc.ICMP_EQ, parseok, Constant.null(parseok.type))
        with cgutils.ifthen(builder, pred):
            builder.ret(api.get_null_object())

        innerargs = [api.to_native_arg(builder.load(obj), ty)
                     for obj, ty in zip(objs, self.fndesc.argtypes)]

        res = builder.call(self.func, innerargs)

        retval = api.from_native_return(res, self.fndesc.restype)
        builder.ret(retval)


    def make_const_string(self, string):
        stringtype = Type.pointer(Type.int(8))
        text = Constant.stringz(string)
        name = "const.%s" % string
        gv = self.module.add_global_variable(text.type, name=name)
        gv.global_constant = True
        gv.initializer = text
        gv.linkage = lc.LINKAGE_INTERNAL
        return Constant.bitcast(gv, stringtype)

    def make_keywords(self, kws):
        strings = []
        stringtype = Type.pointer(Type.int(8))
        for k in kws:
            strings.append(self.make_const_string(k))

        strings.append(Constant.null(stringtype))

        kwlist = Constant.array(stringtype, strings)

        gv = self.module.add_global_variable(kwlist.type, name="kwlist")
        gv.global_constant = True
        gv.initializer = kwlist
        gv.linkage = lc.LINKAGE_INTERNAL

        return Constant.bitcast(gv, Type.pointer(stringtype))

