#! /usr/bin/env python

"""pandoc-eqnos: a pandoc filter that inserts equation nos. and refs."""


__version__ = '2.0.0'


# Copyright 2015-2019 Thomas J. Duck.
# All rights reserved.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


# OVERVIEW
#
# The basic idea is to scan the document twice in order to:
#
#   1. Insert text for the equation number in each equation.
#      For LaTeX, change to a numbered equation and use \label{...}
#      instead.  The equation labels and associated equation numbers
#      are stored in the global references tracker.
#
#   2. Replace each reference with an equation number.  For LaTeX,
#      replace with \ref{...} instead.
#
# This is followed by injecting header code as needed for certain output
# formats.

# pylint: disable=invalid-name

import re
import functools
import argparse
import json
import copy
import textwrap
import uuid

from pandocfilters import walk
from pandocfilters import Math, RawInline, Str, Span

import pandocxnos
from pandocxnos import PandocAttributes
from pandocxnos import STRTYPES, STDIN, STDOUT, STDERR
from pandocxnos import check_bool, get_meta
from pandocxnos import repair_refs, process_refs_factory, replace_refs_factory
from pandocxnos import attach_attrs_factory, detach_attrs_factory
from pandocxnos import insert_secnos_factory, delete_secnos_factory
from pandocxnos import elt


# Patterns for matching labels and references
LABEL_PATTERN = re.compile(r'(eq:[\w/-]*)')

# Meta variables; may be reset elsewhere
cleveref = False    # Flags that clever references should be used
capitalise = False  # Flags that plusname should be capitalised
plusname = ['eq.', 'eqs.']            # Sets names for mid-sentence references
starname = ['Equation', 'Equations']  # Sets names for refs at sentence start
numbersections = False  # Flags that equations should be numbered by section
secoffset = 0           # Section number offset
eqref = False           # Flags that \eqref should be used
warninglevel = 2        # 0 - no warnings; 1 - some warnings; 2 - all warnings

# Processing state variables
cursec = None    # Current section
Nreferences = 0  # Number of references in current section (or document)
references = {}  # Maps reference labels to [number/tag, equation secno]

# Processing flags
plusname_changed = False          # Flags that the plus name changed
starname_changed = False          # Flags that the star name changed
has_unnumbered_equations = False  # Flags unnumbered equations were found

PANDOCVERSION = None
AttrMath = None


# Actions --------------------------------------------------------------------

# pylint: disable=too-many-branches
def _process_equation(value, fmt):
    """Processes the equation.  Returns a dict containing eq properties."""

    # pylint: disable=global-statement
    global Nreferences  # Global references counter
    global cursec       # Current section
    global has_unnumbered_equations  # Flags that unnumbered eqs were found

    # Initialize the return value
    eq = {'is_unnumbered': False,
          'is_unreferenceable': False,
          'is_tagged': False}

    # Parse the equation
    attrs = eq['attrs'] = PandocAttributes(value[0], 'pandoc')

    # Bail out if the label does not conform to expectations
    if not LABEL_PATTERN.match(attrs.id):
        eq.update({'is_unnumbered':True, 'is_unreferenceable':True})
        return eq

    # Identify unreferenceable equations
    if attrs.id == 'eq:': # Make up a unique description
        attrs.id += str(uuid.uuid4())
        eq['is_unreferenceable'] = True

    # Update the current section number
    if attrs['secno'] != cursec:  # The section number changed
        cursec = attrs['secno']   # Update the global section tracker
        Nreferences = 1           # Resets the global reference counter

    # Pandoc's --number-sections supports section numbering latex/pdf, html,
    # epub, and docx
    if numbersections:
        # Latex/pdf supports equation numbers by section natively.  For the
        # other formats we must hard-code in equation numbers by section as
        # tags.
        if fmt in ['html', 'html5', 'epub', 'epub2', 'epub3', 'docx'] and \
          'tag' not in attrs:
            attrs['tag'] = str(cursec+secoffset) + '.' + str(Nreferences)
            Nreferences += 1

    # Save reference information
    eq['is_tagged'] = 'tag' in attrs
    if eq['is_tagged']:   # ... then save the tag
        # Remove any surrounding quotes
        if attrs['tag'][0] == '"' and attrs['tag'][-1] == '"':
            attrs['tag'] = attrs['tag'].strip('"')
        elif attrs['tag'][0] == "'" and attrs['tag'][-1] == "'":
            attrs['tag'] = attrs['tag'].strip("'")
        references[attrs.id] = [attrs['tag'], cursec]
    else:
        references[attrs.id] = [Nreferences, cursec]
        Nreferences += 1  # Increment the global reference counter

    return eq


