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
snipe.main
----------
'''

import asyncio
import logging
import signal

from . import ttyfe
from . import context


def main():
    '''Main function, does high-level setup and kicks off the main loop.'''
    try:
        handler = context.SnipeLogHandler(logging.DEBUG)
        handler.setFormatter(logging.Formatter(
            '%(asctime)s.%(msecs)03d %(name)s: %(message)s', '%b %d %H:%M:%S'))
        logging.getLogger().addHandler(handler)
        signal.signal(signal.SIGUSR1, handler.dump)
        log = logging.getLogger('Snipe')
        log.warning('snipe starting')
        with ttyfe.TTYFrontend() as ui:
            context_ = context.Context(ui, handler)
            loop = asyncio.get_event_loop()
            loop.set_debug(True)
            loop.add_reader(0, ui.readable)
            ui.redisplay()
            loop.run_forever()
        print()
        context_.shutdown()
        log.warning('snipe ends')
    finally:
        if handler.writing:
            handler.dump()
        logging.shutdown()

if __name__ == '__main__':
    main()
