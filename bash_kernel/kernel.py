from ipykernel.kernelbase import Kernel
from pexpect import replwrap, EOF
import pexpect

from subprocess import check_output
from os import unlink

import base64
import imghdr
import re
import signal
import urllib
import os

__version__ = '0.2'

version_pat = re.compile(r'version (\d+(\.\d+)+)')

from .images import (
    extract_image_filenames, display_data_for_image, image_setup_cmd
)

class CREPLWrapper(replwrap.REPLWrapper):
    def __init__(self, cmd_or_spawn, orig_prompt, prompt_change,
                 new_prompt=replwrap.PEXPECT_PROMPT,
                 continuation_prompt=replwrap.PEXPECT_CONTINUATION_PROMPT,
                 extra_init_cmd=None, bkernelp=None):
        self.bkernel = bkernelp
        replwrap.REPLWrapper.__init__(self, cmd_or_spawn, orig_prompt, prompt_change,
                                      new_prompt, continuation_prompt, extra_init_cmd)

    def _expect_prompt(self, timeout=-1):
        if timeout == None:
            # Only one run_command below uses timeout=None, and it should receive continous output
            while True:
                pos = self.child.expect_exact([self.prompt, self.continuation_prompt, '\r\n'],
                                              timeout=None)
                if pos == 2:
                    # if end of line, immediately send output so far
                    self.bkernel.process_output(self.child.before + '\n')
                else:
                    break
        else:
            # The other run_commands use other timeout values, and all output should be collected
            pos = self.child.expect_exact([self.prompt, self.continuation_prompt],
                                          timeout=None)
        return pos

class BashKernel(Kernel):
    implementation = 'bash_kernel'
    implementation_version = __version__

    @property
    def language_version(self):
        m = version_pat.search(self.banner)
        return m.group(1)

    _banner = None

    @property
    def banner(self):
        if self._banner is None:
            self._banner = check_output(['bash', '--version']).decode('utf-8')
        return self._banner

    language_info = {'name': 'bash',
                     'codemirror_mode': 'shell',
                     'mimetype': 'text/x-sh',
                     'file_extension': '.sh'}

    def __init__(self, **kwargs):
        Kernel.__init__(self, **kwargs)
        self._start_bash()

    def _start_bash(self):
        # Signal handlers are inherited by forked processes, and we can't easily
        # reset it from the subprocess. Since kernelapp ignores SIGINT except in
        # message handlers, we need to temporarily reset the SIGINT handler here
        # so that bash and its children are interruptible.
        sig = signal.signal(signal.SIGINT, signal.SIG_DFL)
        try:
            bashrc = os.path.join(os.path.dirname(pexpect.__file__), 'bashrc.sh')
            child = pexpect.spawn("bash", ['--rcfile', bashrc], echo=False,
                                  encoding='utf-8')
            self.bashwrapper = CREPLWrapper(child,
                                            u'\$', u"PS1='{0}' PS2='{1}' PROMPT_COMMAND=''",
                                            extra_init_cmd="export PAGER=cat", bkernelp=self)
        finally:
            signal.signal(signal.SIGINT, sig)

        # Register Bash function to write image data to temporary file
        self.bashwrapper.run_command(image_setup_cmd)

    def process_output(self, output):
        if not self.silent:
            image_filenames, output = extract_image_filenames(output)

            # Send standard output
            stream_content = {'name': 'stdout', 'text': output}
            self.send_response(self.iopub_socket, 'stream', stream_content)

            # Send images, if any
            for filename in image_filenames:
                try:
                    data = display_data_for_image(filename)
                except ValueError as e:
                    message = {'name': 'stdout', 'text': str(e)}
                    self.send_response(self.iopub_socket, 'stream', message)
                else:
                    self.send_response(self.iopub_socket, 'display_data', data)

        
    def do_execute(self, code, silent, store_history=True,
                   user_expressions=None, allow_stdin=False):
        self.silent = silent
        if not code.strip():
            return {'status': 'ok', 'execution_count': self.execution_count,
                    'payload': [], 'user_expressions': {}}

        interrupted = False
        try:
            output = self.bashwrapper.run_command(code.rstrip(), timeout=None)
        except KeyboardInterrupt:
            self.bashwrapper.child.sendintr()
            interrupted = True
            self.bashwrapper._expect_prompt()
            output = self.bashwrapper.child.before
        except EOF:
            output = self.bashwrapper.child.before + 'Restarting Bash'
            self._start_bash()

        self.process_output(output)

        if interrupted:
            return {'status': 'abort', 'execution_count': self.execution_count}

        try:
            exitcode = int(self.bashwrapper.run_command('echo $?').rstrip())
        except Exception:
            exitcode = 1

        if exitcode:
            error_content = {'execution_count': self.execution_count,
                             'ename': '', 'evalue': str(exitcode), 'traceback': []}

            self.send_response(self.iopub_socket, 'error', error_content)
            error_content['status'] = 'error'
            return error_content
        else:
            return {'status': 'ok', 'execution_count': self.execution_count,
                    'payload': [], 'user_expressions': {}}

    def do_complete(self, code, cursor_pos):
        code = code[:cursor_pos]
        default = {'matches': [], 'cursor_start': 0,
                   'cursor_end': cursor_pos, 'metadata': dict(),
                   'status': 'ok'}

        if not code or code[-1] == ' ':
            return default

        tokens = code.replace(';', ' ').split()
        if not tokens:
            return default

        matches = []
        token = tokens[-1]
        start = cursor_pos - len(token)

        if token[0] == '$':
            # complete variables
            cmd = 'compgen -A arrayvar -A export -A variable %s' % token[1:] # strip leading $
            output = self.bashwrapper.run_command(cmd).rstrip()
            completions = set(output.split())
            # append matches including leading $
            matches.extend(['$'+c for c in completions])
        else:
            # complete functions and builtins
            cmd = 'compgen -cdfa %s' % token
            output = self.bashwrapper.run_command(cmd).rstrip()
            matches.extend(output.split())

        if not matches:
            return default
        matches = [m for m in matches if m.startswith(token)]

        return {'matches': sorted(matches), 'cursor_start': start,
                'cursor_end': cursor_pos, 'metadata': dict(),
                'status': 'ok'}