def _adjust_equation(fmt, eq, value):
    """Adjusts the equation depending on the output format."""
    attrs = eq['attrs']
    if fmt in ['latex', 'beamer']:
        if not eq['is_unreferenceable']:  # Code in the tags
            if eq['is_tagged']:
                value[-1] += r'\tag{%s}\label{%s}' % \
                  (references[attrs.id][0].replace(' ', r'\ '), attrs.id)
            else:
                value[-1] += r'\label{%s}'%attrs.id
    elif fmt in ('html', 'html5', 'epub', 'epub2', 'epub3'):
        pass  # Insert html in _add_markup() instead
    else:  # Hard-code in the number/tag
        if isinstance(references[attrs.id][0], int):  # Numbered reference
            value[-1] += r'\qquad (%d)' % references[attrs.id][0]
        else:  # Tagged reference
            assert isinstance(references[attrs.id][0], STRTYPES)
            text = references[attrs.id][0].replace(' ', r'\ ')
            if text.startswith('$') and text.endswith('$'):  # Math
                tag = text[1:-1]
            else:  # Text
                tag = r'\text{%s}' % text
            value[-1] += r'\qquad (%s)' % tag


def _add_markup(fmt, eq, value):
    """Adds markup to the output."""

    attrs = eq['attrs']
    ret = None

    # Context-dependent output
    if eq['is_unnumbered']:  # Unnumbered is also unreferenceable
        ret = None
    elif fmt in ['latex', 'beamer']:
        ret = RawInline('tex',
                        r'\begin{equation}%s\end{equation}'%value[-1])
    elif fmt in ('html', 'html5', 'epub', 'epub2', 'epub3') and \
      LABEL_PATTERN.match(attrs.id):
        # Present equation and its number in a span
        text = str(references[attrs.id][0])
        outer = RawInline('html',
                          '<span%sclass="eqnos">' % \
                            (' ' if eq['is_unreferenceable'] else
                             ' id="%s" '%attrs.id))
        inner = RawInline('html', '<span class="eqnos-number">')
        num = Math({"t":"InlineMath"}, '(%s)' % text[1:-1]) \
          if text.startswith('$') and text.endswith('$') \
          else Str('(%s)' % text)
        endtags = RawInline('html', '</span></span>')
        ret = [outer, AttrMath(*value), inner, num, endtags]
    elif fmt == 'docx':
        # As per http://officeopenxml.com/WPhyperlink.php
        bookmarkstart = \
          RawInline('openxml',
                    '<w:bookmarkStart w:id="0" w:name="%s"/><w:r><w:t>'
                    %attrs.id)
        bookmarkend = \
          RawInline('openxml',
                    '</w:t></w:r><w:bookmarkEnd w:id="0"/>')
        ret = [bookmarkstart, AttrMath(*value), bookmarkend]
    return ret


def process_equations(key, value, fmt, meta):  # pylint: disable=unused-argument
    """Processes the attributed equations."""

    # Process attributed equations and add markup
    if key == 'Math' and len(value) == 3:
        eq = _process_equation(value, fmt)
        if eq['attrs'].id:
            _adjust_equation(fmt, eq, value)
        return _add_markup(fmt, eq, value)

    return None


# TeX blocks -----------------------------------------------------------------

# Define some tex to number equations by section
NUMBER_BY_SECTION_TEX = r"""
%% pandoc-eqnos: number equations by section
\numberwithin{equation}{section}
"""

# Section number offset
SECOFFSET_TEX = r"""
%% pandoc-eqnos: section number offset
\setcounter{section}{%s}
"""

# Define some tex to disable brackets around cleveref numbers
DISABLE_CLEVEREF_BRACKETS_TEX = r"""
%% pandoc-eqnos: disable brackets around cleveref numbers
\creflabelformat{equation}{#2#1#3}
"""

# Html blocks ----------------------------------------------------------------

# Equation css
EQUATION_STYLE_HTML = """
<!-- pandoc-eqnos: equation style -->
<style>
  .eqnos { display: inline-block; position: relative; width: 100%; }
  .eqnos br { display: none; }
  .eqnos-number { position: absolute; right: 0em; top: 50%; line-height: 0; }
</style>
"""


# Main program ---------------------------------------------------------------

