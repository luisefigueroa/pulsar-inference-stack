"""Invocation-owned node processes and a bounded control channel over stdin.

Bash chooses the node and builds the existing SSH command. This helper only
frames that command's input, supervises its children, and reports cancellation.
No durable jobs, model policy, service stop, or process-name matching lives here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import secrets
import select
import signal
import subprocess
import sys
import tempfile
import time

HEARTBEAT_SECONDS = 2.0
LEASE_SECONDS = 30.0
GRACE_SECONDS = 2.0
REAP_SECONDS = 1.0
MAX_PROGRAM_BYTES = 64 * 1024 * 1024
OWNER_ENV = 'PULSAR_VERIFICATION_OWNER'
REPORT_ENV = 'PULSAR_VERIFICATION_REPORT'
REPORT_FD_ENV = 'PULSAR_VERIFICATION_REPORT_FD'
FAILURE_ENV = 'PULSAR_VERIFICATION_BATCH_FAILURE'

# Only this fixed, small bootstrap is passed in argv. Bundled code and requests
# remain on stdin, followed by the live control channel. Unbuffered reads must
# not consume a heartbeat while reading the program frame.
BOOTSTRAP = '''import os,select
_pulsar_control=os.fdopen(os.dup(0),'rb',buffering=0)
def _pulsar_read(size):
 if not select.select([_pulsar_control],[],[],30.0)[0]: raise SystemExit('node frame lease expired')
 data=_pulsar_control.read(size)
 if not data: raise SystemExit('incomplete node frame')
 return data
_line=b''
while not _line.endswith(b'\\n') and len(_line)<128: _line+=_pulsar_read(1)
if not _line.endswith(b'\\n'): raise SystemExit('invalid node frame')
_header=_line.decode('ascii').split()
if len(_header)!=2: raise SystemExit('invalid node frame')
_size=int(_header[0]); _pulsar_token=_header[1]
if not 0<_size<=67108864 or len(_pulsar_token)!=32 or any(c not in '0123456789abcdef' for c in _pulsar_token): raise SystemExit('invalid node frame')
_parts=[]
while _size:
 _part=_pulsar_read(min(_size,65536))
 _parts.append(_part); _size-=len(_part)
exec(compile(b''.join(_parts),'<pulsar-node>','exec'))
'''


class Cancelled(RuntimeError):
    def __init__(self, message, *, signum=signal.SIGTERM, confirmed=True, diagnostic=''):
        super().__init__(message)
        self.exit_code = 128 + signum
        self.confirmed = confirmed
        self.diagnostic = diagnostic


@contextmanager
def cancellation_signals():
    state = {'signal': None}
    def stop(signum, _frame):
        if state['signal'] is None:
            state['signal'] = signum
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield state
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def process_identity(pid):
    """PID plus Linux start time; a reused PID never renews another invocation."""
    try:
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return [int(pid), fields[19]]
    except (OSError, ValueError, TypeError, IndexError):
        return None


def owner_environment(env=None):
    value = dict(os.environ if env is None else env)
    value.setdefault(OWNER_ENV, json.dumps(process_identity(os.getpid())))
    return value


def parent_death_guard(parent):
    """The direct worker must not outlive a SIGKILL of its supervisor."""
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'cannot establish worker parent-death guard')
    if os.getppid() != parent:
        os.kill(os.getpid(), signal.SIGKILL)


def own_descendants():
    # Reap grandchildren adopted after an owned group leader exits. This is
    # scoped to the short-lived supervisor, never a daemon or external service.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'cannot establish child reaping')


def reap_group(pid):
    _, status = os.waitpid(pid, 0)
    while True:
        try:
            child, _ = os.waitpid(-pid, os.WNOHANG)
        except ChildProcessError:
            break
        if child == 0:
            break
    return os.waitstatus_to_exitcode(status)


def exited(pid):
    # WNOWAIT leaves the child PID reserved until cleanup has finished. Signals
    # cannot accidentally target a subsequently reused process-group identity.
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def signal_group(pid, signum):
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


def wait_exit(pid, duration):
    deadline = time.monotonic() + duration
    while not exited(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(.02)
    return True


def group_active(pid):
    """Only members of our reserved session/group; zombies are not running."""
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pid and fields[0] != 'Z':
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def stop_worker(pid, *, grace=GRACE_SECONDS, reap=REAP_SECONDS):
    signal_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while group_active(pid) and time.monotonic() < deadline:
        time.sleep(.02)
    # Keep the unreaped leader reserved while checking remaining group members.
    signal_group(pid, signal.SIGKILL)
    deadline = time.monotonic() + reap
    while (not exited(pid) or group_active(pid)) and time.monotonic() < deadline:
        time.sleep(.02)
    if not exited(pid) or group_active(pid):
        return False
    reap_group(pid)
    return True


def finish_worker(pid):
    signal_group(pid, signal.SIGKILL)
    deadline = time.monotonic() + REAP_SECONDS
    while group_active(pid) and time.monotonic() < deadline:
        time.sleep(.02)
    if group_active(pid):
        raise Cancelled('worker cleanup unconfirmed', confirmed=False)
    return reap_group(pid)


def report_worker(token, state):
    prefix = os.environ.get(REPORT_ENV)
    if prefix:
        line = f'{prefix}:{token}:{state}\n'
        if REPORT_FD_ENV in os.environ:
            os.write(int(os.environ[REPORT_FD_ENV]), line.encode())
        else:
            print(line, file=sys.stderr, end='', flush=True)


def worker_reports(diagnostic, prefix):
    states, lines = {}, []
    for line in diagnostic.splitlines():
        if line.startswith(prefix + ':'):
            fields = line[len(prefix)+1:].split(':')
            if len(fields) == 2 and len(fields[0]) == 32 and fields[1] in ('started', 'confirmed', 'cancelled', 'peer_cancelled', 'incomplete'):
                states[fields[0]] = fields[1]
                continue
        lines.append(line)
    return states, '\n'.join(lines)


def control_event(control_fd, timeout):
    if not select.select([control_fd], [], [], timeout)[0]:
        return None
    data = os.read(control_fd, 4096)
    if not data or b'!' in data:
        return 'controller disconnected'
    if any(value != ord('.') for value in data):
        return 'invalid control message'
    return 'heartbeat'


def supervise_node(main, control_fd, token, *, lease=LEASE_SECONDS, grace=GRACE_SECONDS):
    """Run exactly one node operation; EOF, lease expiry or signals cancel it."""
    own_descendants()
    with cancellation_signals() as cancelled:
        deadline = time.monotonic() + lease
        while True:
            event = control_event(control_fd, min(.1, max(0, deadline-time.monotonic())))
            if event == 'heartbeat':
                # Drain a queued EOF as well: a dead caller must not start work.
                while event == 'heartbeat':
                    event = control_event(control_fd, 0)
                if event is None and cancelled['signal'] is None:
                    break
            if event is not None or cancelled['signal'] is not None or time.monotonic() >= deadline:
                print(f'pulsar-verifier:{token}:cancelled', file=sys.stderr, flush=True)
                print('verification cancelled before worker launch', file=sys.stderr, flush=True)
                return 128 + (cancelled['signal'] or signal.SIGTERM)
        ready_read, ready_write = os.pipe()
        parent = os.getpid()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(ready_read)
                parent_death_guard(parent)
                os.setsid()
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    signal.signal(sig, signal.SIG_DFL)
                os.close(control_fd)
                os.write(ready_write, b'1'); os.close(ready_write)
                rc = main()
                sys.stdout.flush(); sys.stderr.flush()
                os._exit(rc or 0)
            except BaseException:
                import traceback
                traceback.print_exc(); sys.stderr.flush()
                os._exit(2)
        os.close(ready_write)
        try:
            ready = os.read(ready_read, 1)
        finally:
            os.close(ready_read)
        if ready != b'1':
            os.waitpid(pid, 0)
            print(f'pulsar-verifier:{token}:confirmed', file=sys.stderr, flush=True)
            return 2
        deadline = time.monotonic() + lease
        reason = None
        while True:
            if cancelled['signal'] is not None:
                reason = 'interrupted'
                break
            if exited(pid):
                try:
                    code = finish_worker(pid)
                except Cancelled:
                    print(f'pulsar-verifier:{token}:incomplete', file=sys.stderr, flush=True)
                    return 4
                print(f'pulsar-verifier:{token}:confirmed', file=sys.stderr, flush=True)
                return code if code >= 0 else 128 - code
            if time.monotonic() >= deadline:
                reason = 'controller lease expired'
                break
            event = control_event(control_fd, min(.1, max(0, deadline-time.monotonic())))
            if event == 'heartbeat':
                deadline = time.monotonic() + lease
            elif event is not None:
                reason = event
                break
        confirmed = stop_worker(pid, grace=grace)
        state = 'confirmed' if confirmed else 'incomplete'
        receipt = 'cancelled' if confirmed else 'incomplete'
        print(f'pulsar-verifier:{token}:{receipt}', file=sys.stderr, flush=True)
        print(f'verification cancelled: {reason}; worker cleanup {state}', file=sys.stderr, flush=True)
        return 128 + (cancelled['signal'] or signal.SIGTERM) if confirmed else 4


def run_transport(command, program, *, owners=(), heartbeat=HEARTBEAT_SECONDS):
    if not program or len(program) > MAX_PROGRAM_BYTES:
        raise ValueError('invalid node program size')
    own_descendants()
    token = secrets.token_hex(16)
    frame = f'{len(program)} {token}\n'.encode() + program
    pending = memoryview(frame)
    with cancellation_signals() as cancelled, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        if cancelled['signal'] is not None or any(identity is None or process_identity(identity[0]) != identity for identity in owners):
            raise Cancelled('verification cancelled before launch', signum=cancelled['signal'] or signal.SIGHUP)
        parent = os.getpid()
        report_worker(token, 'started')
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                       start_new_session=True, preexec_fn=lambda: parent_death_guard(parent))
        except BaseException:
            report_worker(token, 'confirmed')  # Popen did not leave a started command.
            raise
        next_beat = time.monotonic()
        reason = None
        try:
            os.set_blocking(process.stdin.fileno(), False)
            while True:
                if cancelled['signal'] is not None:
                    reason = 'interrupted'; break
                if any(identity is None or process_identity(identity[0]) != identity for identity in owners):
                    reason = 'caller exited'; break
                if exited(process.pid):
                    break
                if not pending and time.monotonic() >= next_beat:
                    pending = memoryview(b'.')
                    next_beat = time.monotonic() + heartbeat
                if pending:
                    _, writable, _ = select.select([], [process.stdin.fileno()], [], .05)
                    if writable:
                        try:
                            pending = pending[os.write(process.stdin.fileno(), pending[:65536]):]
                        except BrokenPipeError:
                            reason = 'control channel closed'; break
                        except BlockingIOError:
                            pass
                else:
                    time.sleep(.05)
        except BaseException:
            process.stdin.close()
            if stop_worker(process.pid):
                process.returncode = 128 + signal.SIGTERM
            raise
        finally:
            process.stdin.close()  # Remote supervisor sees EOF even if SSH stays alive.
        if reason is not None:
            # Allow the node's 2-second grace plus transport overhead. Local
            # cleanup fits inside Workbench's existing 5-second escalation.
            if not wait_exit(process.pid, GRACE_SECONDS + .5):
                signal_group(process.pid, signal.SIGTERM)
                if not wait_exit(process.pid, .25):
                    signal_group(process.pid, signal.SIGKILL)
                    if not wait_exit(process.pid, REAP_SECONDS):
                        raise Cancelled('verification cancelled; local cleanup unconfirmed', confirmed=False)
        code = finish_worker(process.pid)
        process.returncode = code
        stdout.seek(0); stderr.seek(0)
        output, diagnostic = stdout.read(), stderr.read().decode(errors='replace')
        marker = f'pulsar-verifier:{token}:'
        receipts = [line[len(marker):] for line in diagnostic.splitlines() if line.startswith(marker)]
        diagnostic = '\n'.join(line for line in diagnostic.splitlines() if not line.startswith(marker))
        confirmed = any(value in ('confirmed','cancelled') for value in receipts)
        interrupted = reason not in (None,'control channel closed') or 'cancelled' in receipts
        state = 'cancelled' if interrupted else 'confirmed'
        if interrupted and os.environ.get(FAILURE_ENV) and Path(os.environ[FAILURE_ENV]).exists():
            state = 'peer_cancelled'
        report_worker(token, state if confirmed else 'incomplete')
        if reason is not None and not (reason == 'control channel closed' and confirmed):
            detail = 'worker cleanup confirmed' if confirmed else 'worker cleanup unconfirmed; remote lease expires after 30 seconds without renewal'
            raise Cancelled(f'verification cancelled: {reason}; {detail}',
                            signum=cancelled['signal'] or signal.SIGHUP, confirmed=confirmed)
        if code == 0 and not confirmed:
            raise Cancelled('node operation ended without a worker completion receipt; cleanup unconfirmed', confirmed=False)
        if not confirmed:
            diagnostic += '\nworker cleanup unconfirmed; a disconnected node expires its lease after 30 seconds'
        return subprocess.CompletedProcess(command, code, output, diagnostic)


def run_batch(tasks, directory, *, jobs=3, owners=()):
    """Schedule supplied transports; one per node, no new work after failure.

    Bash supplies every argv and framed program. Per-job files contain data;
    live ownership receipts bypass those files so an interrupted outer command
    still knows which remote workers require confirmed cleanup.
    """
    if type(jobs) is not int or jobs < 1:
        raise ValueError('verification jobs must be a positive integer')
    if [task['index'] for task in tasks] != list(range(len(tasks))):
        raise ValueError('verification tasks must have unique canonical indexes')
    own_descendants()
    root = Path(directory)
    failure = root/'failed'
    if failure.exists():
        raise ValueError('verification batch directory has already been used')
    report_fd = os.dup(int(os.environ.get(REPORT_FD_ENV, sys.stderr.fileno())))
    environment = {**os.environ, REPORT_FD_ENV: str(report_fd), FAILURE_ENV: str(failure)}
    pending = list(tasks)
    active = {}
    client_tokens = {}
    results = {}
    first_error = None
    deadline = None
    owner_dead = False
    parent = os.getpid()

    def cancel_peers():
        for process, _task in active.values():
            signal_group(process.pid, signal.SIGTERM)

    def failed(index, code, error):
        nonlocal first_error, deadline
        if first_error is None:
            first_error = index
            failure.touch(exist_ok=False)
            deadline = time.monotonic() + GRACE_SECONDS + 2
            cancel_peers()
        results[index] = {'index': index, 'returncode': code, 'error': error}

    try:
        with cancellation_signals() as cancelled:
            while pending or active:
                owner_dead = any(identity is None or process_identity(identity[0]) != identity for identity in owners)
                if (cancelled['signal'] is not None or owner_dead) and deadline is None:
                    deadline = time.monotonic() + GRACE_SECONDS + 2
                    cancel_peers()
                # Collect before launching: an observed failure prevents new work.
                for index, (process, task) in list(active.items()):
                    if not exited(process.pid):
                        if deadline is None or time.monotonic() < deadline:
                            continue
                        confirmed = stop_worker(process.pid, grace=0)
                        code = 4
                        error = 'verification client did not finish cleanup; worker cleanup unconfirmed'
                        if confirmed: process.returncode = 128 + signal.SIGKILL
                        report_worker(client_tokens[index], 'incomplete')
                    else:
                        try:
                            code = finish_worker(process.pid)
                            process.returncode = code
                            code = code if code >= 0 else 128-code
                            error = (root/'jobs'/f'{index}.err').read_text().strip()
                            report_worker(client_tokens[index], 'confirmed')
                        except Cancelled as exc:
                            code, error = 4, str(exc)
                            report_worker(client_tokens[index], 'incomplete')
                    del active[index]
                    if code:
                        # A user interruption is different from cleanup following
                        # a failed hash. Do not manufacture a peer-failure latch.
                        if deadline is None:
                            failed(index, code, error)
                        else:
                            results[index] = {'index': index, 'returncode': code, 'error': error}
                    else:
                        from .state import now
                        results[index] = {'index': index, 'returncode': 0, 'verified_at': now()}
                if deadline is not None:
                    if not active: break
                else:
                    busy = {task['node_slot'] for _, task in active.values()}
                    for task in list(pending):
                        if len(active) >= jobs: break
                        if task['node_slot'] in busy: continue
                        pending.remove(task)
                        index = task['index']
                        client_tokens[index] = secrets.token_hex(16)
                        report_worker(client_tokens[index], 'started')
                        try:
                            with open(task['program'], 'rb') as program, \
                                 (root/'jobs'/f'{index}.out').open('wb') as stdout, \
                                 (root/'jobs'/f'{index}.err').open('wb') as stderr:
                                process = subprocess.Popen(task['command'], stdin=program, stdout=stdout,
                                    stderr=stderr, env=environment, pass_fds=(report_fd,), start_new_session=True,
                                    preexec_fn=lambda: parent_death_guard(parent))
                            active[index] = (process, task)
                            busy.add(task['node_slot'])
                        except OSError as exc:
                            report_worker(client_tokens[index], 'confirmed')
                            failed(index, 2, str(exc)); break
                if active: time.sleep(.02)
            for task in pending:
                results[task['index']] = {'index': task['index'], 'returncode': 125,
                    'error': 'verification not started after batch failure or cancellation'}
            code = (128 + (cancelled['signal'] or signal.SIGHUP)) if cancelled['signal'] or owner_dead else 0
            if not code and first_error is not None:
                code = results[first_error]['returncode']
            report = {'returncode': code, 'first_error': first_error,
                      'results': [results[index] for index in range(len(tasks))]}
            (root/'batch.json').write_text(json.dumps(report))
            return code
    finally:
        # Unexpected exceptions must close ownership, too. Transport clients
        # close the remote control channel before their bounded local exit.
        cancel_peers()
        for index, (process, _task) in active.items():
            if wait_exit(process.pid, GRACE_SECONDS + 2):
                process.returncode = finish_worker(process.pid)
                report_worker(client_tokens[index], 'confirmed')
            elif stop_worker(process.pid, grace=0):
                process.returncode = 128 + signal.SIGKILL
                report_worker(client_tokens[index], 'incomplete')
        os.close(report_fd)


def run_command(command, *, env=None, cwd=None):
    """Public-envelope child ownership; never invokes a service-stop command."""
    own_descendants()
    with cancellation_signals() as cancelled, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        parent = os.getpid()
        prefix = 'pulsar-verification-owner-' + secrets.token_hex(16)
        environment = owner_environment(env)
        environment[REPORT_ENV] = prefix
        process = subprocess.Popen(command, env=environment, cwd=cwd,
                                   stdout=stdout, stderr=stderr, start_new_session=True,
                                   preexec_fn=lambda: parent_death_guard(parent))
        try:
            while not exited(process.pid) and cancelled['signal'] is None:
                time.sleep(.02)
            if cancelled['signal'] is not None:
                confirmed = stop_worker(process.pid, grace=4.0, reap=.5)
                if confirmed:
                    process.returncode = 128 + cancelled['signal']
                stderr.seek(0)
                states, _ = worker_reports(stderr.read().decode(errors='replace'), prefix)
                verified = confirmed and all(value in ('confirmed','cancelled','peer_cancelled') for value in states.values())
                detail = 'local command and tracked node workers reaped' if verified else 'worker cleanup unconfirmed; a disconnected node expires its lease after 30 seconds'
                raise Cancelled('Stack command cancelled; '+detail,
                                signum=cancelled['signal'], confirmed=verified)
            code = finish_worker(process.pid)
            process.returncode = code
        except Cancelled:
            raise
        except BaseException:
            if stop_worker(process.pid, grace=4.0, reap=.5):
                process.returncode = 128 + signal.SIGTERM
            raise
        stdout.seek(0); stderr.seek(0)
        states, diagnostic = worker_reports(stderr.read().decode(errors='replace'), prefix)
        if any(value not in ('confirmed','cancelled','peer_cancelled') for value in states.values()):
            raise Cancelled('Stack command ended with unconfirmed worker cleanup', confirmed=False, diagnostic=diagnostic)
        if 'cancelled' in states.values():
            raise Cancelled('Stack verification cancelled; tracked node workers reaped')
        if code == 0 and 'peer_cancelled' in states.values():
            raise Cancelled('Stack verification did not cover the complete prepared set', diagnostic=diagnostic)
        return subprocess.CompletedProcess(command, code, stdout.read().decode(errors='replace'),
                                           diagnostic)


def main():
    if sys.argv[1:2] == ['batch']:
        parser = argparse.ArgumentParser(description='Run an invocation-owned verification batch')
        parser.add_argument('batch'); parser.add_argument('--tasks', required=True)
        parser.add_argument('--directory', required=True); parser.add_argument('--jobs', type=int, default=3)
        args = parser.parse_args()
        owners = [process_identity(os.getppid())]
        if OWNER_ENV in os.environ: owners.append(json.loads(os.environ[OWNER_ENV]))
        return run_batch(json.loads(Path(args.tasks).read_text()), args.directory, jobs=args.jobs, owners=owners)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', type=int, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    owners = [process_identity(args.owner), process_identity(os.getppid())]
    if OWNER_ENV in os.environ:
        owners.append(json.loads(os.environ[OWNER_ENV]))
    try:
        result = run_transport(command, sys.stdin.buffer.read(), owners=owners)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.returncode == 0:
            sys.stdout.buffer.write(result.stdout)
        return result.returncode if result.returncode >= 0 else 128-result.returncode
    except Cancelled as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code if exc.confirmed else 4
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'verification transport: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
