"""Pause local Claude Code CLIs before system sleep, resume only on full wake.

Uses IOKit system graphics capability, not display-on state: locking the
screen or turning the display off during normal operation does not pause work.
No account changes, quota checks, credentials, GUI operations, or API calls.
"""
import argparse
import ctypes as C
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

HOME = Path.home()
STATE = HOME / '.local/state/claude-sleep-guard/state.json'
GRAPHICS = 2  # IOPM.h: kIOPMSystemCapabilityGraphics


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Power:
    """Thin ctypes binding to the system power API; no sudo required."""
    def __init__(self):
        self.io = C.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        signatures = {
            'IOServiceMatching': (C.c_void_p, [C.c_char_p]),
            'IOServiceGetMatchingService': (C.c_uint, [C.c_uint, C.c_void_p]),
            'IORegistryEntryCreateCFProperty': (C.c_void_p, [C.c_uint, C.c_void_p, C.c_void_p, C.c_uint]),
            'IOObjectRelease': (C.c_int, [C.c_uint]),
            'IOAllowPowerChange': (C.c_int, [C.c_uint, C.c_long]),
            'IONotificationPortGetRunLoopSource': (C.c_void_p, [C.c_void_p]),
        }
        for name, (ret, args) in signatures.items():
            fn = getattr(self.io, name); fn.restype = ret; fn.argtypes = args
        self.cf.CFStringCreateWithCString.restype = C.c_void_p
        self.cf.CFStringCreateWithCString.argtypes = [C.c_void_p, C.c_char_p, C.c_uint]
        self.cf.CFNumberGetValue.argtypes = [C.c_void_p, C.c_int, C.c_void_p]
        self.cf.CFRelease.argtypes = [C.c_void_p]
        self.domain = self.io.IOServiceGetMatchingService(0, self.io.IOServiceMatching(b'IOPMrootDomain'))
        if not self.domain:
            raise RuntimeError('IOPMrootDomain unavailable')
        self.key = self.cf.CFStringCreateWithCString(None, b'System Capabilities', 0x08000100)

    def full_wake(self):
        prop = self.io.IORegistryEntryCreateCFProperty(self.domain, self.key, None, 0)
        if not prop:
            return False  # Fail closed during transitions.
        number = C.c_longlong()
        ok = self.cf.CFNumberGetValue(prop, 4, C.byref(number))
        self.cf.CFRelease(prop)
        return bool(ok and number.value & GRAPHICS)

    def run(self, guard):
        callback_type = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_void_p)
        port = C.c_void_p(); notifier = C.c_uint()
        connection = 0

        def callback(ref, service, kind, arg):
            # iokit_common_msg = 0xe0000000 | message number (IOMessage.h).
            try:
                if kind == 0xe0000280:  # WillSleep: freeze before acknowledging.
                    guard.sleep_pending = True
                    guard.tick(sleeping=True)
                elif kind == 0xe0000300:  # HasPoweredOn: DarkWake must stay held.
                    guard.sleep_pending = False
                    guard.tick()
                elif kind == 0xe0000290:  # WillNotSleep: an idle sleep was cancelled.
                    guard.sleep_pending = False
                    guard.tick()
            except Exception as exc:
                print('claude-sleep-guard power callback: ' + type(exc).__name__, file=sys.stderr, flush=True)
            finally:
                if kind in (0xe0000270, 0xe0000280):
                    self.io.IOAllowPowerChange(connection, int(arg or 0))

        cb = callback_type(callback)
        self.io.IORegisterForSystemPower.restype = C.c_uint
        self.io.IORegisterForSystemPower.argtypes = [C.c_void_p, C.POINTER(C.c_void_p), callback_type, C.POINTER(C.c_uint)]
        connection = self.io.IORegisterForSystemPower(None, C.byref(port), cb, C.byref(notifier))
        if not connection:
            raise RuntimeError('Cannot register system sleep listener')
        cf = self.cf
        cf.CFRunLoopGetCurrent.restype = C.c_void_p
        cf.CFRunLoopAddSource.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p]
        mode = C.c_void_p.in_dll(cf, 'kCFRunLoopDefaultMode').value
        loop = cf.CFRunLoopGetCurrent()
        cf.CFRunLoopAddSource(loop, self.io.IONotificationPortGetRunLoopSource(port), mode)
        timer_type = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p)

        def on_timer(timer, info):
            try:
                guard.tick()
            except Exception as exc:
                print('claude-sleep-guard tick: ' + type(exc).__name__, file=sys.stderr, flush=True)

        timer_cb = timer_type(on_timer)
        cf.CFAbsoluteTimeGetCurrent.restype = C.c_double
        cf.CFRunLoopTimerCreate.restype = C.c_void_p
        cf.CFRunLoopTimerCreate.argtypes = [C.c_void_p, C.c_double, C.c_double, C.c_ulong, C.c_long, timer_type, C.c_void_p]
        cf.CFRunLoopAddTimer.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p]
        timer = cf.CFRunLoopTimerCreate(None, cf.CFAbsoluteTimeGetCurrent(), 5, 0, 0, timer_cb, None)
        cf.CFRunLoopAddTimer(loop, timer, mode)
        guard.tick()
        cf.CFRunLoopRun()


