from __future__ import print_function, division, absolute_import
from numba import utils
from numba.bytecode import ByteCodeInst, CustomByteCode


def bind(loops, typingctx, targetctx, locals, flags):
    """
    Install loop dispatchers into the module
    """
    disps = []
    for loopbc in loops:
        d = bind_loop(loopbc, typingctx, targetctx, locals, flags)
        disps.append(d)
    return disps


def bind_loop(loopbc, typingctx, targetctx, locals, flags):
    from numba.dispatcher import LiftedLoop
    fname = loopbc.func_name
    disp = getattr(loopbc.module, fname, None)
    if disp is not None:
        if not isinstance(disp, LiftedLoop):
            raise ValueError("Function %s exist but not a lifted-loop" % fname)
        # Short circuit
        return disp
    else:
        disp = LiftedLoop(loopbc, typingctx, targetctx, locals, flags)
        setattr(loopbc.module, fname, disp)
    return disp


def lift_loop(bytecode):
    """Lift the top-level loops.

    Returns (outer, loops)
    ------------------------
    * outer: ByteCode of a copy of the loop-less function.
    * loops: a list of ByteCode of the loops.
    """
    outer = []
    loops = []
    separate_loops(bytecode, outer, loops)

    # Discover variables references
    outer_rds, outer_wrs = find_varnames_uses(bytecode, outer)
    outer_wrs |= set(bytecode.argspec.args)

    lbclist = []
    outerlabels = set(bytecode.labels)
    outernames = list(bytecode.co_names)
    for loop in loops:
        args, rets = discover_args_and_returns(bytecode, loop, outer_rds,
                                               outer_wrs)
        if rets:
            # Cannot deal with loop that write to variables used in outer body
            # Put the loop back into the outer function
            outer = stitch_instructions(outer, loop)
            # Recompute read-write variable set
            wrs, rds = find_varnames_uses(bytecode, loop)
            outer_wrs |= wrs
            outer_rds |= rds
        else:
            insert_loop_call(bytecode, loop, args, lbclist, outer, outerlabels,
                             outernames)

    # Build outer bytecode
    codetable = utils.SortedMap((i.offset, i) for i in outer)
    outerbc = CustomByteCode(func=bytecode.func,
                             func_name=bytecode.func_name,
                             argspec=bytecode.argspec,
                             filename=bytecode.filename,
                             co_names=outernames,
                             co_varnames=bytecode.co_varnames,
                             co_consts=bytecode.co_consts,
                             table=codetable,
                             labels=outerlabels & set(codetable.keys()))
    return outerbc, lbclist


def insert_loop_call(bytecode, loop, args, lbclist, outer, outerlabels,
                     outernames):
    endloopoffset = loop[-1].next
    # Accepted. Create a bytecode object for the loop
    args = tuple(args)
    lbc = make_loop_bytecode(bytecode, loop, args)
    lbclist.append(lbc)


    # Insert jump to the end
    jmp = ByteCodeInst.get(loop[0].offset, 'JUMP_ABSOLUTE',
                           outer[-1].next)
    jmp.lineno = loop[0].lineno
    insert_instruction(outer, jmp)

    outerlabels.add(outer[-1].next)

    # Prepare arguments
    outernames.append(lbc.func_name)
    loadfn = ByteCodeInst.get(outer[-1].next, "LOAD_GLOBAL",
                              outernames.index(lbc.func_name))
    loadfn.lineno = loop[0].lineno
    insert_instruction(outer, loadfn)

    for arg in args:
        loadarg = ByteCodeInst.get(outer[-1].next, 'LOAD_FAST',
                                   bytecode.co_varnames.index(arg))
        loadarg.lineno = loop[0].lineno
        insert_instruction(outer, loadarg)

    # Call function
    assert len(args) < 256
    call = ByteCodeInst.get(outer[-1].next, "CALL_FUNCTION", len(args))
    call.lineno = loop[0].lineno
    insert_instruction(outer, call)

    poptop = ByteCodeInst.get(outer[-1].next, "POP_TOP", None)
    poptop.lineno = loop[0].lineno
    insert_instruction(outer, poptop)

    jmpback = ByteCodeInst.get(outer[-1].next, 'JUMP_ABSOLUTE',
                               endloopoffset)

    jmpback.lineno = loop[0].lineno
    insert_instruction(outer, jmpback)


def insert_instruction(insts, item):
    i = find_previous_inst(insts, item.offset)
    insts.insert(i, item)


def find_previous_inst(insts, offset):
    for i, inst in enumerate(insts):
        if inst.offset > offset:
            return i
    return len(insts)


def make_loop_bytecode(bytecode, loop, args):
    # Add return None
    co_consts = tuple(bytecode.co_consts)
    if None not in co_consts:
        co_consts += (None,)

    # Load None
    load_none = ByteCodeInst.get(loop[-1].next, "LOAD_CONST",
                                 co_consts.index(None))
    load_none.lineno = loop[-1].lineno
    loop.append(load_none)

    # Return None
    return_value = ByteCodeInst.get(loop[-1].next, "RETURN_VALUE", 0)
    return_value.lineno = loop[-1].lineno
    loop.append(return_value)

    # Function name
    loopfuncname = bytecode.func_name+"__numba__loop%d__" % loop[0].offset

    # Argspec
    argspectype = type(bytecode.argspec)
    argspec = argspectype(args=args, varargs=(), keywords=(), defaults=())

    # Code table
    codetable = utils.SortedMap((i.offset, i) for i in loop)

    # Custom bytecode object
    lbc = CustomByteCode(func=bytecode.func,
                         func_name=loopfuncname,
                         argspec=argspec,
                         filename=bytecode.filename,
                         co_names=bytecode.co_names,
                         co_varnames=bytecode.co_varnames,
                         co_consts=co_consts,
                         table=codetable,
                         labels=bytecode.labels)

    return lbc


def stitch_instructions(outer, loop):
    begin = loop[0].offset
    i = find_previous_inst(outer, begin)
    return outer[:i] + loop + outer[i:]


def discover_args_and_returns(bytecode, insts, outer_rds, outer_wrs):
    """
    Basic analysis for args and returns
    This completely ignores the ordering or the read-writes.
    """
    rdnames, wrnames = find_varnames_uses(bytecode, insts)
    # Pass names that are written outside and read locally
    args = outer_wrs & rdnames
    # Return values that it written locally and read outside
    rets = wrnames & outer_rds
    return args, rets


def find_varnames_uses(bytecode, insts):
    rdnames = set()
    wrnames = set()
    for inst in insts:
        if inst.opname == 'LOAD_FAST':
            rdnames.add(bytecode.co_varnames[inst.arg])
        elif inst.opname == 'STORE_FAST':
            wrnames.add(bytecode.co_varnames[inst.arg])
    return rdnames, wrnames


def separate_loops(bytecode, outer, loops):
    """
    Separate top-level loops from the function

    Stores loopless instructions from the original function into `outer`.
    Stores list of loop instructions into `loops`.
    Both `outer` and `loops` are list-like (`append(item)` defined).
    """
    endloop = None
    cur = None
    for inst in bytecode:
        if endloop is None:
            if inst.opname == 'SETUP_LOOP':
                cur = [inst]
                endloop = inst.next + inst.arg
            else:
                outer.append(inst)
        else:
            cur.append(inst)
            if inst.next == endloop:
                loops.append(cur)
                endloop = None