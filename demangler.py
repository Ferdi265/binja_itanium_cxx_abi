# encoding:utf-8
"""
This module implements a C++ Itanium ABI demangler.

The demangler provides a single entry point, `demangle`, and returns either `None`
or an abstract syntax tree. All nodes have, at least, a `kind` field.

Name nodes:
    * `name`: `node.value` (`str`) holds an unqualified name
    * `ctor`: `node.value` is one of `"complete"`, `"base"`, or `"allocating"`, specifying
      the type of constructor
    * `dtor`: `node.value` is one of `"deleting"`, `"complete"`, or `"base"`, specifying
      the type of destructor
    * `operator`: `node.value` (`str`) holds a symbolic operator name, without the keyword
      "operator"
    * `tpl_args`: `node.value` (`tuple`) holds a sequence of type nodes
    * `qual_name`: `node.value` (`tuple`) holds a sequence of `name` and `tpl_args` nodes,
      possibly ending in a `ctor`, `dtor` or `operator` node

Type nodes:
    * `name` and `qual_name` specify a type by its name
    * `pointer`, `lvalue` and `rvalue`: `node.value` holds a pointee type node
    * `literal`: `node.value` (`str`) holds the literal representation as-is,
      `node.qual` holds a type node specifying the type of the literal
    * `cv_qual`: `node.value` holds a type node, `node.qual` (`set`) is any of
      `"const"`, `"volatile"`, or `"restrict"`
    * `function`: `node.name` holds a name node specifying the function name,
      `node.ret` holds a type node specifying the return type of a template function,
      if any, or `None`, ``node.args` (`tuple`) holds a sequence of type nodes
      specifying thefunction arguments

Special nodes:
    * `vtable`, `vtt`, `typeinfo`, and `typeinfo_name`: `node.value` holds a type node
      specifying the type described by this RTTI data structure
"""

import re
from collections import namedtuple


class _Cursor:
    def __init__(self, raw, pos=0):
        self._raw = raw
        self._pos = pos

    def at_end(self):
        return self._pos == len(self._raw)

    def accept(self, delim):
        if self._raw[self._pos:self._pos + len(delim)] == delim:
            self._pos += len(delim)
            return True

    def advance(self, amount):
        if self._pos + amount > len(self._raw):
            return None
        result = self._raw[self._pos:self._pos + amount]
        self._pos += amount
        return result

    def advance_until(self, delim):
        new_pos = self._raw.find(delim, self._pos)
        if new_pos == -1:
            return None
        result = self._raw[self._pos:new_pos]
        self._pos = new_pos + len(delim)
        return result

    def match(self, pattern):
        match = pattern.match(self._raw, self._pos)
        if match:
            self._pos = match.end(0)
        return match

    def __repr__(self):
        return "_Cursor({}, {})".format(self._raw[:self._pos] + '→' + self._raw[self._pos:],
                                        self._pos)


class Node(namedtuple('Node', 'kind value')):
    def __str__(self):
        if self.kind == 'name':
            return self.value
        elif self.kind == 'qual_name':
            result = ''
            for node in self.value:
                if result != '' and node.kind != 'tpl_args':
                    result += '::'
                result += str(node)
            return result
        elif self.kind == 'tpl_args':
            return '<' + ', '.join(map(str, self.value)) + '>'
        elif self.kind == 'ctor':
            if self.value == 'complete':
                return '{ctor}'
            elif self.value == 'base':
                return '{base ctor}'
            elif self.value == 'allocating':
                return '{allocating ctor}'
            else:
                assert False
        elif self.kind == 'dtor':
            if self.value == 'deleting':
                return '{deleting dtor}'
            elif self.value == 'complete':
                return '{dtor}'
            elif self.value == 'base':
                return '{base dtor}'
            else:
                assert False
        elif self.kind == 'operator':
            if self.value.startswith('new') or self.value.startswith('delete'):
                return 'operator ' + self.value
            else:
                return 'operator' + self.value
        elif self.kind == 'pointer':
            return str(self.value) + '*'
        elif self.kind == 'lvalue':
            return str(self.value) + '&'
        elif self.kind == 'rvalue':
            return str(self.value) + '&&'
        elif self.kind == 'function':
            name, args = self.value
            if args == (Node('name', 'void'),):
                return str(name) + '()'
            else:
                return str(name) + '(' + ', '.join(map(str, args)) + ')'
        elif self.kind == 'tpl_param':
            return '{T' + str(self.value) + '}'
        elif self.kind == 'vtable':
            return 'vtable for ' + str(self.value)
        elif self.kind == 'vtt':
            return 'vtt for ' + str(self.value)
        elif self.kind == 'typeinfo':
            return 'typeinfo for ' + str(self.value)
        elif self.kind == 'typeinfo_name':
            return 'typeinfo name for ' + str(self.value)
        else:
            assert False

    def __repr__(self):
        return "<Node {} {}>".format(self.kind, repr(self.value))


