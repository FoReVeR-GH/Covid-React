"""
Implement transformation on Numba IR
"""

from __future__ import absolute_import, print_function

from collections import namedtuple

from numba.analysis import compute_cfg_from_blocks, find_top_level_loops
from numba import ir, errors
from numba.analysis import compute_use_defs


class BaseContextManager(object):
    pass


class ByPassContext(BaseContextManager):
    def mutate_with_body(self, func_ir, blk_start, blk_end):
        _bypass_with_context(func_ir, blk_start, blk_end)


ByPassContext = ByPassContext()


def _extract_loop_lifting_candidates(cfg, blocks):
    """
    Returns a list of loops that are candidate for loop lifting
    """
    # check well-formed-ness of the loop
    def same_exit_point(loop):
        "all exits must point to the same location"
        outedges = set()
        for k in loop.exits:
            succs = set(x for x, _ in cfg.successors(k))
            if not succs:
                # If the exit point has no successor, it contains an return
                # statement, which is not handled by the looplifting code.
                # Thus, this loop is not a candidate.
                return False
            outedges |= succs
        return len(outedges) == 1

    def one_entry(loop):
        "there is one entry"
        return len(loop.entries) == 1

    def cannot_yield(loop):
        "cannot have yield inside the loop"
        insiders = set(loop.body) | set(loop.entries) | set(loop.exits)
        for blk in map(blocks.__getitem__, insiders):
            for inst in blk.body:
                if isinstance(inst, ir.Assign):
                    if isinstance(inst.value, ir.Yield):
                        return False
        return True

    return [loop for loop in find_top_level_loops(cfg)
            if same_exit_point(loop) and one_entry(loop) and cannot_yield(loop)]


_loop_lift_info = namedtuple('loop_lift_info',
                             'loop,inputs,outputs,callfrom,returnto')


def _loop_lift_get_candidate_infos(cfg, blocks, livemap):
    """
    Returns information on looplifting candidates.
    """
    loops = _extract_loop_lifting_candidates(cfg, blocks)
    loopinfos = []
    for loop in loops:
        [callfrom] = loop.entries   # requirement checked earlier
        an_exit = next(iter(loop.exits))  # anyone of the exit block
        [(returnto, _)] = cfg.successors(an_exit)  # requirement checked earlier
        inputs = livemap[callfrom]
        outputs = livemap[returnto]

        # ensure live variables are actually used in the blocks, else remove,
        # saves having to create something valid to run through postproc
        # to achieve similar
        local_block_ids = set(loop.body) | set(loop.entries) | set(loop.exits)
        loopblocks = {}
        for k in local_block_ids:
            loopblocks[k] = blocks[k]

        used_vars = set()
        for vs in compute_use_defs(loopblocks).usemap.values():
            used_vars |= vs

        # note: sorted for stable ordering
        inputs = sorted(set(inputs) & used_vars)
        outputs = sorted(set(outputs) & used_vars)

        lli = _loop_lift_info(loop=loop, inputs=inputs, outputs=outputs,
                              callfrom=callfrom, returnto=returnto)
        loopinfos.append(lli)

    return loopinfos


def _loop_lift_modify_call_block(liftedloop, block, inputs, outputs, returnto):
    """
    Transform calling block from top-level function to call the lifted loop.
    """
    scope = block.scope
    loc = block.loc
    blk = ir.Block(scope=scope, loc=loc)
    # load loop
    fn = ir.Const(value=liftedloop, loc=loc)
    fnvar = scope.make_temp(loc=loc)
    blk.append(ir.Assign(target=fnvar, value=fn, loc=loc))
    # call loop
    args = [scope.get_exact(name) for name in inputs]
    callexpr = ir.Expr.call(func=fnvar, args=args, kws=(), loc=loc)
    # temp variable for the return value
    callres = scope.make_temp(loc=loc)
    blk.append(ir.Assign(target=callres, value=callexpr, loc=loc))
    # unpack return value
    for i, out in enumerate(outputs):
        target = scope.get_exact(out)
        getitem = ir.Expr.static_getitem(value=callres, index=i,
                                         index_var=None, loc=loc)
        blk.append(ir.Assign(target=target, value=getitem, loc=loc))
    # jump to next block
    blk.append(ir.Jump(target=returnto, loc=loc))
    return blk


