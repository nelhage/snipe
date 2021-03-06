# -*- encoding: utf-8 -*-
# Copyright © 2014 Karl Ramm
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following
# disclaimer in the documentation and/or other materials provided
# with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
# INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
# TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR
# TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF
# THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
'''
snipe.editor
------------
'''

import contextlib
import logging
import functools
import unicodedata
import re

from . import window
from . import interactive
from . import keymap
from . import util
from . import gap


@functools.total_ordering
class Mark:
    def __init__(self, buf, point):
        self.buf = buf
        self.mark = buf.buf.mark(point)

    @property
    def point(self):
        """The value of the mark in external coordinates"""
        return self.mark.point

    @point.setter
    def point(self, val):
        self.mark.point = val

    def __repr__(self):
        return '<%s %x %s>' % (
            self.__class__.__name__,
            id(self),
            repr(self.mark),
            )

    def replace(self, count, string, collapsible=False):
        return self.buf.replace(self.mark, count, string, collapsible)

    def insert(self, s, collapsible=False):
        self.point += self.replace(0, s, collapsible)

    def delete(self, count):
        self.replace(count, '')

    def __int__(self):
        return self.point

    def __index__(self):
        return self.point

    def __eq__(self, other):
        try:
            return self.point == int(other)
        except (ValueError, TypeError):
            return False

    def __lt__(self, other):
        return self.point < int(other)


class Buffer:
    '''higher-level abstract notion of an editable chunk of data'''

    registry = {}

    def __init__(self, name=None, content=None, chunksize=None):
        if not name:
            name = '*x%x*' % (id(self),)
        self.name = self.register(name)

        self.buf = gap.UndoableGapBuffer(content=content, chunksize=chunksize)
        self.cache = {}

    def register(self, name):
        if name not in self.registry:
            self.registry[name]  = self
            return name

        r = re.compile(r'^%s(?:|\[(\d+)\])$' % (re.escape(name),))
        competition = filter(
            None, (r.match(bufname) for bufname in self.registry))
        competition = [m.group(1) for m in competition]
        n = max(int(i) if i is not None else 0 for i in competition) + 1
        return self.register('%s[%d]' % (name, n))

    def mark(self, where):
        return Mark(self, where)

    def __str__(self):
        return self.buf.text

    def __len__(self):
        return self.buf.size

    def __getitem__(self, k):
        if hasattr(k, 'start'):
            start, stop, step = k.indices(len(self))
            if step != 1:
                raise ValueError('cannot step through a buffer')
            return self.buf.textrange(start, stop)
        else:
            if k < 0:
                k = len(self) - k
            return self.buf.textrange(k, k+1)

    def undo(self, which):
        self.cache = {}
        return self.buf.undo(which)

    def replace(self, where, count, string, collapsible):
        self.cache = {}
        return self.buf.replace(where, count, string, collapsible)