# pylint: disable=too-many-statements
def process(meta):
    """Saves metadata fields in global variables and returns a few
    computed fields."""

    # pylint: disable=global-statement
    global cleveref    # Flags that clever references should be used
    global capitalise  # Flags that plusname should be capitalised
    global plusname    # Sets names for mid-sentence references
    global starname    # Sets names for references at sentence start
    global numbersections  # Flags that sections should be numbered by section
    global secoffset       # Section number offset
    global warninglevel    # 0 - no warnings; 1 - some; 2 - all
    global plusname_changed  # Flags that the plus name changed
    global starname_changed  # Flags that the star name changed
    global eqref             # Flags that \eqref should be used

    # Read in the metadata fields and do some checking

    for name in ['eqnos-warning-level', 'xnos-warning-level']:
        if name in meta:
            warninglevel = int(get_meta(meta, name))
            break

    metanames = ['eqnos-warning-level', 'xnos-warning-level',
                 'eqnos-cleveref', 'xnos-cleveref',
                 'xnos-capitalise', 'xnos-capitalize',
                 'xnos-caption-separator', # Used by pandoc-fignos/tablenos
                 'eqnos-plus-name', 'eqnos-star-name',
                 'eqnos-number-by-section', 'xnos-number-by-section',
                 'xnos-number-offset',
                 'eqnos-eqref']

    if warninglevel:
        for name in meta:
            if (name.startswith('eqnos') or name.startswith('xnos')) and \
              name not in metanames:
                msg = textwrap.dedent("""
                          pandoc-eqnos: unknown meta variable "%s"\n
                      """ % name)
                STDERR.write(msg)

    for name in ['eqnos-cleveref', 'xnos-cleveref']:
        # 'xnos-cleveref' enables cleveref in all 3 of fignos/eqnos/tablenos
        if name in meta:
            cleveref = check_bool(get_meta(meta, name))
            break

    for name in ['xnos-capitalise', 'xnos-capitalize']:
        # 'xnos-capitalise' enables capitalise in all 3 of
        # fignos/eqnos/tablenos.  Since this uses an option in the caption
        # package, it is not possible to select between the three (use
        # 'eqnos-plus-name' instead.  'xnos-capitalize' is an alternative
        # spelling
        if name in meta:
            capitalise = check_bool(get_meta(meta, name))
            break

    if 'eqnos-plus-name' in meta:
        tmp = get_meta(meta, 'eqnos-plus-name')
        old_plusname = copy.deepcopy(plusname)
        if isinstance(tmp, list):  # The singular and plural forms were given
            plusname = tmp
        else:  # Only the singular form was given
            plusname[0] = tmp
        plusname_changed = plusname != old_plusname
        assert len(plusname) == 2
        for name in plusname:
            assert isinstance(name, STRTYPES)
        if plusname_changed:
            starname = [name.title() for name in plusname]

    if 'eqnos-star-name' in meta:
        tmp = get_meta(meta, 'eqnos-star-name')
        old_starname = copy.deepcopy(starname)
        if isinstance(tmp, list):
            starname = tmp
        else:
            starname[0] = tmp
        starname_changed = starname != old_starname
        assert len(starname) == 2
        for name in starname:
            assert isinstance(name, STRTYPES)

    for name in ['eqnos-number-by-section', 'xnos-number-by-section']:
        if name in meta:
            numbersections = check_bool(get_meta(meta, name))
            break

    if 'xnos-number-offset' in meta:
        secoffset = int(get_meta(meta, 'xnos-number-offset'))

    if 'eqnos-eqref' in meta:
        eqref = check_bool(get_meta(meta, 'eqnos-eqref'))
        if eqref:  # Eqref and cleveref are mutually exclusive
            cleveref = False

def add_tex(meta):
    """Adds tex to the meta data."""

    warnings = warninglevel == 2 and references and \
      (pandocxnos.cleveref_required() or
       plusname_changed or starname_changed or numbersections or secoffset)
    if warnings:
        msg = textwrap.dedent("""\
                  pandoc-eqnos: Wrote the following blocks to
                  header-includes.  If you use pandoc's
                  --include-in-header option then you will need to
                  manually include these yourself.
              """)
        STDERR.write('\n')
        STDERR.write(textwrap.fill(msg))
        STDERR.write('\n')

    # Update the header-includes metadata.  Pandoc's
    # --include-in-header option will override anything we do here.  This
    # is a known issue and is owing to a design decision in pandoc.
    # See https://github.com/jgm/pandoc/issues/3139.

    if pandocxnos.cleveref_required() and references:
        tex = """
            %%%% pandoc-eqnos: required package
            \\usepackage%s{cleveref}
        """ % ('[capitalise]' if capitalise else '')
        pandocxnos.add_to_header_includes(
            meta, 'tex', tex, warninglevel,
            r'\\usepackage(\[[\w\s,]*\])?\{cleveref\}')

        pandocxnos.add_to_header_includes(
            meta, 'tex', DISABLE_CLEVEREF_BRACKETS_TEX, warninglevel)

    if plusname_changed and references:
        tex = """
            %%%% pandoc-eqnos: change cref names
            \\crefname{equation}{%s}{%s}
        """ % (plusname[0], plusname[1])
        pandocxnos.add_to_header_includes(meta, 'tex', tex, warninglevel)

    if starname_changed and references:
        tex = """
            %%%% pandoc-eqnos: change Cref names
            \\Crefname{equation}{%s}{%s}
        """ % (starname[0], starname[1])
        pandocxnos.add_to_header_includes(meta, 'tex', tex, warninglevel)

    if numbersections and references:
        pandocxnos.add_to_header_includes(
            meta, 'tex', NUMBER_BY_SECTION_TEX, warninglevel)

    if secoffset and references:
        pandocxnos.add_to_header_includes(
            meta, 'tex', SECOFFSET_TEX % secoffset, warninglevel,
            r'\\setcounter\{section\}')

    if warnings:
        STDERR.write('\n')

