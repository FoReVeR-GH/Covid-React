from __future__ import print_function
import sys
import pprint


class RedefinedError(NameError):
    pass


class NotDefinedError(NameError):
    pass


class VerificationError(Exception):
    pass


class Loc(object):
    """Source location

    """

    def __init__(self, line, col=None):
        self.line = line
        self.col = col

    def __repr__(self):
        return "Loc(line=%s, col=%s)" % (self.line, self.col)

    def __str__(self):
        return "(line=%s, col=%s)" % (self.line, self.col)


class VarMap(object):
    def __init__(self):
        self._con = {}

    def define(self, name, var):
        if name in self._con:
            raise RedefinedError(name)
        else:
            self._con[name] = var

    def refer(self, name):
        try:
            return self._con[name]
        except KeyError:
            raise NotDefinedError(name)

    def get_or_insert(self, name, var):
        if name not in self._con:
            self._con[name] = var
        return self._con[name]

    def __len__(self):
        return len(self._con)

    def __repr__(self):
        return pprint.pformat(self._con)

    def __hash__(self):
        return hash(self.name)

    def __iter__(self):
        return self._con.iterkeys()


class Stmt(object):
    is_terminator = False


class Expr(object):
    def __init__(self, op, loc, **kws):
        self.op = op
        self.loc = loc
        self._kws = kws
        for k, v in kws.items():
            setattr(self, k, v)

    @classmethod
    def binop(cls, fn, lhs, rhs, loc):
        op = 'binop'
        return cls(op=op, loc=loc, fn=fn, lhs=lhs, rhs=rhs)

    @classmethod
    def call(cls, func, args, kws, loc):
        op = 'call'
        return cls(op=op, loc=loc, func=func, args=args, kws=kws)

    @classmethod
    def getiter(cls, value, loc):
        op = 'getiter'
        return cls(op=op, loc=loc, value=value)

    @classmethod
    def iternext(cls, value, loc):
        op = 'iternext'
        return cls(op=op, loc=loc, value=value)

    @classmethod
    def itervalid(cls, value, loc):
        op = 'itervalid'
        return cls(op=op, loc=loc, value=value)

    @classmethod
    def getattr(cls, value, attr, loc):
        op = 'getattr'
        return cls(op=op, loc=loc, value=value, attr=attr)

    @classmethod
    def getitem(cls, target, index, loc):
        op = 'getitem'
        return cls(op=op, loc=loc, target=target, index=index)

    def __repr__(self):
        if self.op == 'call':
            args = ', '.join(str(a) for a in self.args)
            kws = ', '.join('%s=%s' % (k, v) for k, v in self.kws)
            return 'call %s(%s, %s)' % (self.func, args, kws)
        elif self.op == 'binop':
            return '%s %s %s' % (self.lhs, self.fn, self.rhs)
        else:
            args = ('%s=%s' % (k, v) for k, v in self._kws.items())
            return '%s(%s)' % (self.op, ', '.join(args))


class SetItem(Stmt):
    def __init__(self, target, index, value, loc):
        self.target = target
        self.index = index
        self.value = value
        self.loc = loc

    def __str__(self):
        return '%s[%s] = %s' % (self.target, self.index, self.value)


class Return(Stmt):
    is_terminator = True

    def __init__(self, value, loc):
        self.value = value
        self.loc = loc

    def __str__(self):
        return 'return %s' % self.value


class Jump(Stmt):
    is_terminator = True

    def __init__(self, target, loc):
        self.target = target
        self.loc = loc

    def __str__(self):
        return 'jump %s' % self.target


class Branch(Stmt):
    is_terminator = True

    def __init__(self, cond, truebr, falsebr, loc):
        self.cond = cond
        self.truebr = truebr
        self.falsebr = falsebr
        self.loc = loc

    def __str__(self):
        return 'branch %s, %s, %s' % (self.cond, self.truebr, self.falsebr)


class Assign(Stmt):
    def __init__(self, value, target, loc):
        self.value = value
        self.target = target
        self.loc = loc

    def __str__(self):
        return '%s = %s' % (self.target, self.value)


class Const(object):
    def __init__(self, value, loc):
        self.value = value
        self.loc = loc

    def __repr__(self):
        return 'const(%s, %s)' % (type(self.value), self.value)


class Global(object):
    def __init__(self, name, value, loc):
        self.name = name
        self.value = value
        self.loc = loc

    def __str__(self):
        return 'global(%s: %s)' % (self.name, self.value)


class Var(object):
    """
    Attributes
    -----------
    - scope: Scope

    - name: str

    - loc: Loc
        Definition location
    """

    def __init__(self, scope, name, loc):
        self.scope = scope
        self.name = name
        self.loc = loc

    def __repr__(self):
        return 'Var(%s, %s)' % (self.name, self.loc)

    def __str__(self):
        return self.name


class Scope(object):
    """
    Attributes
    -----------
    - parent: Scope
        Parent scope

    - localvars: VarMap
        Scope-local variable map

    - loc: Loc
        Start of scope location

    """

    def __init__(self, parent, loc):
        self.parent = parent
        self.localvars = VarMap()
        self.loc = loc

    def define(self, name, loc):
        """
        Define a variable
        """
        v = Var(scope=self, name=name, loc=loc)
        self.localvars.define(v.name, v)
        return v

    def refer(self, name):
        """
        Refer to a variable
        """
        try:
            return self.localvars.refer(name)
        except NotDefinedError:
            if self.has_parent:
                return self.parent.refer(name)
            else:
                raise

    def get_or_insert(self, name, loc):
        v = Var(scope=self, name=name, loc=loc)
        self.localvars.get_or_insert(v.name, v)
        return v

    def make_temp(self, loc):
        n = len(self.localvars)
        v = Var(scope=self, name='$%d' % n, loc=loc)
        self.localvars.define(v.name, v)
        return v

    @property
    def has_parent(self):
        return self.parent is not None

    def __repr__(self):
        return "Scope(has_parent=%r, num_vars=%d, %s)" % (self.has_parent,
                                                          len(self.localvars),
                                                          self.loc)


class Block(object):
    """A code block

    """

    def __init__(self, scope, loc):
        self.scope = scope
        self.body = []
        self.loc = loc

    def append(self, inst):
        assert isinstance(inst, Stmt)
        self.body.append(inst)

    def dump(self, file=sys.stdout):
        for inst in self.body:
            print('  ', inst, file=file)

    @property
    def terminator(self):
        return self.body[-1]

    @property
    def is_terminated(self):
        return self.body and self.body[-1].is_terminator

    def verify(self):
        if not self.is_terminated:
            raise VerificationError("Missing block terminator")
            # Only the last instruction can be a terminator
        for inst in self.body[:-1]:
            if inst.is_terminator:
                raise VerificationError("Terminator before the last "
                                        "instruction")


class Loop(object):
    __slots__ = "entry", "condition", "body", "exit"

    def __init__(self, entry, exit, condition=None):
        self.entry = entry
        self.condition = condition
        self.body = []
        self.exit = exit

    def valid(self):
        try:
            self.verify()
        except VerificationError:
            return False
        else:
            return True

    def verify(self):
        if self.entry is None:
            raise VerificationError("Missing entry block")
        if self.condition is None:
            raise VerificationError("Missing condition block")
        if self.exit is None:
            raise VerificationError("Missing exit block")
        if not self.body:
            raise VerificationError("Missing body block")

    def __repr__(self):
        args = self.entry, self.condition, self.body, self.exit
        return "Loop(entry=%s, condition=%s, body=%s, exit=%s)" % args


# A stub for undefined global reference
UNDEFINED = object()