class Viewer(window.Window, window.PagingMixIn):
    EOL = '\n'

    def __init__(
        self,
        *args,
        prototype=None,
        chunksize=None,
        content=None,
        name=None,
        **kw):

        if not prototype:
            self.buf = Buffer(name=name, content=content, chunksize=chunksize)
        else:
            self.buf = prototype.buf

        super().__init__(*args, **kw)

        from . import help
        self.keymap['[escape] ?'] = help.help

        self.log = logging.getLogger('Editor.%x' % (id(self),))

        if not prototype:
            self.cursor = self.buf.mark(0)
            self.the_mark = None
            self.mark_ring = []

            self.yank_state = 1
            self.undo_state = None

            self.goal_column = None
        else:
            self.cursor = self.buf.mark(prototype.cursor)
            self.the_mark = self.buf.mark(prototype.the_mark)
            self.mark_ring = [self.buf.mark(i) for i in prototype.mark_ring]

            self.yank_state = prototype.yank_state
            self.undo_state = prototype.undo_state #?

            self.goal_column = prototype.goal_column

    def movable(self, point, interactive):
        return point

    def check_redisplay_hint(self, hint):
        self.log.debug('checking hint %s', repr(hint))
        if super().check_redisplay_hint(hint):
            return True
        if hint.get('buffer', None) is self.buf:
            return True
        return False

    def redisplay_hint(self):
        hint = super().redisplay_hint()
        hint['buffer'] = self.buf
        self.log.debug('returning hint %s', repr(hint))
        return hint

    def insert(self, s, collapsible=False):
        self.cursor.point += self.replace(0, s, collapsible)

    def delete(self, count):
        self.log.debug('delete %d', count)
        self.replace(count, '')

    def replace(self, count, string, collapsible=False):
        return self.cursor.replace(count, string, collapsible)

    @keymap.bind('Control-F', '[right]')
    def move_forward(self, count: interactive.integer_argument=1):
        """Move the point forward a character.  Given a count, move forward n
        characters."""

        self.move(count, True)

    @keymap.bind('Control-B', '[left]')
    def move_backward(self, count: interactive.integer_argument=1):
        """Move the point backward a character.  Given a count, move backward n
        characters."""

        self.move(-count, True)

    def move(self, delta, interactive=False):
        """.move(delta, mark=None) -> actual distance moved
        Move the point by delta."""

        z = self.cursor.point
        # the setter does appropriate clamping
        self.cursor.point = self.movable(self.cursor.point + delta, interactive)
        self.goal_column = None
        return self.cursor.point - z

    @keymap.bind('Control-N', '[down]')
    def line_next(self, count: interactive.integer_argument=1):
        """Move the point down a line.  Given a count, move down n lines.
        Attempts to keep the cursor at the same column."""

        self.line_move(count, interactive=True)

    @keymap.bind('Control-P', '[up]')
    def line_previous(self, count: interactive.integer_argument=1):
        """Move the point up a line.  Given a count, move up n lines.
        Attempts to keep the cursor at the same column."""

        self.line_move(-count, interactive=True)

    def line_move(self, delta, track_column=True, interactive=False):
        where = self.buf.mark(self.cursor)
        with self.save_excursion(where):
            count = abs(delta)
            goal_column = self.goal_column
            if goal_column is None:
                with self.save_excursion():
                    p = self.cursor.point
                    self.beginning_of_line()
                    goal_column = p - self.cursor.point
            for _ in range(count):
                if delta < 0:
                    self.beginning_of_line()
                    self.move(-1)
                    self.beginning_of_line()
                elif delta > 0:
                    self.end_of_line()
                    if not self.move(1):
                        self.beginning_of_line()
            with self.save_excursion():
                p = self.cursor.point
                self.end_of_line()
                line_length = self.cursor.point - p
            if track_column:
                self.move(min(goal_column, line_length))
                self.goal_column = goal_column
        oldpoint = self.cursor.point
        self.cursor.point = self.movable(where.point, interactive)
        return self.cursor.point - oldpoint

    def extract_current_line(self):
        p = self.cursor.point
        r = self.buf.cache.setdefault('extract_current_line', {}).get(p)
        if r is not None:
            return r

        with self.save_excursion():
            self.beginning_of_line()
            start = self.cursor.point
            self.end_of_line()
            self.move(1)
            result = (start, self.buf[start:self.cursor])
            self.buf.cache['extract_current_line'][p] = result
            return result

    def view(self, origin, direction='forward'):
        # this is the right place to do special processing of
        # e.g. control characters
        m = self.buf.mark(origin)

        if direction not in ('forward', 'backward'):
            raise ValueError('invalid direction', direction)

        while True:
            with self.save_excursion(m):
                p, s = self.extract_current_line()
            l = len(s)
            if ((p <= self.cursor.point < p + l)
                or (self.cursor.point == p + l == len(self.buf))):
                yield (
                    self.buf.mark(p),
                    [
                        ((), s[:self.cursor.point - p]),
                        (('cursor', 'visible'), ''),
                        ((), s[self.cursor.point - p:]),
                        ],
                    )
            else:
                yield self.buf.mark(p), [((), s)]
            if direction == 'forward':
                if p == len(self.buf) or s[-1:] != '\n':
                    break
                m.point += l
            else:
                if p == 0:
                    break
                m.point = p - 1

    def character_at_point(self):
        return self.buf[self.cursor.point]

    def find_character(self, cs, delta=1):
        while self.move(delta):
            x = self.character_at_point()
            if x and x in cs:
                return x
        return ''

    @keymap.bind('Control-A', '[home]')
    def beginning_of_line(
            self,
            count: interactive.integer_argument=None,
            interactive: interactive.isinteractive=False,
            ):
        """Move the point to the beginning of the line.  Given a count, move the
        point to the beginning of the n-1th line down."""
        where = self.buf.mark(self.cursor)

        with self.save_excursion(where):
            if count is not None:
                self.line_move(count - 1)
            if self.cursor.point == 0:
                return
            with self.save_excursion():
                self.move(-1)
                if self.character_at_point() == self.EOL:
                    return
            if self.find_character(self.EOL, -1):
                self.move(1)
        oldpoint = self.cursor.point
        self.cursor.point = self.movable(where.point, interactive)
        return self.cursor.point - oldpoint

    @keymap.bind('Control-E', '[end]')
    def end_of_line(
            self,
            count: interactive.integer_argument=None,
            interactive: interactive.isinteractive=False,
            ):
        """Move the point to the end of the line.  Given a count, move the
        point to the beginning of the n-1th line down."""

        where = self.buf.mark(self.cursor)
        with self.save_excursion(where):
            if count is not None:
                self.line_move(count - 1)
            if not self.character_at_point() == self.EOL:
                self.find_character(self.EOL)
        oldpoint = self.cursor.point
        self.cursor.point = self.movable(where.point, interactive)
        return self.cursor.point - oldpoint

    @contextlib.contextmanager
    def save_excursion(self, where=None):
        """Context manager that saves the current cursor and mark state.
        If called with a mark, set the cursor to that mark at he beginning,
        and set the mark to the inside-cursor state at the end."""

        cursor = self.buf.mark(self.cursor)
        mark = self.buf.mark(self.the_mark or self.cursor)
        mark_ring = self.mark_ring
        self.mark_ring = list(self.mark_ring)
        goal_column = self.goal_column
        if where is not None:
            self.cursor.point = where
        yield
        if where is not None:
            where.point = self.cursor
        self.cursor.point = cursor
        self.the_mark = mark
        self.mark_ring = mark_ring
        self.goal_column = goal_column

    @keymap.bind('Shift-[HOME]', '[SHOME]', 'Meta-<')
    def beginning_of_buffer(
            self,
            pct: interactive.argument,
            interactive: interactive.isinteractive=False,
            ):
        """Move the point to the beginning of the buffer.  With a single-digit
        count, move the point to the beginning of a line somewhere around n*10 %
        of the way through the buffer."""

        self.log.debug('beginning_of_buffer: pct=%s', repr(pct))
        where = self.buf.mark(self.cursor)
        oldpoint = self.cursor.point
        with self.save_excursion(where):
            if not isinstance(pct, int):
                pct = 0
            if pct < 0:
                return self.end_of_buffer(-pct)
            self.cursor.point = min(pct * len(self.buf) // 10, len(self.buf))
            self.beginning_of_line()
            if oldpoint != self.cursor.point:
                self.set_mark(oldpoint)
        self.cursor.point = self.movable(where.point, interactive)
        return self.cursor.point - oldpoint

    @keymap.bind('Shift-[END]', '[SEND]', 'Meta->')
    def end_of_buffer(
            self,
            pct: interactive.argument=None,
            interactive: interactive.isinteractive=False,
            ):
        """Move the point to the end of the buffer.  With a single-digit count,
        move the point to the beginning of a line somewhere around n*10 %
        of the way from the end of the buffer."""

        self.log.debug(
            'end_of_buffer: arg, %s oldpoint %s', repr(pct), self.cursor.point)
        where = self.buf.mark(self.cursor)
        oldpoint = self.cursor.point
        with self.save_excursion(where):
            noarg = pct is None
            if not isinstance(pct, int):
                pct = 0
            if pct < 0:
                return self.beginning_of_buffer(-pct)
            self.cursor.point = max((10 - pct) * len(self.buf) // 10, 0)
            if not noarg:
                self.beginning_of_line()
            self.log.debug('end_of_buffer: newpoint %s', self.cursor.point)
            if oldpoint != self.cursor.point:
                self.log.debug('end_of_buffer: setting mark %s', oldpoint)
                self.set_mark(oldpoint)
        self.cursor.point = self.movable(where.point, interactive)
        return self.cursor.point - oldpoint

    def input_char(self, k):
        self.log.debug('before command %s %s', self.cursor, self.the_mark)
        super().input_char(k)
        self.log.debug('after command  %s %s', self.cursor, self.the_mark)

    @staticmethod
    def iswordchar(c):
        cat = unicodedata.category(c)
        return cat[0] == 'L' or cat[0] == 'N' or cat == 'Pc' or c == "'" # sigh

    def isword(self, delta=0):
        return self.ispred(self.iswordchar, delta)

    def ispred(self, pred, delta=0):
        with self.save_excursion():
            if delta and not self.move(delta):
                return None
            c = self.character_at_point()
            if not c:
                return False
            return pred(c)

    @keymap.bind('Meta-f') #XXX should also be Meta-right but curses
    def word_forward(
            self,
            count: interactive.integer_argument=1,
            interactive: interactive.isinteractive=False,
            ):
        """Move the point forward to the location after the current word.  Given
        a count, do this n times.  Given a negative count, hand off to
        word_backward."""

        if count < 0:
            return self.word_backward(-count, interactive=interactive)
        for _ in range(count):
            while not self.isword():
                if not self.move(1, interactive):
                    return
            while self.isword():
                if not self.move(1, interactive):
                    return

    @keymap.bind('Meta-b') #XXX should also be Meta-left but curses
    def word_backward(
            self,
            count: interactive.integer_argument=1,
            interactive: interactive.isinteractive=False,
            ):
        """Move the point backward to the beginning of the the previous word.
        Given a count, do this n times.  Given a negative count, hand off to
        word_forward."""

        if count < 0:
            return self.word_forward(-count, interactive=interactive)
        for _ in range(count):
            while not self.isword(-1):
                if not self.move(-1, interactive):
                    return
            while self.isword(-1):
                if not self.move(-1, interactive):
                    return

    def region(self, mark=None):
        if mark is None:
            mark = self.the_mark
        if mark is None:
            return None

        start = min(self.cursor, self.the_mark)
        stop = max(self.cursor, self.the_mark)
        return self.buf[start:stop]

    @keymap.bind('Meta-w')
    def copy_region(self):
        """Copy (do not delete) the region (between the point and the mark) into
        the kill ring."""

        if self.the_mark is None:
            self.whine('no mark is set')
            return
        self.context.copy(self.region())
        self.yank_state = 1

    @keymap.bind('Control-[space]')
    def set_mark(self, where=None, prefix: interactive.argument=None):
        """Without a ^U before*, set the mark (append it to the mark ring).

        With a ^U, stick the current point at the current beginning of the
        mark ring, and set the point to the current end of the mark ring.

        * repeating the command after a ^U will continue to jump around
        rather than setting the mark.
        """
        #XXX this documentation is weak and this could share a lot of code
        #with the same command over in the messager

        if prefix is not None or \
          (self.last_command == 'set_mark' and self.set_mark_state == 1):
            self.mark_ring.insert(
                0, self.buf.mark(where if where is not None else self.cursor))
            where = self.the_mark
            self.the_mark = self.mark_ring.pop()
            self.cursor = where
            self.set_mark_state = 1
        else:
            self.mark_ring.append(self.the_mark)
            self.the_mark = self.buf.mark(where if where is not None else self.cursor)
            self.set_mark_state = 0

    @keymap.bind('Control-X Control-X')
    def exchange_point_and_mark(self):
        """Move the point to where the mark is, and set the mark where the point
        used to be."""

        if self.the_mark is not None:
            self.cursor, self.the_mark = self.the_mark, self.cursor

    def insert_region(self, s):
        self.set_mark()
        self.insert(s)


class Editor(Viewer):
    default_fill_column = util.Configurable(
        'editor.fill_column', 72, 'Default fill column for auto-fill buffers',
        coerce=int)

    def __init__(self, *args, fill=False, **kw):
        super().__init__(*args, **kw)
        if fill:
            self.fill_column = self.default_fill_column
        else:
            self.fill_column = 0

        self.column = None

        self.keymap.default = self.self_insert

        prototype = kw.get('prototype')
        if prototype is None:
            self._writable = True
        else:
            self._writable = getattr(prototype, '_writable', True)

    def writable(self):
        return self._writable

    def replace(self, count, string, collapsible=False):
        if not self.writable():
            self.whine('window is readonly')
            return
        return super().replace(count, string, collapsible)

    @keymap.bind(
        '[tab]', '[linefeed]',
        *(chr(x) for x in range(ord(' '), ord('~') + 1)))
    def self_insert(
            self,
            key: interactive.keystroke,
            count: interactive.positive_integer_argument=1):
        """Insert the last keystroke of the triggering key sequence into the
        buffer."""

        if self.fill_column:
            if self.last_command != 'self_insert':
                self.column = None
            if self.column is None:
                self.column = self.current_column()
            self.column += count #XXX tabs, wide characters

        collapsible = True
        if self.last_command == 'self_insert':
            if (not self.last_key.isspace()) and key.isspace():
                collapsible=False
        for _ in range(count):
            self.insert(key, collapsible)

        if self.fill_column and key.isspace() and self.column > self.fill_column:
            self.log.debug('triggering autofill')
            self.do_auto_fill()

    def current_column(self):
        with self.save_excursion():
            p0 = self.cursor.point
            self.beginning_of_line()
            return p0 - self.cursor.point

    def do_auto_fill(self):
        with self.save_excursion():
            p0 = self.cursor.point

            self.beginning_of_line()
            bol = self.cursor.point

            if not self.writable():
                return # don't wrap prompt lines :-p

            import textwrap
            ll = textwrap.wrap(
                self.buf[bol:p0], width=self.fill_column, break_long_words=False)
            s = '\n'.join(ll)
            if ll:
                if len(ll[-1]) < self.fill_column:
                    s += ' '
                else:
                    s += '\n'
            self.replace(p0 - bol, s)
            self.column = None

    @keymap.bind('Control-X f')
    def set_fill_column(self, column: interactive.argument=None):
        """Set the current fill column.  With a non-numeric prefix argument, set
        it to the current column.  With a numeric prefix argument, set it to the
        given number.  Otherwise, ask."""

        if column is None:
            s = yield from self.read_string(
                'new fill column (current is %d): ' % self.fill_column)
            try:
                self.fill_column = int(s)
            except ValueError as e:
                self.whine(str(e))
        elif isinstance(column, int):
            self.fill_column = column
        else:
            self.fill_column = self.current_column

    @keymap.bind('[carriage return]', 'Control-J')
    def insert_newline(self, count: interactive.positive_integer_argument=1):
        """Insert a newline, or n newlines."""

        self.insert('\n' * count)

    @keymap.bind('Control-D', '[dc]')
    def delete_forward(self, count: interactive.integer_argument=1):
        """Delete characters after the point, one by default, n if
        specified."""

        if count < 0:
            moved = self.move(count)
            count = -moved
        self.delete(count)

    @keymap.bind('Control-H', 'Control-?', '[backspace]')
    def delete_backward(self, count: interactive.integer_argument=1):
        """Delete characters before the point, one by default, n if specified."""

        self.delete_forward(-count)

    @keymap.bind('Control-k')
    def kill_to_end_of_line(self, count: interactive.integer_argument):
        """Kill (delete and copy to the kill ring) the text until the end of the
        line.  If the point is at the end of the line already, kill the line
        boundary.  Given a count of zero, kill to the beginning of the line,
        otherwise kill that many lines.

        If the previous command was a kill command, append the killed text to the
        text on the end of the kill ring."""

        m = self.buf.mark(self.cursor)
        if count is None: #"normal" case
            self.end_of_line()
            if m == self.cursor:
                # at the end of a line, move past it
                self.move(1)
            if m == self.cursor:
                # end of buffer
                return
        elif count == 0: # kill to beginning of line?
            self.beginning_of_line()
        else:
            self.line_move(count, False)
        self.kill_region(mark=m, append=self.last_command.startswith('kill_'))

    @keymap.bind('Control-W')
    def kill_region(self, mark=None, append=False):
        """Kill (delete and copy to the kill ring) the current region.

        If the previous command was a kill command, append the killed text to the
        text on the end of the kill ring."""

        if mark is None:
            mark = self.the_mark
        if mark is None:
            self.whine('no mark is set')
            return
        self.log.debug('kill region %d-%d', self.cursor.point, mark.point)

        if not append:
            self.context.copy(self.region(mark))
        else:
            self.context.copy(self.region(mark), mark < self.cursor)

        count = abs(mark.point - self.cursor.point)
        self.cursor = min(self.cursor, mark)
        self.delete(count)

        self.yank_state = 1

    @keymap.bind('Control-Y')
    def yank(self, arg: interactive.argument=None):
        """Insert a copy of the the top of the kill ring to the buffer and leave
        the mark set at the top of what was just inserted.  With an integer
        argument, rotate the kill ring by n first.  With just a prexix argument,
        exchange the point and the mark afterwards."""

        if arg and isinstance(arg, int):
            self.yank_state += arg - 1
        self.insert_region(self.context.yank(self.yank_state))
        if arg is not None and not isinstance(nth, int):
            self.exchange_point_and_mark()

    @keymap.bind('Meta-y')
    def yank_pop(self, arg: interactive.integer_argument=1):
        """After a yank, rotate the kill ring by one (or n if specified) and
        replace the just-yanked text with whatever's now at the top of the kill
        ring."""

        if self.last_command not in ('yank', 'yank_pop'):
            self.whine('last command was not a yank')
            return
        self.yank_state += arg
        if self.cursor > self.the_mark:
            self.exchange_point_and_mark()
        self.delete(abs(self.the_mark.point - self.cursor.point))
        self.insert_region(self.context.yank(self.yank_state))

    @keymap.bind('Control-_', 'Control-x u')
    def undo(self, count: interactive.positive_integer_argument=1):
        """Undo previous changes.   Repeat to undo more.  An integer argument
        is a repeat count."""

        if not self.writable():
            self.whine('window is read-only')
            return
        if self.last_command != 'undo':
            self.undo_state = None
        for _ in range(count):
            self.undo_state, where = self.buf.undo(self.undo_state)
            self.cursor.point = where
            if self.undo_state == None:
                self.whine('Nothing to undo')
                break

    @keymap.bind('Control-T')
    def transpose_chars(self):
        """Transpose the characters around the point, moving forward one
        character."""

        off = 1
        p = self.cursor.point
        if p == len(self.buf):
            off = 2
        if self.move(-off) != -off:
            self.cursor.point = p
            self.whine('At beginning')
            return
        s = self.buf[self.cursor:int(self.cursor) + 2]
        s = ''.join(reversed(s)) # *sigh*
        self.replace(2, s)
        self.move(2)

    @keymap.bind('Control-O')
    def open_line(self, count: interactive.positive_integer_argument=1):
        """Insert a newline (perhaps n newlines) ahead of the point."""

        with self.save_excursion():
            self.insert('\n' * count)

    @keymap.bind(
        'Meta-[backspace]', 'Meta-Control-H', 'Meta-[dc]', 'Meta-[del]')
    def kill_word_backward(self, count: interactive.integer_argument=1):
        """Delete the word before the point.  With an integer argument, delete
        n words before the point."""

        mark = self.buf.mark(self.cursor)
        self.word_backward(count)
        self.kill_region(mark, append=self.last_command.startswith('kill_'))

    @keymap.bind('Meta-d')
    def kill_word_forward(self, count: interactive.integer_argument=1):
        """Delete the word after the point.  With an integer argument, delete
        n words after the point."""

        mark = self.buf.mark(self.cursor)
        self.word_forward(count)
        self.kill_region(mark, append=self.last_command.startswith('kill_'))

    @keymap.bind('Control-X i')
    def insert_file(self):
        """Read a file name and then insert the contents in to the buffer."""

        filename = yield from self.read_filename('Insert File: ')
        try:
            with open(filename) as fp:
                self.insert(fp.read())
        except Exception as exc:
            self.whine(str(exc))

    @keymap.bind('Control-X Control-Q')
    def toggle_writable(self):
        """Toggle the readonly flag on the buffer."""

        self._writable = not self._writable

