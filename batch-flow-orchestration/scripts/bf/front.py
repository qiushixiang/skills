"""Chat-front command whitelist (design 6.3): one bf/cli.py invocation with a read/answer/run/stop subcommand."""
import os
import re
import shlex

COMMANDS = ('status', 'report', 'run', 'stop', 'resume', 'answer')
FLAGS = {'run': {'--parallel': 1, '--profile': 1}, 'answer': {'--file': 1, '--approve': 0, '--reject': 0}}


def allowed(command):
    if re.search(r'[;&|<>`$()\n\\]', command or ''):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) < 5 or not os.path.basename(words[0]).startswith('python'):
        return False
    cli = os.path.normpath(words[1])
    if os.path.basename(cli) != 'cli.py' or os.path.basename(os.path.dirname(cli)) != 'bf':
        return False
    if words[2] != '--base' or words[4] not in COMMANDS:
        return False
    rest, flags, i = words[5:], FLAGS.get(words[4], {}), 0
    if words[4] == 'answer':
        if not rest or not re.match(r'^WO-\d+$', rest[0]):
            return False
        rest = rest[1:]
    while i < len(rest):
        if rest[i] not in flags:
            return False
        i += 1 + flags[rest[i]]
    return i == len(rest)
