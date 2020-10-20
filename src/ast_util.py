from typing import Dict, Union, List, Tuple, Callable, Optional, Any, Set
import attr
import bisect
import copy
from random import Random
import re
import sys
import time
import typing

from pycparser import c_ast as ca, c_parser, c_generator

from .error import CandidateConstructionFailure
from .ast_types import (
    SimpleType,
    TypeMap,
    build_typemap,
    decayed_expr_type,
    resolve_typedefs,
    set_decl_name,
)


@attr.s
class Indices:
    starts: Dict[ca.Node, int] = attr.ib()
    ends: Dict[ca.Node, int] = attr.ib()


Block = Union[ca.Compound, ca.Case, ca.Default]
if typing.TYPE_CHECKING:
    # ca.Expression and ca.Statement don't actually exist, they live only in
    # the stubs file.
    Expression = ca.Expression
    Statement = ca.Statement
else:
    Expression = Statement = None


def to_c(node: ca.Node) -> str:
    source = PatchedCGenerator().visit(node)
    if "#pragma" not in source:
        return source
    lines = source.split("\n")
    out = []
    same_line = 0
    in_late_defines = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#pragma"):
            if stripped == "#pragma _permuter sameline start":
                same_line += 1
            elif stripped == "#pragma _permuter sameline end":
                same_line -= 1
                if same_line == 0:
                    out.append("\n")
            elif stripped == "#pragma _permuter latedefine start":
                assert not in_late_defines
                in_late_defines = True
            elif stripped == "#pragma _permuter latedefine end":
                assert in_late_defines
                in_late_defines = False
            elif stripped.startswith("#pragma _permuter define "):
                assert in_late_defines
                out.append("#" + stripped.split(" ", 2)[2] + "\n")

            # Ignore permuter pragmas, but leave actual pragmas in (like intrinsics)
            if stripped.startswith("#pragma _permuter"):
                continue
        if in_late_defines:
            continue
        if not same_line:
            line += "\n"
        elif out and not out[-1].endswith("\n"):
            line = " " + line.lstrip()
        out.append(line)
    assert same_line == 0
    return "".join(out).rstrip() + "\n"


class PatchedCGenerator(c_generator.CGenerator):
    """Like a CGenerator, except it keeps else if's prettier despite
    the terrible things we've done to them in normalize_ast."""

    def visit_If(self, n: ca.If) -> str:
        n2 = n
        if (
            n.iffalse
            and isinstance(n.iffalse, ca.Compound)
            and n.iffalse.block_items
            and len(n.iffalse.block_items) == 1
            and isinstance(n.iffalse.block_items[0], ca.If)
        ):
            n2 = ca.If(cond=n.cond, iftrue=n.iftrue, iffalse=n.iffalse.block_items[0])
        return super().visit_If(n2)  # type: ignore


def extract_fn(ast: ca.FileAST, fn_name: str) -> Tuple[ca.FuncDef, int]:
    ret = []
    for i, node in enumerate(ast.ext):
        if isinstance(node, ca.FuncDef):
            if node.decl.name == fn_name:
                ret.append((node, i))
            else:
                node = node.decl
                ast.ext[i] = node
        if isinstance(node, ca.Decl) and isinstance(node.type, ca.FuncDecl):
            node.funcspec = [spec for spec in node.funcspec if spec != "static"]
    if len(ret) == 0:
        raise CandidateConstructionFailure(f"Function {fn_name} not found in base.c.")
    if len(ret) > 1:
        raise CandidateConstructionFailure(
            f"Found multiple copies of function {fn_name} in base.c."
        )
    return ret[0]


def compute_node_indices(top_node: ca.Node) -> Indices:
    starts: Dict[ca.Node, int] = {}
    ends: Dict[ca.Node, int] = {}
    cur_index = 1

    class Visitor(ca.NodeVisitor):
        def generic_visit(self, node: ca.Node) -> None:
            nonlocal cur_index
            assert node not in starts, "nodes should only appear once in AST"
            starts[node] = cur_index
            cur_index += 2
            super().generic_visit(node)
            ends[node] = cur_index
            cur_index += 2

    Visitor().visit(top_node)
    return Indices(starts, ends)