def processes():
    """Identify CLI binaries by full executable path, never GUI helpers."""
    lib = C.CDLL('/usr/lib/libproc.dylib')
    lib.proc_pidpath.argtypes = [C.c_int, C.c_void_p, C.c_uint]
    output = subprocess.check_output(['/bin/ps', '-axo', 'pid=,lstart=,stat=,comm='], text=True)
    found = {}
    for line in output.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8 or Path(parts[7]).name != 'claude':
            continue
        pid = int(parts[0]); buf = C.create_string_buffer(4096)
        if lib.proc_pidpath(pid, buf, len(buf)) <= 0:
            continue
        executable = buf.value.decode(errors='replace')
        if not (executable.startswith(str(HOME / '.local/share/claude/versions') + '/')
                or '/Cellar/claude-code/' in executable):
            continue
        found[str(pid)] = {'start': ' '.join(parts[1:6]), 'stopped': 'T' in parts[6]}
    return found


class Guard:
    def __init__(self, power):
        self.power = power
        self.state = read_json(STATE)
        self.state.setdefault('held', {})
        self.sleep_pending = False

    def tick(self, sleeping=False):
        full = not sleeping and not self.sleep_pending and self.power.full_wake()
        found = processes()
        changed = False
        for pid, proc in found.items():
            held = self.state['held'].get(pid)
            if held and held['start'] != proc['start']:
                self.state['held'].pop(pid); held = None; changed = True
            if not full and held and not proc['stopped']:
                try:
                    os.kill(int(pid), signal.SIGSTOP)
                except ProcessLookupError:
                    pass
            elif not full and not held and not proc['stopped']:
                self.state['held'][pid] = {'start': proc['start']}
                save_json(STATE, self.state)
                try:
                    os.kill(int(pid), signal.SIGSTOP)
                    print('Paused Claude PID %s for system sleep / DarkWake' % pid, flush=True)
                except ProcessLookupError:
                    self.state['held'].pop(pid, None)
                changed = True
            elif full and held:
                try:
                    os.kill(int(pid), signal.SIGCONT)
                    print('Resumed Claude PID %s after full wake' % pid, flush=True)
                except ProcessLookupError:
                    pass
                self.state['held'].pop(pid, None); changed = True
        for pid in list(self.state['held']):
            if pid not in found:
                self.state['held'].pop(pid); changed = True
        if changed or self.state.get('fullWake') != full:
            self.state['fullWake'] = full
            self.state['checkedAt'] = time.time()
            save_json(STATE, self.state)


def hook():
    # Fallback for a CLI launched during DarkWake. No hook input is read.
    if not Power().full_wake():
        print(json.dumps({'continue': False,
                          'stopReason': 'Claude paused during DarkWake. Continue after a normal full wake.'}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['daemon', 'hook', 'status'])
    args = parser.parse_args()
    if args.command == 'hook':
        hook()
    elif args.command == 'status':
        print(json.dumps({'state': read_json(STATE), 'fullWake': Power().full_wake(),
                          'claudeProcesses': list(processes())}, indent=2))
    else:
        STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (STATE.parent / 'daemon.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            power = Power()
            print('System sleep listener starting', flush=True)
            power.run(Guard(power))


if __name__ == '__main__':
    main()
