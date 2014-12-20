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

import array
import weakref
import contextlib
import logging
import functools
import unicodedata
import re

from . import window
from . import interactive
from . import keymap
from . import util


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

    def __eq__(self, other):
        return self.point == int(other)

    def __lt__(self, other):
        return self.point < int(other)


class Buffer:
    '''higher-level abstract notion of an editable chunk of data'''

    registry = {}

    def __init__(self, name=None, content=None, chunksize=None):
        if name is None:
            name = '*x%x*' % (id(self),)
        self.name = self.register(name)

        self.buf = UndoableGapBuffer(content=content, chunksize=chunksize)
        self.cache = {}

    def register(self, name):
        if name not in self.registry:
            self.registry[name]  = self
            return name

        r = re.compile(r'^%s(?:|\[(\d+)\])$' % (name,))
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
        if hasattr(k, 'start'): # slice object, ignore the step
            start = k.start if k.start is not None else 0
            stop = k.stop if k.stop is not None else len(self)
            if start < 0:
                start = len(self) - start
            if stop < 0:
                stop = len(self) - start
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
        self.move(count)

    @keymap.bind('Control-B', '[left]')
    def move_backward(self, count: interactive.integer_argument=1):
        self.move(-count)

    def move(self, delta):
        '''.move(delta, mark=None) -> actual distance moved
        Move the point by delta.
        '''
        z = self.cursor.point
        self.cursor.point += delta # the setter does appropriate clamping
        self.goal_column = None
        return self.cursor.point - z

    @keymap.bind('Control-N', '[down]')
    def line_next(self, count: interactive.integer_argument=1):
        self.line_move(count)

    @keymap.bind('Control-P', '[up]')
    def line_previous(self, count: interactive.integer_argument=1):
        self.line_move(-count)

    def line_move(self, delta, track_column=True):
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
    def beginning_of_line(self, count: interactive.integer_argument=None):
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

    @keymap.bind('Control-E', '[end]')
    def end_of_line(self, count: interactive.integer_argument=None):
        if count is not None:
            self.line_move(count - 1)
        if not self.character_at_point() == self.EOL:
            self.find_character(self.EOL)

    @contextlib.contextmanager
    def save_excursion(self, where=None):
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
    def beginning_of_buffer(self, pct: interactive.argument):
        self.log.debug('beginning_of_buffer: pct=%s', repr(pct))
        oldpoint = self.cursor.point
        if not isinstance(pct, int):
            pct = 0
        if pct < 0:
            return self.end_of_buffer(-pct)
        self.cursor.point = min(pct * len(self.buf) // 10, len(self.buf))
        self.beginning_of_line()
        if oldpoint != self.cursor.point:
            self.set_mark(oldpoint)

    @keymap.bind('Shift-[END]', '[SEND]', 'Meta->')
    def end_of_buffer(self, pct: interactive.argument=None):
        oldpoint = self.cursor.point
        self.log.debug('end_of_buffer: arg, %s oldpoint %s', repr(pct), oldpoint)
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
    def word_forward(self, count: interactive.integer_argument=1):
        if count < 0:
            return self.word_backward(-count)
        for _ in range(count):
            while not self.isword():
                if not self.move(1):
                    return
            while self.isword():
                if not self.move(1):
                    return

    @keymap.bind('Meta-b') #XXX should also be Meta-left but curses
    def word_backward(self, count: interactive.integer_argument=1):
        if count < 0:
            return self.word_forward(-count)
        for _ in range(count):
            while not self.isword(-1):
                if not self.move(-1):
                    return
            while self.isword(-1):
                if not self.move(-1):
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
        if self.the_mark is None:
            self.whine('no mark is set')
            return
        self.context.copy(self.region())
        self.yank_state = 1

    @keymap.bind('Control-[space]')
    def set_mark(self, where=None, prefix: interactive.argument=None):
        if prefix is None:
            self.mark_ring.append(self.the_mark)
            self.the_mark = self.buf.mark(where if where is not None else self.cursor)
        else:
            self.mark_ring.insert(
                0, self.buf.mark(where if where is not None else self.cursor))
            where = self.the_mark
            self.the_mark = self.mark_ring.pop()
            self.cursor = where

    @keymap.bind('Control-X Control-X')
    def exchange_point_and_mark(self):
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

    @keymap.bind('Meta-T')
    def insert_test_content(
            self, count: interactive.positive_integer_argument=80):
        import itertools
        self.insert(''.join(
            itertools.islice(itertools.cycle('1234567890'), count)))

    @keymap.bind(
        '[tab]', '[linefeed]',
        *(chr(x) for x in range(ord(' '), ord('~') + 1)))
    def self_insert(
            self,
            key: interactive.keystroke,
            count: interactive.positive_integer_argument=1):

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
        self.insert('\n' * count)

    @keymap.bind('Control-D', '[dc]')
    def delete_forward(self, count: interactive.integer_argument=1):
        if count < 0:
            moved = self.move(count)
            count = -moved
        self.delete(count)

    @keymap.bind('Control-H', 'Control-?', '[backspace]')
    def delete_backward(self, count: interactive.integer_argument=1):
        self.delete_forward(-count)

    @keymap.bind('Control-k')
    def kill_to_end_of_line(self, count: interactive.integer_argument):
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
        if arg and isinstance(arg, int):
            self.yank_state += arg - 1
        self.insert_region(self.context.yank(self.yank_state))
        if arg is not None and not isinstance(nth, int):
            self.exchange_point_and_mark()

    @keymap.bind('Meta-y')
    def yank_pop(self, arg: interactive.integer_argument=1):
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
        with self.save_excursion():
            self.insert('\n' * count)

    @keymap.bind(
        'Meta-[backspace]', 'Meta-Control-H', 'Meta-[dc]', 'Meta-[del]')
    def kill_word_backward(self, count: interactive.integer_argument=1):
        mark = self.buf.mark(self.cursor)
        self.word_backward(count)
        self.kill_region(mark, append=self.last_command.startswith('kill_'))

    @keymap.bind('Meta-d')
    def kill_word_forward(self, count: interactive.integer_argument=1):
        mark = self.buf.mark(self.cursor)
        self.word_forward(count)
        self.kill_region(mark, append=self.last_command.startswith('kill_'))

    @keymap.bind('Control-X i')
    def insert_file(self):
        filename = yield from self.read_filename('Insert File: ')
        try:
            with open(filename) as fp:
                self.insert(fp.read())
        except Exception as exc:
            self.whine(str(exc))

    @keymap.bind('Control-X Control-Q')
    def toggle_writable(self):
        self._writable = not self._writable


class LongPrompt(Editor):
    histories = {}

    def __init__(
            self,
            *args,
            prompt='',
            complete=None,
            callback=lambda x: None,
            history=None,
            **kw):
        self.divider = 0
        super().__init__(*args, **kw)
        self.prompt = prompt
        self.callback = callback
        self.complete = complete
        proto = kw.get('prototype', None)
        if proto is not None:
            self.prompt = proto.prompt
            self.callback = proto.callback
            self.complete = proto.complete
            self.divider = proto.divider
        else:
            self.cursor.point = 0
            self.insert(prompt)
            self.divider = int(self.cursor)
        self.complete_state = None
        self.end_of_buffer()
        self.histptr = 0
        self.history = self.histories.setdefault(history, [])

    def destroy(self):
        self.history.append(self.buf[self.divider:])
        super().destroy()

    @keymap.bind('Meta-p')
    def previous_history(self):
        self.move_history(-1)

    @keymap.bind('Meta-n')
    def next_history(self):
        self.move_history(1)

    def move_history(self, offset):
        new_ptr = self.histptr - offset
        if new_ptr < 0 or new_ptr > len(self.history):
            return

        old = self.buf[self.divider:]
        if self.histptr == 0:
            self.stash = old
        else:
            self.history[-self.histptr] = old

        if new_ptr == 0:
            new = self.stash
        else:
            new = self.history[-new_ptr]

        self.cursor.point = self.divider
        self.delete(len(old))
        self.insert(new)
        self.histptr = new_ptr

    def writable(self):
        return super().writable() and self.cursor >= self.divider

    @keymap.bind('Control-J', 'Control-C Control-C')
    def runcallback(self):
        self.callback(self.buf[self.divider:])

    @keymap.bind('[tab]')
    def complete(self, key: interactive.keystroke):
        if self.complete is None:
            return self.self_insert(key=key)

        if self.cursor < self.divider:
            self.whine('No completing the prompt')
            return

        if self.last_command != 'complete' or self.complete_state is None:
            self.complete_state = self.complete(
                self.buf[self.divider:self.cursor], self.buf[self.cursor:])

        try:
            left, right = next(self.complete_state)
        except StopIteration:
            self.whine('No more completions')
            self.complete_state = None
            self.replace(len(self.buf) - self.cursor.point, '')
            return

        self.log.debug('complete: %s, %s', repr(left), repr(right))

        c = self.buf.mark(self.cursor)
        self.cursor.point = self.divider
        self.replace(len(self.buf) - self.divider, left + right)
        self.cursor.point += len(left)


class ShortPrompt(LongPrompt):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.keymap['[carriage return]'] = self.runcallback


class ReplyMode:
    def __init__(self, msg):
        self.msg = msg

    @keymap.bind('Control-C Control-Y')
    def yank_original(self, window: interactive.window):
        m = window.buf.mark(window.cursor)
        prefix = '> '
        with window.save_excursion(m):
            window.insert(
                prefix + ('\n' + prefix).join(self.msg.body.splitlines()))
        window.set_mark(m)


class GapBuffer:
    CHUNKSIZE = 4096

    def __init__(self, content=None, chunksize=None):
        super().__init__()
        self.log = logging.getLogger(
            '%s.%x' % ('GapBuffer', id(self),))
        self.chunksize = chunksize or self.CHUNKSIZE
        self.marks = weakref.WeakSet()
        self.buf = self._array(self.chunksize)
        self.gapstart = 0
        self.gapend = len(self.buf)

        if content is not None:
            self.replace(0, 0, content)

    def __repr__(self):
        return '<%s size=%d:%d %d-%d>' % (
            self.__class__.__name__,
            self.size, len(self.buf),
            self.gapstart, self.gapend,
            )

    def _array(self, size):
        return array.array('u', u' ' * size)

    @property
    def size(self):
        return len(self.buf) - self.gaplength

    @property
    def text(self):
        return (
            self.buf[:self.gapstart].tounicode()
            + self.buf[self.gapend:].tounicode())

    def textrange(self, beg, end):
        beg = self.pointtopos(beg)
        end = self.pointtopos(end)
        l = []
        if beg <= self.gapstart:
            l.append(self.buf[beg:min(self.gapstart, end)].tounicode())
        if end > self.gapstart:
            l.append(self.buf[max(self.gapend, beg):end].tounicode())
        return ''.join(l)

    @property
    def gaplength(self):
        return self.gapend - self.gapstart

    def pointtopos(self, point):
        point = int(point)
        if point < 0:
            return 0
        if point <= self.gapstart:
            return point
        if point < self.size:
            return point + self.gaplength
        return len(self.buf)

    def postopoint(self, pos):
        if pos < self.gapstart:
            return pos
        elif pos <= self.gapend:
            return self.gapstart
        else:
            return pos - self.gaplength

    def movegap(self, pos, size):
        # convert marks to point coordinates
        for mark in self.marks:
            mark.pos = mark.point
        point = self.postopoint(pos)

        # expand the gap if necessary
        if size > self.gaplength:
            increase = (
                ((size - self.gaplength) // self.chunksize + 1) * self.chunksize)
            self.buf[self.gapstart:self.gapstart] = self._array(increase)
            self.gapend += increase

        pos = self.pointtopos(point)
        # okay, now we move the gap.
        if pos < self.gapstart:
            # If we're moving it towards the top of the buffer
            newend = pos + self.gaplength
            self.buf[newend:self.gapend] = self.buf[pos:self.gapstart]
            self.gapstart = pos
            self.gapend = newend
        elif pos > self.gapend:
            # towards the bottom
            newstart = pos - self.gaplength
            self.buf[self.gapstart:newstart] = self.buf[self.gapend:pos]
            self.gapstart = newstart
            self.gapend = pos
        # turns marks back to pos coordinates
        for mark in self.marks:
            mark.point = mark.pos

    def replace(self, where, size, string, collapsible=None):
        assert size >= 0
        if hasattr(where, 'pos'):
            where = where.pos
        else:
            where = self.pointtopos(where)
        length = len(string)
        self.movegap(where, length - size)
        self.gapend += size
        newstart = self.gapstart + length
        self.buf[self.gapstart:newstart] = array.array('u', string)
        self.gapstart = newstart
        return length

    def mark(self, where):
        if where is None:
            return None
        return GapMark(self, where)


class UndoableGapBuffer(GapBuffer):
    def __init__(self, *args, **kw):
        self.undolog = []
        super().__init__(*args, **kw)

    def replace(self, where, size, string, collapsible=False):
        self.log.debug('collapsible %s %d %d %s', collapsible, where, size, repr(string))
        if self.undolog:
            self.log.debug('self.undolog[-1] %s', repr(self.undolog[-1]))
        if collapsible and self.undolog \
          and where == self.undolog[-1][0] + self.undolog[-1][1] \
          and string != '' and self.undolog[-1][2] == '':
            #XXX only "collapses" inserts
            self.log.debug('collapse %s', repr(self.undolog[-1]))
            self.undolog[-1] = (self.undolog[-1][0], len(string) + self.undolog[-1][1], '')
        else:
            self.undolog.append(
                (int(where), len(string), self.textrange(where, int(where) + size)))
        return super().replace(where, size, string)

    def undo(self, which):
        if not self.undolog:
            return None
        if which is not None:
            off = which
        else:
            off = len(self.undolog) - 1
        where, size, string = self.undolog[off]
        self.replace(where, size, string)
        return (off - 1) % len(self.undolog), where + len(string)


class GapMark:
    def __init__(self, buf, point):
        self.buf = buf
        self.buf.marks.add(self)
        self.pos = self.buf.pointtopos(point)

    @property
    def point(self):
        """The value of the mark in external coordinates"""
        return self.buf.postopoint(self.pos)

    @point.setter
    def point(self, val):
        self.pos = self.buf.pointtopos(val)

    def __repr__(self):
        return '<%s %x (%x) %d (%d)>' % (
            self.__class__.__name__,
            id(self),
            id(self.buf),
            self.pos,
            self.point,
            )

    def __int__(self):
        return self.point