def equal_ast(a: ca.Node, b: ca.Node) -> bool:
    def equal(a: Any, b: Any) -> bool:
        if type(a) != type(b):
            return False
        if a is None:
            return b is None
        if isinstance(a, list):
            assert isinstance(b, list)
            if len(a) != len(b):
                return False
            for i in range(len(a)):
                if not equal(a[i], b[i]):
                    return False
            return True
        if isinstance(a, (int, str)):
            return bool(a == b)
        assert isinstance(a, ca.Node)
        for name in a.__slots__[:-2]:  # type: ignore
            if not equal(getattr(a, name), getattr(b, name)):
                return False
        return True

    return equal(a, b)


def is_lvalue(expr: Expression) -> bool:
    if isinstance(expr, (ca.ID, ca.StructRef, ca.ArrayRef)):
        return True
    if isinstance(expr, ca.UnaryOp):
        return expr.op == "*"
    return False


def is_effectful(expr: Expression) -> bool:
    found = False

    class Visitor(ca.NodeVisitor):
        def visit_UnaryOp(self, node: ca.UnaryOp) -> None:
            nonlocal found
            if node.op in ["p++", "p--", "++", "--"]:
                found = True
            else:
                self.generic_visit(node.expr)

        def visit_FuncCall(self, _: ca.Node) -> None:
            nonlocal found
            found = True

        def visit_Assignment(self, _: ca.Node) -> None:
            nonlocal found
            found = True

    Visitor().visit(expr)
    return found


def get_block_stmts(block: Block, force: bool) -> List[Statement]:
    if isinstance(block, ca.Compound):
        ret = block.block_items or []
        if force and not block.block_items:
            block.block_items = ret
    else:
        ret = block.stmts or []
        if force and not block.stmts:
            block.stmts = ret
    return ret


def insert_decl(fn: ca.FuncDef, var: str, type: SimpleType) -> None:
    type = copy.deepcopy(type)
    decl = ca.Decl(
        name=var, quals=[], storage=[], funcspec=[], type=type, init=None, bitsize=None
    )
    set_decl_name(decl)
    assert fn.body.block_items, "Non-empty function"
    for index, stmt in enumerate(fn.body.block_items):
        if not isinstance(stmt, ca.Decl):
            break
    else:
        index = len(fn.body.block_items)
    fn.body.block_items[index:index] = [decl]


def insert_statement(block: Block, index: int, stmt: Statement) -> None:
    stmts = get_block_stmts(block, True)
    stmts[index:index] = [stmt]


def brace_nested_blocks(stmt: Statement) -> None:
    def brace(stmt: Statement) -> Block:
        if isinstance(stmt, (ca.Compound, ca.Case, ca.Default)):
            return stmt
        return ca.Compound([stmt])

    if isinstance(stmt, (ca.For, ca.While, ca.DoWhile)):
        stmt.stmt = brace(stmt.stmt)
    elif isinstance(stmt, ca.If):
        stmt.iftrue = brace(stmt.iftrue)
        if stmt.iffalse:
            stmt.iffalse = brace(stmt.iffalse)
    elif isinstance(stmt, ca.Switch):
        stmt.stmt = brace(stmt.stmt)
    elif isinstance(stmt, ca.Label):
        brace_nested_blocks(stmt.stmt)


def has_nested_block(node: ca.Node) -> bool:
    return isinstance(
        node,
        (
            ca.Compound,
            ca.For,
            ca.While,
            ca.DoWhile,
            ca.If,
            ca.Switch,
            ca.Case,
            ca.Default,
        ),
    )


def for_nested_blocks(stmt: Statement, callback: Callable[[Block], None]) -> None:
    def invoke(stmt: Statement) -> None:
        assert isinstance(
            stmt, (ca.Compound, ca.Case, ca.Default)
        ), "brace_nested_blocks should have turned nested statements into blocks"
        callback(stmt)

    if isinstance(stmt, ca.Compound):
        invoke(stmt)
    elif isinstance(stmt, (ca.For, ca.While, ca.DoWhile)):
        invoke(stmt.stmt)
    elif isinstance(stmt, ca.If):
        if stmt.iftrue:
            invoke(stmt.iftrue)
        if stmt.iffalse:
            invoke(stmt.iffalse)
    elif isinstance(stmt, ca.Switch):
        invoke(stmt.stmt)
    elif isinstance(stmt, (ca.Case, ca.Default)):
        invoke(stmt)
    elif isinstance(stmt, ca.Label):
        for_nested_blocks(stmt.stmt, callback)