class QualNode(namedtuple('QualNode', 'kind value qual')):
    def __str__(self):
        if self.kind == 'cv_qual':
            return ' '.join([str(self.value)] + list(self.qual))
        else:
            assert False

    def __repr__(self):
        return "<QualNode {} {} {}>".format(self.kind, repr(self.qual), repr(self.value))


class CastNode(namedtuple('CastNode', 'kind value ty')):
    def __str__(self):
        if self.kind == 'literal':
            return '(' + str(self.ty) + ')' + str(self.value)
        else:
            assert False

    def __repr__(self):
        return "<CastNode {} {} {}>".format(self.kind, repr(self.ty), repr(self.value))


_ctor_dtor_map = {
    'C1': 'complete',
    'C2': 'base',
    'C3': 'allocating',
    'D0': 'deleting',
    'D1': 'complete',
    'D2': 'base'
}

_std_names = {
    'St': [Node('name', 'std')],
    'Sa': [Node('name', 'std'), Node('name', 'allocator')],
    'Sb': [Node('name', 'std'), Node('name', 'basic_string')],
    'Ss': [Node('name', 'std'), Node('name', 'string')],
    'Si': [Node('name', 'std'), Node('name', 'istream')],
    'So': [Node('name', 'std'), Node('name', 'ostream')],
    'Sd': [Node('name', 'std'), Node('name', 'iostream')],
}

_operators = {
    'nw': 'new',
    'na': 'new[]',
    'dl': 'delete',
    'da': 'delete[]',
    'ps': '+', # (unary)
    'ng': '-', # (unary)
    'ad': '&', # (unary)
    'de': '*', # (unary)
    'co': '~',
    'pl': '+',
    'mi': '-',
    'ml': '*',
    'dv': '/',
    'rm': '%',
    'an': '&',
    'or': '|',
    'eo': '^',
    'aS': '=',
    'pL': '+=',
    'mI': '-=',
    'mL': '*=',
    'dV': '/=',
    'rM': '%=',
    'aN': '&=',
    'oR': '|=',
    'eO': '^=',
    'ls': '<<',
    'rs': '>>',
    'lS': '<<=',
    'rS': '>>=',
    'eq': '==',
    'ne': '!=',
    'lt': '<',
    'gt': '>',
    'le': '<=',
    'ge': '>=',
    'nt': '!',
    'aa': '&&',
    'oo': '||',
    'pp': '++', # (postfix in <expression> context)
    'mm': '--', # (postfix in <expression> context)
    'cm': ',',
    'pm': '->*',
    'pt': '->',
    'cl': '()',
    'ix': '[]',
    'qu': '?',
}

_builtin_types = {
    'v':  'void',
    'w':  'wchar_t',
    'b':  'bool',
    'c':  'char',
    'a':  'signed char',
    'h':  'unsigned char',
    's':  'short',
    't':  'unsigned short',
    'i':  'int',
    'j':  'unsigned int',
    'l':  'long',
    'm':  'unsigned long',
    'x':  'long long',
    'y':  'unsigned long long',
    'n':  '__int128',
    'o':  'unsigned __int128',
    'f':  'float',
    'd':  'double',
    'e':  '__float80',
    'g':  '__float128',
    'z':  '...',
    'Di': 'char32_t',
    'Ds': 'char16_t',
    'Da': 'auto',
}


def _handle_cv(qualifiers, node):
    qualifier_set = set()
    if 'r' in qualifiers:
        qualifier_set.add('restrict')
    if 'V' in qualifiers:
        qualifier_set.add('volatile')
    if 'K' in qualifiers:
        qualifier_set.add('const')
    if qualifier_set:
        return QualNode('cv_qual', node, qualifier_set)
    return node

def _handle_indirect(qualifier, node):
    if qualifier == 'P':
        return Node('pointer', node)
    elif qualifier == 'R':
        return Node('lvalue', node)
    elif qualifier == 'O':
        return Node('rvalue', node)
    return node


def _parse_until_end(cursor, kind, fn):
    nodes = []
    while not cursor.accept('E'):
        node = fn(cursor)
        if node is None or cursor.at_end():
            return None
        nodes.append(node)
    return Node(kind, nodes)

def _parse_source_name(cursor, length):
    name_len = int(length)
    name = cursor.advance(name_len)
    if name is None:
        return None
    return Node('name', name)