def _loop_lift_prepare_loop_func(loopinfo, blocks):
    """
    Inplace transform loop blocks for use as lifted loop.
    """
    def make_prologue():
        """
        Make a new block that unwraps the argument and jump to the loop entry.
        This block is the entry block of the function.
        """
        entry_block = blocks[loopinfo.callfrom]
        scope = entry_block.scope
        loc = entry_block.loc

        block = ir.Block(scope=scope, loc=loc)
        # load args
        args = [ir.Arg(name=k, index=i, loc=loc)
                for i, k in enumerate(loopinfo.inputs)]
        for aname, aval in zip(loopinfo.inputs, args):
            tmp = ir.Var(scope=scope, name=aname, loc=loc)
            block.append(ir.Assign(target=tmp, value=aval, loc=loc))
        # jump to loop entry
        block.append(ir.Jump(target=loopinfo.callfrom, loc=loc))
        return block

    def make_epilogue():
        """
        Make a new block to prepare the return values.
        This block is the last block of the function.
        """
        entry_block = blocks[loopinfo.callfrom]
        scope = entry_block.scope
        loc = entry_block.loc

        block = ir.Block(scope=scope, loc=loc)
        # prepare tuples to return
        vals = [scope.get_exact(name=name) for name in loopinfo.outputs]
        tupexpr = ir.Expr.build_tuple(items=vals, loc=loc)
        tup = scope.make_temp(loc=loc)
        block.append(ir.Assign(target=tup, value=tupexpr, loc=loc))
        # return
        block.append(ir.Return(value=tup, loc=loc))
        return block

    # Lowering assumes the first block to be the one with the smallest offset
    firstblk = min(blocks) - 1
    blocks[firstblk] = make_prologue()
    blocks[loopinfo.returnto] = make_epilogue()


def _loop_lift_modify_blocks(func_ir, loopinfo, blocks,
                             typingctx, targetctx, flags, locals):
    """
    Modify the block inplace to call to the lifted-loop.
    Returns a dictionary of blocks of the lifted-loop.
    """
    from numba.dispatcher import LiftedLoop

    # Copy loop blocks
    loop = loopinfo.loop
    loopblockkeys = set(loop.body) | set(loop.entries) | set(loop.exits)
    loopblocks = dict((k, blocks[k].copy()) for k in loopblockkeys)
    # Modify the loop blocks
    _loop_lift_prepare_loop_func(loopinfo, loopblocks)

    # Create a new IR for the lifted loop
    lifted_ir = func_ir.derive(blocks=loopblocks,
                               arg_names=tuple(loopinfo.inputs),
                               arg_count=len(loopinfo.inputs),
                               force_non_generator=True)
    liftedloop = LiftedLoop(lifted_ir,
                            typingctx, targetctx, flags, locals)

    # modify for calling into liftedloop
    callblock = _loop_lift_modify_call_block(liftedloop, blocks[loopinfo.callfrom],
                                             loopinfo.inputs, loopinfo.outputs,
                                             loopinfo.returnto)
    # remove blocks
    for k in loopblockkeys:
        del blocks[k]
    # update main interpreter callsite into the liftedloop
    blocks[loopinfo.callfrom] = callblock
    return liftedloop


def loop_lifting(func_ir, typingctx, targetctx, flags, locals):
    """
    Loop lifting transformation.

    Given a interpreter `func_ir` returns a 2 tuple of
    `(toplevel_interp, [loop0_interp, loop1_interp, ....])`
    """
    blocks = func_ir.blocks.copy()
    cfg = compute_cfg_from_blocks(blocks)
    loopinfos = _loop_lift_get_candidate_infos(cfg, blocks,
                                               func_ir.variable_lifetime.livemap)
    loops = []
    for loopinfo in loopinfos:
        lifted = _loop_lift_modify_blocks(func_ir, loopinfo, blocks,
                                          typingctx, targetctx, flags, locals)
        loops.append(lifted)

    # Make main IR
    main = func_ir.derive(blocks=blocks)

    return main, loops


def canonicalize_cfg_single_backedge(blocks):
    """
    Rewrite loops that have multiple backedges.
    """
    cfg = compute_cfg_from_blocks(blocks)
    newblocks = blocks.copy()

    def new_block_id():
        return max(newblocks.keys()) + 1

    def has_multiple_backedges(loop):
        count = 0
        for k in loop.body:
            blk = blocks[k]
            edges = blk.terminator.get_targets()
            # is a backedge?
            if loop.header in edges:
                count += 1
                if count > 1:
                    # early exit
                    return True
        return False

    def yield_loops_with_multiple_backedges():
        for lp in cfg.loops().values():
            if has_multiple_backedges(lp):
                yield lp

    def replace_target(term, src, dst):
        def replace(target):
            return (dst if target == src else target)

        if isinstance(term, ir.Branch):
            return ir.Branch(cond=term.cond,
                             truebr=replace(term.truebr),
                             falsebr=replace(term.falsebr),
                             loc=term.loc)
        elif isinstance(term, ir.Jump):
            return ir.Jump(target=replace(term.target), loc=term.loc)
        else:
            assert not term.get_targets()
            return term

    def rewrite_single_backedge(loop):
        """
        Add new tail block that gathers all the backedges
        """
        header = loop.header
        tailkey = new_block_id()
        for blkkey in loop.body:
            blk = newblocks[blkkey]
            if header in blk.terminator.get_targets():
                newblk = blk.copy()
                # rewrite backedge into jumps to new tail block
                newblk.body[-1] = replace_target(blk.terminator, header,
                                                 tailkey)
                newblocks[blkkey] = newblk
        # create new tail block
        entryblk = newblocks[header]
        tailblk = ir.Block(scope=entryblk.scope, loc=entryblk.loc)
        # add backedge
        tailblk.append(ir.Jump(target=header, loc=tailblk.loc))
        newblocks[tailkey] = tailblk

    for loop in yield_loops_with_multiple_backedges():
        rewrite_single_backedge(loop)

    return newblocks