def normalize_ast(fn: ca.FuncDef, ast: ca.FileAST) -> None:
    """Add braces to all ifs/fors/etc., to make it easier to insert statements."""

    def rec(block: Block) -> None:
        stmts = get_block_stmts(block, False)
        for stmt in stmts:
            brace_nested_blocks(stmt)
            for_nested_blocks(stmt, rec)

    rec(fn.body)


def _ast_used_ids(ast: ca.FileAST) -> Set[str]:
    seen = set()
    re_token = re.compile(r"[a-zA-Z0-9_$]+")

    class Visitor(ca.NodeVisitor):
        def visit_ID(self, node: ca.ID) -> None:
            seen.add(node.name)

        def visit_Pragma(self, node: ca.Pragma) -> None:
            for token in re_token.findall(node.string):
                seen.add(token)

    Visitor().visit(ast)
    return seen


def _used_type_names(ast: ca.FileAST) -> Set[str]:
    seen = set()

    class Visitor(ca.NodeVisitor):
        def visit_IdentifierType(self, node: ca.IdentifierType) -> None:
            for name in node.names:
                seen.add(name)

        def visit_TypeDecl(self, node: ca.TypeDecl) -> None:
            tp = node.type
            if isinstance(tp, ca.IdentifierType):
                for name in tp.names:
                    seen.add(name)
            elif isinstance(tp, ca.Enum):
                if tp.name and not tp.values:
                    seen.add(tp.name)
            else:
                if tp.name and not tp.decls:
                    seen.add(tp.name)
            self.generic_visit(node)

    Visitor().visit(ast)
    return seen


def prune_ast(fn: ca.FuncDef, ast: ca.FileAST) -> int:
    """Prune away unnecessary parts of the AST, to reduce overhead from serialization
    and from the compiler's C parser.

    Structs involved in circular references are currently not removed -- the algorithm
    is based on refcounting instead of tracing GC for simplicity."""
    used_ids = _ast_used_ids(ast)

    new_items = []
    for i in range(len(ast.ext)):
        item = ast.ext[i]
        if (
            isinstance(item, ca.Decl)
            and item.name
            and not item.init
            and item.name not in used_ids
        ):
            # Skip pointless declaration. (Declarations with initializer lists can
            # in some cases affect codegen, so we keep them.)
            pass
        else:
            new_items.append(item)
    ast.ext = new_items

    has_removals = True
    while has_removals:
        has_removals = False

        used_type_names = _used_type_names(ast)

        def useful_type(tp: "ca.Type", for_typedef: bool) -> bool:
            """Check whether a type is needed, e.g. because it contains a definition of
            a struct that's referenced from outside, or definitions or enum members."""
            if isinstance(tp, (ca.PtrDecl, ca.ArrayDecl)):
                return useful_type(tp.type, for_typedef)
            if isinstance(tp, ca.FuncDecl):
                return not for_typedef
            if not isinstance(tp, ca.TypeDecl):
                return True
            inner_type = tp.type
            if isinstance(inner_type, ca.IdentifierType):
                return False
            if inner_type.name is not None and inner_type.name in used_type_names:
                return True
            if isinstance(inner_type, ca.Enum):
                return any(it.name in used_ids for it in inner_type.values.enumerators)
            return False

        new_items = []
        for i in range(len(ast.ext)):
            item = ast.ext[i]
            if (
                isinstance(item, ca.Typedef)
                and item.name not in used_type_names
                and not useful_type(item.type, True)
            ):
                # Skip unused typedefs.
                has_removals = True
            elif (
                isinstance(item, ca.Decl)
                and not item.name
                and not useful_type(item.type, False)
            ):
                # Skip unused struct/union/enum definitions.
                has_removals = True
            else:
                new_items.append(item)

        ast.ext = new_items

    return new_items.index(fn)