_NAME_RE = re.compile(r"""
(?P<source_name>        \d+)    |
(?P<ctor_name>          C[123]) |
(?P<dtor_name>          D[012]) |
(?P<std_name>           S[absiod]) |
(?P<operator_name>      nw|na|dl|da|ps|ng|ad|de|co|pl|mi|ml|dv|rm|an|or|
                        eo|aS|pL|mI|mL|dV|rM|aN|oR|eO|ls|rs|lS|rS|eq|ne|
                        lt|gt|le|ge|nt|aa|oo|pp|mm|cm|pm|pt|cl|ix|qu) |
(?P<std_prefix>         St) |
(?P<nested_name>        N (?P<cv_qual> [rVK]*) (?P<ref_qual> [RO]?)) |
(?P<template_args>      I)
""", re.X)

def _parse_name(cursor):
    match = cursor.match(_NAME_RE)
    if match is None:
        return None
    elif match.group('source_name') is not None:
        node = _parse_source_name(cursor, match.group('source_name'))
        if node is None:
            return None
    elif match.group('ctor_name') is not None:
        node = Node('ctor', _ctor_dtor_map[match.group('ctor_name')])
    elif match.group('dtor_name') is not None:
        node = Node('dtor', _ctor_dtor_map[match.group('dtor_name')])
    elif match.group('std_name') is not None:
        node = Node('qual_name', _std_names[match.group('std_name')])
    elif match.group('operator_name') is not None:
        node = Node('operator', _operators[match.group('operator_name')])
    elif match.group('std_prefix') is not None:
        name = _parse_name(cursor)
        if name is None:
            return None
        if name.kind == 'qual_name':
            node = Node('qual_name', [Node('name', 'std')] + name.value)
        else:
            node = Node('qual_name', [Node('name', 'std'), name])
    elif match.group('nested_name') is not None:
        node = _parse_until_end(cursor, 'qual_name', _parse_name)
        node = _handle_cv(match.group('cv_qual'), node)
        node = _handle_indirect(match.group('ref_qual'), node)
    elif match.group('template_args') is not None:
        node = _parse_until_end(cursor, 'tpl_args', _parse_type)
    if node is None:
        return None

    if cursor.accept('I'):
        templ_args = _parse_until_end(cursor, 'tpl_args', _parse_type)
        if templ_args is None:
            return None
        node = Node('qual_name', [node, templ_args])

    return node


_TYPE_RE = re.compile(r"""
(?P<builtin_type>       v|w|b|c|a|h|s|t|i|j|l|m|x|y|n|o|f|d|e|g|z|
                        Dd|De|Df|Dh|DF|Di|Ds|Da|Dc|Dn) |
(?P<qualified_type>     [rVK]+) |
(?P<template_param>     T) |
(?P<indirect_type>      [PRO]) |
(?P<expr_primary>       (?= L))
""", re.X)

def _parse_type(cursor):
    match = cursor.match(_TYPE_RE)
    if match is None:
        return _parse_name(cursor)
    elif match.group('builtin_type') is not None:
        return Node('name', _builtin_types[match.group('builtin_type')])
    elif match.group('qualified_type') is not None:
        ty = _parse_type(cursor)
        if ty is None:
            return None
        return _handle_cv(match.group('qualified_type'), ty)
    elif match.group('template_param') is not None:
        seq_id = cursor.advance_until('_')
        if seq_id == '':
            return Node('tpl_param', 0)
        else:
            return Node('tpl_param', 1 + int(seq_id, 36))
    elif match.group('indirect_type') is not None:
        ty = _parse_type(cursor)
        if ty is None:
            return None
        return _handle_indirect(match.group('indirect_type'), ty)
    elif match.group('expr_primary') is not None:
        return _parse_expr_primary(cursor)


_EXPR_PRIMARY_RE = re.compile(r"""
(?P<mangled_name>       L (?= _Z)) |
(?P<literal>            L)
""", re.X)

def _parse_expr_primary(cursor):
    match = cursor.match(_EXPR_PRIMARY_RE)
    if match is None:
        return None
    elif match.group('mangled_name') is not None:
        mangled_name = cursor.advance_until('E')
        return _parse_mangled_name(_Cursor(mangled_name))
    elif match.group('literal') is not None:
        ty = _parse_type(cursor)
        if ty is None:
            return None
        value = cursor.advance_until('E')
        if value is None:
            return None
        return CastNode('literal', value, ty)


_SPECIAL_RE = re.compile(r"""
(?P<special>            T (?P<kind> [VTIS]))
""", re.X)

def _parse_special(cursor):
    match = cursor.match(_SPECIAL_RE)
    if match is None:
        return None
    elif match.group('special') is not None:
        name = _parse_type(cursor)
        if name is None:
            return None
        if match.group('kind') == 'V':
            return Node('vtable', name)
        elif match.group('kind') == 'T' is not None:
            return Node('vtt', name)
        elif match.group('kind') == 'I' is not None:
            return Node('typeinfo', name)
        elif match.group('kind') == 'S' is not None:
            return Node('typeinfo_name', name)