def add_html(meta):
    """Adds html to the meta data."""

    warnings = warninglevel == 2 and references

    if warnings:
        msg = textwrap.dedent("""\
                  pandoc-eqnos: Wrote the following blocks to
                  header-includes.  If you use pandoc's
                  --include-in-header option then you will need to
                  manually include these yourself.
              """)
        STDERR.write('\n')
        STDERR.write(textwrap.fill(msg))
        STDERR.write('\n')

    # Update the header-includes metadata.  Pandoc's
    # --include-in-header option will override anything we do here.  This
    # is a known issue and is owing to a design decision in pandoc.
    # See https://github.com/jgm/pandoc/issues/3139.

    if references:
        pandocxnos.add_to_header_includes(
            meta, 'html', EQUATION_STYLE_HTML, warninglevel)

# pylint: disable=too-many-locals, unused-argument
def main(stdin=STDIN, stdout=STDOUT, stderr=STDERR):
    """Filters the document AST."""

    # pylint: disable=global-statement
    global PANDOCVERSION
    global AttrMath

    # Read the command-line arguments
    parser = argparse.ArgumentParser(\
      description='Pandoc equations numbers filter.')
    parser.add_argument(\
      '--version', action='version',
      version='%(prog)s {version}'.format(version=__version__))
    parser.add_argument('fmt')
    parser.add_argument('--pandocversion', help='The pandoc version.')
    args = parser.parse_args()

    # Get the output format and document
    fmt = args.fmt
    doc = json.loads(stdin.read())

    # Initialize pandocxnos
    PANDOCVERSION = pandocxnos.init(args.pandocversion, doc)

    # Element primitives
    AttrMath = elt('Math', 3)

    # Chop up the doc
    meta = doc['meta'] if PANDOCVERSION >= '1.18' else doc[0]['unMeta']
    blocks = doc['blocks'] if PANDOCVERSION >= '1.18' else doc[1:]

    # Process the metadata variables
    process(meta)

    # First pass
    attach_attrs_math = attach_attrs_factory('pandoc-eqnos', Math,
                                             warninglevel, allow_space=True)
    detach_attrs_math = detach_attrs_factory(Math)
    insert_secnos = insert_secnos_factory(Math)
    delete_secnos = delete_secnos_factory(Math)
    altered = functools.reduce(lambda x, action: walk(x, action, fmt, meta),
                               [attach_attrs_math, insert_secnos,
                                process_equations, delete_secnos,
                                detach_attrs_math], blocks)

    # Second pass
    process_refs = process_refs_factory('pandoc-eqnos', LABEL_PATTERN,
                                        references.keys(), warninglevel)
    replace_refs = replace_refs_factory(references,
                                        cleveref, eqref,
                                        plusname if not capitalise or \
                                        plusname_changed else
                                        [name.title() for name in plusname],
                                        starname)
    attach_attrs_span = attach_attrs_factory('pandoc-eqnos', Span,
                                             warninglevel, replace=True)
    altered = functools.reduce(lambda x, action: walk(x, action, fmt, meta),
                               [repair_refs, process_refs, replace_refs,
                                attach_attrs_span],
                               altered)

    if fmt in ['latex', 'beamer']:
        add_tex(meta)
    elif fmt in ['html', 'html5', 'epub', 'epub2', 'epub3']:
        add_html(meta)

    # Update the doc
    if PANDOCVERSION >= '1.18':
        doc['blocks'] = altered
    else:
        doc = doc[:1] + altered

    # Dump the results
    json.dump(doc, stdout)

    # Flush stdout
    stdout.flush()

if __name__ == '__main__':
    main()