def canonicalize_cfg(blocks):
    """
    Rewrite the given blocks to canonicalize the CFG.
    Returns a new dictionary of blocks.
    """
    return canonicalize_cfg_single_backedge(blocks)


def with_lifting(func_ir):
    """With-lifting transformation

    Rewrite the IR to extract all withs.
    Only the top-level withs are extracted.
    Returns the (the_new_ir, the_extracted_blocks)
    """
    blocks = func_ir.blocks.copy()
    withs = find_setupwiths(blocks)
    cfg = compute_cfg_from_blocks(blocks)
    _legalize_withs_cfg(withs, cfg)
    # Remove the with blocks that are in the with-body
    extracted = {}
    for (blk_start, blk_end) in withs:
        cur_with = []
        for node in _cfg_nodes_in_region(cfg, blk_start, blk_end):
            cur_with.append(blocks[node])
            del blocks[node]
        extracted[(blk_start, blk_end)] = cur_with

        _legalize_with_head(blocks[blk_start])
        cmkind = _get_with_contextmanager(func_ir, blocks, blk_start)
        cmkind.mutate_with_body(blocks, blk_start, blk_end)

    if not extracted:
        # Unchanged
        new_ir = func_ir
    else:
        new_ir = func_ir.derive(blocks)
    return new_ir, extracted


def _get_with_contextmanager(func_ir, blocks, blk_start):
    """Get the global object used for as the context manager
    """
    for stmt in blocks[blk_start].body:
        if isinstance(stmt, ir.EnterWith):
            var_ref = stmt.contextmanager
            dfn = func_ir.get_definition(var_ref)
            if not isinstance(dfn, ir.Global):
                raise errors.CompilerError("Illegal use of context-manager. ")
            return dfn.value
    # No
    raise errors.CompilerError("malformed with-context usage")


def _bypass_with_context(blocks, blk_start, blk_end):
    """Given the starting and ending block of the with-context,
    replaces a new head block that jumps to the end.

    *blocks* is modified inplace.
    """
    sblk = blocks[blk_start]
    newblk = ir.Block(scope=sblk.scope, loc=sblk.loc)
    newblk.append(ir.Jump(target=blk_end, loc=sblk.loc))
    blocks[blk_start] = newblk


def _legalize_with_head(blk):
    """Given *blk*, the head block of the with-context, check that it doesn't
    do anything else.
    """
    # TODO: check that the with-head doesn't do anything else


def _cfg_nodes_in_region(cfg, region_begin, region_end):
    """Find the set of CFG nodes that are in the given region
    """
    region_nodes = set()
    stack = [region_begin]
    while stack:
        tos = stack.pop()
        succs, _ = zip(*cfg.successors(tos))
        nodes = set([node for node in succs
                     if node not in region_nodes and
                     node != region_end])
        stack.extend(nodes)
        region_nodes |= nodes

    return region_nodes


def _legalize_withs_cfg(withs, cfg):
    """Verify the CFG of the with-context(s).
    """
    doms = cfg.dominators()
    postdoms = cfg.post_dominators()

    # Verify that the with-context has no side-exits
    for s, e in withs:
        if s not in doms[e]:
            msg = "Entry of with-context not dominating the exit."
            raise errors.CompilerError(msg)
        if e not in postdoms[s]:
            msg = "Exit of with-context not post-dominating the entry."
            raise errors.CompilerError(msg)


def find_setupwiths(blocks):
    """Find all top-level with
    """
    def find_ranges(blocks):
        for blk in blocks.values():
            for md in blk.find_insts(ir.Metadata):
                if md.name == 'setupwith':
                    begin = md.data['begin']
                    end = md.data['end']
                    yield begin, end

    def previously_occurred(start, known_ranges):
        for a, b in known_ranges:
            if s >= a and s < b:
                return True
        return False

    known_ranges = []
    for s, e in sorted(find_ranges(blocks)):
        if not previously_occurred(s, known_ranges):
            assert s in blocks, 'starting offset is not a label'
            assert e in blocks, 'ending offset is not a label'
            known_ranges.append((s, e))

    return known_ranges