_MANGLED_NAME_RE = re.compile(r"""
(?P<mangled_name>       _Z)
""", re.X)

def _parse_mangled_name(cursor):
    match = cursor.match(_MANGLED_NAME_RE)
    if match is None:
        return None
    else:
        special = _parse_special(cursor)
        if special is not None:
            return special

        name = _parse_name(cursor)
        if name is None:
            return None

        arg_types = []
        while not cursor.at_end():
            arg_type = _parse_type(cursor)
            if arg_type is None:
                return None
            arg_types.append(arg_type)

        if arg_types:
            return Node('function', (name, tuple(arg_types)))
        else:
            return name


def parse(raw):
    return _parse_mangled_name(_Cursor(raw))

# ================================================================================================

import unittest


class TestDemangler(unittest.TestCase):
    def assertDemangles(self, mangled, demangled):
        result = parse(mangled)
        if result is not None:
            result = str(result)
        self.assertEqual(result, demangled)

    def test_name(self):
        self.assertDemangles('_Z3foo', 'foo')
        self.assertDemangles('_Z3x', None)

    def test_ctor_dtor(self):
        self.assertDemangles('_ZN3fooC1E', 'foo::{ctor}')
        self.assertDemangles('_ZN3fooC2E', 'foo::{base ctor}')
        self.assertDemangles('_ZN3fooC3E', 'foo::{allocating ctor}')
        self.assertDemangles('_ZN3fooD0E', 'foo::{deleting dtor}')
        self.assertDemangles('_ZN3fooD1E', 'foo::{dtor}')
        self.assertDemangles('_ZN3fooD2E', 'foo::{base dtor}')

    def test_operator(self):
        for op in _operators:
            if _operators[op] in ['new', 'new[]', 'delete', 'delete[]']:
                continue
            self.assertDemangles('_Z' + op, 'operator' + _operators[op])
        self.assertDemangles('_Znw', 'operator new')
        self.assertDemangles('_Zna', 'operator new[]')
        self.assertDemangles('_Zdl', 'operator delete')
        self.assertDemangles('_Zda', 'operator delete[]')

    def test_std_substs(self):
        self.assertDemangles('_ZSt', None)
        self.assertDemangles('_ZSt3foo', 'std::foo')
        self.assertDemangles('_ZSs', 'std::string')

    def test_nested_name(self):
        self.assertDemangles('_ZN3fooE', 'foo')
        self.assertDemangles('_ZN3foo5bargeE', 'foo::barge')
        self.assertDemangles('_ZN3fooIcE5bargeE', 'foo<char>::barge')
        self.assertDemangles('_ZNK3fooE', 'foo const')
        self.assertDemangles('_ZNV3fooE', 'foo volatile')
        self.assertDemangles('_ZNKR3fooE', 'foo const&')
        self.assertDemangles('_ZNKO3fooE', 'foo const&&')

    def test_template_args(self):
        self.assertDemangles('_Z3fooIcE', 'foo<char>')
        self.assertDemangles('_ZN3fooIcEE', 'foo<char>')

    def test_builtin_types(self):
        for ty in _builtin_types:
            if ty == 'v':
                continue
            self.assertDemangles('_Z1f' + ty, 'f(' + _builtin_types[ty] + ')')
        self.assertDemangles('_Z1fv', 'f()')

    def test_qualified_type(self):
        self.assertDemangles('_Z1fri', 'f(int restrict)')
        self.assertDemangles('_Z1fKi', 'f(int const)')
        self.assertDemangles('_Z1fVi', 'f(int volatile)')
        self.assertDemangles('_Z1fVVVi', 'f(int volatile)')

    def test_indirect_type(self):
        self.assertDemangles('_Z1fPi', 'f(int*)')
        self.assertDemangles('_Z1fRi', 'f(int&)')
        self.assertDemangles('_Z1fOi', 'f(int&&)')
        self.assertDemangles('_Z1fKRi', 'f(int& const)')
        self.assertDemangles('_Z1fRKi', 'f(int const&)')

    def test_literal(self):
        self.assertDemangles('_Z1fILi1EE', 'f<(int)1>')
        self.assertDemangles('_Z1fIL_Z1gEE', 'f<g>')

    def test_special(self):
        self.assertDemangles('_ZTV1f', 'vtable for f')
        self.assertDemangles('_ZTT1f', 'vtt for f')
        self.assertDemangles('_ZTI1f', 'typeinfo for f')
        self.assertDemangles('_ZTS1f', 'typeinfo name for f')


if __name__ == '__main__':
    import sys
    ast = parse(sys.argv[1])
    print(repr(ast))
    print(ast)