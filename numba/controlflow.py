from __future__ import print_function, division, absolute_import

import collections
import functools
import sys

from numba import utils


NEW_BLOCKERS = frozenset(['SETUP_LOOP', 'FOR_ITER'])


class CFBlock(object):
    def __init__(self, offset):
        self.offset = offset
        self.body = []
        # A map of jumps to outgoing blocks (successors):
        #   { offset of outgoing block -> number of stack pops }
        self.outgoing_jumps = {}
        # A map of jumps to incoming blocks (predecessors):
        #   { offset of incoming block -> number of stack pops }
        self.incoming_jumps = {}
        self.terminating = False

    def __repr__(self):
        args = self.offset, sorted(self.outgoing_jumps), sorted(self.incoming_jumps)
        return "block(offset:%d, outgoing: %s, incoming: %s)" % args

    def __iter__(self):
        return iter(self.body)


Loop = collections.namedtuple("Loop", ("entries", "exits", "header", "body"))


class CFGraph(object):
    """
    Generic (almost) implementation of a Control Flow Graph.
    """

    def __init__(self):
        self._nodes = set()
        self._preds = collections.defaultdict(set)
        self._succs = collections.defaultdict(set)
        self._edge_data = {}
        self._entry_point = None

    def add_node(self, node):
        """
        Add *node* to the graph.  This is necessary before adding any
        edges from/to the node.  *node* can be any hashable object.
        """
        self._nodes.add(node)

    def add_edge(self, src, dest, data=None):
        """
        Add an edge from node *src* to node *dest*, with optional
        per-edge *data*.
        If such an edge already exists, it is replaced (duplicate edges
        are not possible).
        """
        assert src in self._nodes
        assert dest in self._nodes
        self._add_edge(src, dest, data)

    def successors(self, src):
        """
        Yield (node, data) pairs representing the successors of node *src*.
        (*data* will be None if no data was specified when adding the edge)
        """
        for dest in self._succs:
            yield dest, self._edge_data[src, dest]

    def predecessors(self, dest):
        """
        Yield (node, data) pairs representing the predecessors of node *dest*.
        (*data* will be None if no data was specified when adding the edge)
        """
        for src in self._preds:
            yield src, self._edge_data[src, dest]

    def set_entry_point(self, node):
        """
        Set the entry point of the graph to *node*.
        """
        assert node in self._nodes
        self._entry_point = node

    def process(self):
        """
        Compute various properties of the control flow graph.  The graph
        must have been fully populated, and its entry point specified.
        """
        if self._entry_point is None:
            raise RuntimeError("no entry point defined!")
        self._eliminate_dead_blocks()
        self._find_exit_points()
        self._find_dominators()
        self._find_back_edges()
        self._find_loops()
        self._find_post_dominators()

    def dominators(self):
        """
        Return a dictionary of {node -> set(nodes)} mapping each node to
        the nodes dominating it.

        A node D dominates a node N when any path leading to N must go through D.
        """
        return self._doms

    def post_dominators(self):
        """
        Return a dictionary of {node -> set(nodes)} mapping each node to
        the nodes post-dominating it.

        A node P post-dominates a node N when any path starting from N must go
        through P.
        """
        return self._post_doms

    def exit_points(self):
        """
        Return the computed set of exit nodes (may be empty).
        """
        return self._exit_points

    def backbone(self):
        """
        Return the set of nodes constituting the graph's backbone.
        (i.e. the nodes that every path starting from the entry point
         must go through).  By construction, it is non-empty: it contains
         at least the entry point.
        """
        return self._post_doms[self._entry_point]

    def loops(self):
        """
        Return a dictionary of {node -> loop} mapping each loop header
        to the loop (a Loop instance) starting with it.
        """
        return self._loops

    def in_loops(self, node):
        """
        Return the list of Loop objects the *node* belongs to,
        from innermost to outermost.
        """
        return [self._loops[x] for x in self._in_loops[node]]

    def dead_nodes(self):
        """
        Return the set of dead nodes (eliminated from the graph).
        """
        return self._dead_nodes

    def nodes(self):
        """
        Return the set of live nodes.
        """
        return self._nodes

    def dump(self, file=None):
        """
        Dump extensive debug information.
        """
        import pprint
        file = file or sys.stdout
        if 1:
            print("CFG adjacency lists:", file=file)
            self._dump_adj_lists(file)
        print("CFG dominators:", file=file)
        pprint.pprint(self._doms, stream=file)
        print("CFG post-dominators:", file=file)
        pprint.pprint(self._post_doms, stream=file)
        print("CFG back edges:", sorted(self._back_edges), file=file)
        print("CFG loops:", file=file)
        pprint.pprint(self._loops, stream=file)
        print("CFG node-to-loops:", file=file)
        pprint.pprint(self._in_loops, stream=file)

    # Internal APIs

    def _add_edge(self, from_, to, data=None):
        # This internal version allows adding edges to/from unregistered
        # (ghost) nodes.
        self._preds[to].add(from_)
        self._succs[from_].add(to)
        self._edge_data[from_, to] = data

    def _remove_node_edges(self, node):
        for succ in self._succs.pop(node, ()):
            self._preds[succ].remove(node)
            del self._edge_data[node, succ]
        for pred in self._preds.pop(node, ()):
            self._succs[pred].remove(node)
            del self._edge_data[pred, node]

    def _dfs(self, entries=None):
        if entries is None:
            entries = (self._entry_point,)
        seen = set()
        stack = list(entries)
        while stack:
            node = stack.pop()
            if node not in seen:
                yield node
                seen.add(node)
                for succ in self._succs[node]:
                    stack.append(succ)

    def _eliminate_dead_blocks(self):
        """
        Eliminate all blocks not reachable from the entry point, and
        stash them into self._dead_nodes.
        """
        live = set()
        for node in self._dfs():
            live.add(node)
        self._dead_nodes = self._nodes - live
        self._nodes = live
        # Remove all edges leading from dead nodes
        for dead in self._dead_nodes:
            self._remove_node_edges(dead)

    def _find_exit_points(self):
        """
        Compute the graph's exit points.
        """
        exit_points = set()
        for n in self._nodes:
            if not self._succs.get(n):
                exit_points.add(n)
        self._exit_points = exit_points

    def _find_dominators_internal(self, post=False):
        # See theoretical description in
        # http://en.wikipedia.org/wiki/Dominator_%28graph_theory%29
        # The algorithm implemented here uses a todo-list as described
        # in http://pages.cs.wisc.edu/~fischer/cs701.f08/finding.loops.html
        if post:
            entries = set(self._exit_points)
            preds_table = self._succs
            succs_table = self._preds
        else:
            entries = set([self._entry_point])
            preds_table = self._preds
            succs_table = self._succs

        if not entries:
            raise RuntimeError("no entry points: dominator algorithm "
                               "cannot be seeded")

        doms = {}
        for e in entries:
            doms[e] = set([e])

        todo = []
        for n in self._nodes:
            if n not in entries:
                doms[n] = set(self._nodes)
                todo.append(n)

        while todo:
            n = todo.pop()
            if n in entries:
                continue
            new_doms = set([n])
            preds = preds_table[n]
            if preds:
                new_doms |= functools.reduce(set.intersection,
                                             [doms[p] for p in preds])
            if new_doms != doms[n]:
                assert len(new_doms) < len(doms[n])
                doms[n] = new_doms
                todo.extend(succs_table[n])
        return doms

    def _find_dominators(self):
        self._doms = self._find_dominators_internal(post=False)

    def _find_post_dominators(self):
        # To handle infinite loops correctly, we need to add a dummy
        # exit point, and link members of infinite loops to it.
        dummy_exit = object()
        self._exit_points.add(dummy_exit)
        for loop in self._loops.values():
            if not loop.exits:
                for b in loop.body:
                    self._add_edge(b, dummy_exit)
        self._post_doms = self._find_dominators_internal(post=True)
        # Fix the _post_doms table to make no reference to the dummy exit
        del self._post_doms[dummy_exit]
        for doms in self._post_doms.values():
            doms.discard(dummy_exit)
        self._remove_node_edges(dummy_exit)
        self._exit_points.remove(dummy_exit)
        # A crude check that we've removed all references to dummy_exit
        assert sys.getrefcount(dummy_exit) == 2, sys.getrefcount(dummy_exit)

    # Finding loops and back edges: see
    # http://pages.cs.wisc.edu/~fischer/cs701.f08/finding.loops.html

    def _find_back_edges(self):
        """
        Find back edges.  An edge (src, dest) is a back edge if and
        only if *dest* dominates *src*.
        """
        back_edges = set()
        for src, succs in self._succs.items():
            back = self._doms[src] & succs
            # In CPython bytecode, at most one back edge can flow from a
            # given block.
            assert len(back) <= 1
            back_edges.update((src, dest) for dest in back)
        self._back_edges = back_edges

    def _find_loops(self):
        """
        Find the loops defined by the graph's back edges.
        """
        bodies = {}
        for src, dest in self._back_edges:
            # The destination of the back edge is the loop header
            header = dest
            # Build up the loop body from the back edge's source node.
            body = set([header])
            queue = [src]
            while queue:
                n = queue.pop()
                if n not in body:
                    body.add(n)
                    queue.extend(self._preds[n])
            if header in bodies:
                bodies[header].update(body)
            else:
                bodies[header] = body

        loops = {}
        for header, body in bodies.items():
            entries = set()
            exits = set()
            for n in body:
                entries.update(self._preds[n] - body)
                exits.update(self._succs[n] - body)
            loop = Loop(header=header, body=body, entries=entries, exits=exits)
            loops[header] = loop
        self._loops = loops

        in_loops = dict((n, []) for n in self._nodes)
        # Sort loops from longest to shortest
        # This ensures that outer loops will come before inner loops
        for loop in sorted(loops.values(), key=lambda loop: len(loop.body)):
            for n in loop.body:
                in_loops[n].append(loop.header)
        self._in_loops = in_loops

    def _dump_adj_lists(self, file):
        adj_lists = dict((src, list(dests))
                         for src, dests in self._succs.items())
        import pprint
        pprint.pprint(adj_lists, stream=file)


class ControlFlowAnalysis(object):
    """
    Attributes
    ----------
    - bytecode

    - blocks

    - blockseq

    - doms: dict of set
        Dominators

    - backbone: set of block offsets
        The set of block that is common to all possible code path.

    """
    def __init__(self, bytecode):
        self.bytecode = bytecode
        self.blocks = {}
        self.liveblocks = {}
        self.blockseq = []
        self.doms = None
        self.backbone = None
        # Internal temp states
        self._force_new_block = True
        self._curblock = None
        self._blockstack = []
        self._loops = []

    def iterblocks(self):
        """
        Return all blocks in sequence of occurrence
        """
        for i in self.blockseq:
            yield self.blocks[i]

    def iterliveblocks(self):
        """
        Return all live blocks in sequence of occurrence
        """
        for i in self.blockseq:
            if i in self.liveblocks:
                yield self.blocks[i]

    def incoming_blocks(self, block):
        """
        Yield (incoming block, number of stack pops) pairs for *block*.
        """
        for i, pops in block.incoming_jumps.items():
            if i in self.liveblocks:
                yield self.blocks[i], pops

    def dump(self, file=None):
        self.graph.dump(file=None)

    def run(self):
        for inst in self._iter_inst():
            fname = "op_%s" % inst.opname
            fn = getattr(self, fname, None)
            if fn is not None:
                fn(inst)
            else:
                assert not inst.is_jump, inst

        # Close all blocks
        for cur, nxt in zip(self.blockseq, self.blockseq[1:]):
            blk = self.blocks[cur]
            if not blk.outgoing_jumps and not blk.terminating:
                blk.outgoing_jumps[nxt] = 0

        graph = CFGraph()
        for b in self.blocks:
            graph.add_node(b)
        for b in self.blocks.values():
            for out, pops in b.outgoing_jumps.items():
                graph.add_edge(b.offset, out, pops)
        graph.set_entry_point(min(self.blocks))
        graph.process()
        self.graph = graph

        # Fill incoming
        for b in utils.dict_itervalues(self.blocks):
            for out, pops in b.outgoing_jumps.items():
                self.blocks[out].incoming_jumps[b.offset] = pops

        # Find liveblocks
        self.liveblocks = dict((i, self.blocks[i])
                               for i in self.graph.nodes())

        for lastblk in reversed(self.blockseq):
            if lastblk in self.liveblocks:
                break
        else:
            raise AssertionError("No live block that exits!?")

        # Find backbone
        backbone = self.graph.backbone()
        # Filter out in loop blocks (Assuming no other cyclic control blocks)
        # This is to unavoid variable defined in loops to be considered as
        # function scope.
        inloopblocks = set()

        for b in self.blocks.keys():
            for s, e in self._loops:
                if s <= b < e:
                    inloopblocks.add(b)

        self.backbone = backbone - inloopblocks

    def jump(self, target, pops=0):
        """
        Register a jump (conditional or not) to *target* offset.
        *pops* is the number of stack pops implied by the jump (default 0).
        """
        self._curblock.outgoing_jumps[target] = pops

    def _iter_inst(self):
        for inst in self.bytecode:
            if self._use_new_block(inst):
                self._start_new_block(inst)
            self._curblock.body.append(inst.offset)
            yield inst

    def _use_new_block(self, inst):
        if inst.offset in self.bytecode.labels:
            res = True
        elif inst.opname in NEW_BLOCKERS:
            res = True
        else:
            res = self._force_new_block

        self._force_new_block = False
        return res

    def _start_new_block(self, inst):
        self._curblock = CFBlock(inst.offset)
        self.blocks[inst.offset] = self._curblock
        self.blockseq.append(inst.offset)

    def op_SETUP_LOOP(self, inst):
        end = inst.get_jump_target()
        self._blockstack.append(end)
        self._loops.append((inst.offset, end))

    def op_POP_BLOCK(self, inst):
        self._blockstack.pop()

    def op_FOR_ITER(self, inst):
        self.jump(inst.get_jump_target())
        self.jump(inst.next)
        self._force_new_block = True

    def _op_ABSOLUTE_JUMP_IF(self, inst):
        self.jump(inst.get_jump_target())
        self.jump(inst.next)
        self._force_new_block = True

    op_POP_JUMP_IF_FALSE = _op_ABSOLUTE_JUMP_IF
    op_POP_JUMP_IF_TRUE = _op_ABSOLUTE_JUMP_IF
    op_JUMP_IF_FALSE = _op_ABSOLUTE_JUMP_IF
    op_JUMP_IF_TRUE = _op_ABSOLUTE_JUMP_IF

    def _op_ABSOLUTE_JUMP_OR_POP(self, inst):
        self.jump(inst.get_jump_target())
        self.jump(inst.next, pops=1)
        self._force_new_block = True

    op_JUMP_IF_FALSE_OR_POP = _op_ABSOLUTE_JUMP_OR_POP
    op_JUMP_IF_TRUE_OR_POP = _op_ABSOLUTE_JUMP_OR_POP

    def op_JUMP_ABSOLUTE(self, inst):
        self.jump(inst.get_jump_target())
        self._force_new_block = True

    def op_JUMP_FORWARD(self, inst):
        self.jump(inst.get_jump_target())
        self._force_new_block = True

    def op_RETURN_VALUE(self, inst):
        self._curblock.terminating = True
        self._force_new_block = True

    def op_RAISE_VARARGS(self, inst):
        self._curblock.terminating = True
        self._force_new_block = True

    def op_BREAK_LOOP(self, inst):
        self.jump(self._blockstack[-1])
        self._force_new_block = True
