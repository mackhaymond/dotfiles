"""Pause local coding-agent trees only across actual system sleep.

CPU-target loss arms a durable hold before acknowledging sleep. Incidental
wakes retain it until generic OS metadata identifies an interactive full wake.
Display-off/headless operation without system sleep never arms the guard.
"""
import argparse
import ctypes as C
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

HOME = Path.home()
STATE = HOME / '.local/state/claude-sleep-guard/state.json'
CPU, GRAPHICS = 1, 2  # Shared by registry and IOPMConnection capability masks.
WILL_SLEEP, HAS_POWERED_ON, WILL_NOT_SLEEP = 0xe0000280, 0xe0000300, 0xe0000290


INTERACTIVE_WAKE_TYPES = {'User', 'HID Activity', 'Lid Open'}
BACKGROUND_WAKE_TYPES = {'SleepService', 'Maintenance', 'SleepTimer', 'Network', 'Notification', 'Alarm', 'LowBattery'}


def valid_epoch(value):
    if not isinstance(value, str):
        return False
    parts = value.split(':')
    return len(parts) == 2 and all(part.isdigit() for part in parts) and int(parts[0]) > 0 and int(parts[1]) < 1000000


class TimeVal(C.Structure):
    _fields_ = [('seconds', C.c_long), ('microseconds', C.c_int32)]


def wake_epoch():
    """Kernel's actual wake timestamp; no subprocess or wall-clock timeout."""
    libc = C.CDLL('/usr/lib/libSystem.B.dylib')
    libc.sysctlbyname.argtypes = [C.c_char_p, C.c_void_p, C.POINTER(C.c_size_t), C.c_void_p, C.c_size_t]
    value, length = TimeVal(), C.c_size_t(C.sizeof(TimeVal))
    if libc.sysctlbyname(b'kern.waketime', C.byref(value), C.byref(length), None, 0) != 0 or length.value != C.sizeof(value):
        return None
    epoch = '%d:%d' % (value.seconds, value.microseconds)
    return epoch if valid_epoch(epoch) else None


def interactive(snapshot):
    return (snapshot.get('cpu') is True and snapshot.get('graphics') is True and
            (snapshot.get('userTriggeredFullWake') is True or snapshot.get('wakeType') in INTERACTIVE_WAKE_TYPES))


def policy_state(state, snapshot):
    """Read-only interpretation shared by hooks/status and daemon startup."""
    result = state.copy()
    if 'sleepHold' not in result:
        background = snapshot.get('wakeType') in BACKGROUND_WAKE_TYPES and snapshot.get('userTriggeredFullWake') is not True
        result['sleepHold'] = bool(result.get('held')) or snapshot.get('cpu') is False or background
        result['sleepPhase'] = 'waking' if snapshot.get('cpu') is True else 'sleeping'
    epoch = snapshot.get('wakeEpoch')
    if result.get('sleepPhase') == 'armed':
        baseline = result.get('sleepWakeEpoch')
        if valid_epoch(epoch) and valid_epoch(baseline) and epoch != baseline:
            result['sleepPhase'] = 'waking'
            result.pop('preSleepHold', None)
    else:
        previous = result.get('lastWakeEpoch')
        if valid_epoch(epoch) and valid_epoch(previous) and epoch != previous:
            result.update(sleepHold=True, sleepPhase='waking')
            result.pop('preSleepHold', None)
    if snapshot.get('cpu') is False:
        result.update(sleepHold=True, sleepPhase='sleeping')
    elif result.get('sleepHold') and result.get('sleepPhase') == 'sleeping' and snapshot.get('cpu') is True:
        result['sleepPhase'] = 'waking'
    return result


def allowed(snapshot, state=None):
    state = policy_state({} if state is None else state, snapshot)
    if snapshot.get('cpu') is False:
        return False
    if not state['sleepHold']:
        return True  # Graphics loss alone never means system sleep.
    return state.get('sleepPhase') == 'waking' and interactive(snapshot)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def valid_state(value):
    if not isinstance(value, dict) or not isinstance(value.get('held'), dict):
        return False
    if not isinstance(value.get('generation', 0), int) or isinstance(value.get('generation', 0), bool):
        return False
    if 'sleepHold' in value and type(value['sleepHold']) is not bool:
        return False
    if 'sleepPhase' in value and value['sleepPhase'] not in ('awake', 'armed', 'sleeping', 'waking'):
        return False
    for pid, entry in value['held'].items():
        if not isinstance(pid, str) or not pid.isdigit() or int(pid) <= 1 or not isinstance(entry, dict):
            return False
        if not isinstance(entry.get('start'), str) or not entry['start']:
            return False
        if 'path' in entry and (not isinstance(entry['path'], str) or not entry['path'].startswith('/')):
            return False
    return True


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if valid_state(value) else None
    except (OSError, ValueError, TypeError):
        return None


class Ownership:
    """Mirrored durable identity journal; STOP happens only after both writes."""
    def __init__(self, path, repair=True):
        self.path = Path(path)
        self.backup = self.path.with_name(self.path.name + '.backup')
        copies = [read_json(self.path), read_json(self.backup)]
        good = [copy for copy in copies if copy is not None]
        if not good and any(path.exists() for path in (self.path, self.backup)):
            raise RuntimeError('Both ownership journals invalid; preserve them for manual recovery')
        self.value = max(good, key=lambda copy: copy.get('generation', 0)) if good else {'held': {}, 'generation': 0}
        if repair and good and any(copy != self.value for copy in copies):
            self.save()

    def save(self):
        self.value['generation'] = self.value.get('generation', 0) + 1
        # Write backup first: a torn update never destroys the last good copy.
        save_json(self.backup, self.value)
        save_json(self.path, self.value)


class BSDInfo(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', C.c_char * 16), ('name', C.c_char * 32)]
    _fields_ += [(name, C.c_uint32) for name in ('nfiles', 'pgid', 'jobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', C.c_int32), ('startsec', C.c_uint64), ('startusec', C.c_uint64)]


class NativeProcesses:
    """libproc identity/status scan: no ps subprocess per polling interval."""
    def __init__(self):
        self.lib = C.CDLL('/usr/lib/libproc.dylib')
        self.lib.proc_listallpids.argtypes = [C.c_void_p, C.c_int]
        self.lib.proc_listallpids.restype = C.c_int
        self.lib.proc_pidinfo.argtypes = [C.c_int, C.c_int, C.c_uint64, C.c_void_p, C.c_int]
        self.lib.proc_pidinfo.restype = C.c_int
        self.lib.proc_pidpath.argtypes = [C.c_int, C.c_void_p, C.c_uint]
        self.lib.proc_pidpath.restype = C.c_int

    def path(self, pid):
        buf = C.create_string_buffer(4096)
        return buf.value.decode(errors='replace') if self.lib.proc_pidpath(pid, buf, len(buf)) > 0 else ''

    def get(self, pid):
        info = BSDInfo()
        if self.lib.proc_pidinfo(int(pid), 3, 0, C.byref(info), C.sizeof(info)) != C.sizeof(info):
            return None
        path = self.path(int(pid))
        if info.uid != os.getuid() or not path or info.status == 5:  # SZOMB
            return None
        return {'start': '%d:%d' % (info.startsec, info.startusec), 'path': path,
                'ppid': str(info.ppid), 'stopped': info.status == 4,
                'legacyStart': ' '.join(time.strftime('%a %b %e %H:%M:%S %Y', time.localtime(info.startsec)).split())}

    def scan(self):
        size = max(self.lib.proc_listallpids(None, 0) + 256, 512)
        buf = (C.c_int * size)()
        count = self.lib.proc_listallpids(buf, C.sizeof(buf))
        return {str(pid): proc for pid in list(buf)[:max(count, 0)] if pid > 1 and (proc := self.get(pid))}


_native = None


def native():
    global _native
    if _native is None:
        _native = NativeProcesses()
    return _native


def processes():
    return native().scan()


def process(pid):
    return native().get(pid)


def agent_kind(path):
    """Recognize installed native CLI layouts; never app-bundle executables."""
    if '.app/' in path:
        return None
    if path.startswith(str(HOME / '.local/share/claude/versions') + '/') or (
            '/Cellar/claude-code/' in path and Path(path).name == 'claude'):
        return 'claude'
    roots = (str(HOME / '.bun/install/global/node_modules') + '/',
             str(HOME / '.local/lib/node_modules') + '/',
             '/opt/homebrew/lib/node_modules/', '/usr/local/lib/node_modules/')
    if Path(path).name == 'codex' and (
            ('/Cellar/codex/' in path) or
            (path.startswith(roots) and '/@openai/codex' in path and
             (path.endswith('/bin/codex') or path.endswith('/codex/codex')))):
        return 'codex'
    return None


def safe_descendant(path):
    # CPython's command-line interpreter on Apple/Xcode and Homebrew is an
    # executable inside Python.app. It is a CLI runtime, not a GUI app helper.
    python_runtime = ('/Python.framework/Versions/' in path or '/Python3.framework/Versions/' in path) and path.endswith('/Resources/Python.app/Contents/MacOS/Python')
    developer_tool = '/Contents/Developer/' in path and '.app/' not in path.split('/Contents/Developer/', 1)[1]
    return ('.app/' not in path or python_runtime or developer_tool) and Path(path).name not in ('tmux', 'login', 'launchd')


def descendants(table, roots):
    """Only descendant ancestry proves ownership; shared tmux work is excluded."""
    selected = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, proc in table.items():
            if pid not in selected and proc.get('ppid') in selected and safe_descendant(proc['path']):
                selected.add(pid)
                changed = True
    return selected


def same_identity(entry, proc):
    return bool(proc and entry.get('start') == proc.get('start') and entry.get('path') == proc.get('path'))


class Power:
    """Native read-only power state and acknowledged target-capability events."""
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
        for name, ret, args in (
            ('CFStringCreateWithCString', C.c_void_p, [C.c_void_p, C.c_char_p, C.c_uint]),
            ('CFRelease', None, [C.c_void_p]),
            ('CFRunLoopGetCurrent', C.c_void_p, []),
            ('CFRunLoopAddSource', None, [C.c_void_p, C.c_void_p, C.c_void_p]),
            ('CFRunLoopRun', None, []), ('CFRunLoopStop', None, [C.c_void_p]),
        ):
            fn = getattr(self.cf, name); fn.restype = ret; fn.argtypes = args
        self.domain = self.io.IOServiceGetMatchingService(0, self.io.IOServiceMatching(b'IOPMrootDomain'))
        if not self.domain:
            raise RuntimeError('IOPMrootDomain unavailable')
        self.connection = None
        self.loop = None
        self.backend = "connection"

    def decode(self, value):
        if not value:
            return None
        cf = self.cf
        signatures = {
            'CFGetTypeID': (C.c_ulong, [C.c_void_p]),
            'CFBooleanGetValue': (C.c_bool, [C.c_void_p]),
            'CFNumberGetValue': (C.c_bool, [C.c_void_p, C.c_int, C.c_void_p]),
            'CFStringGetLength': (C.c_long, [C.c_void_p]),
            'CFStringGetCString': (C.c_bool, [C.c_void_p, C.c_void_p, C.c_long, C.c_uint]),
        }
        for name, (ret, args) in signatures.items():
            fn = getattr(cf, name); fn.restype = ret; fn.argtypes = args
        for name in ('CFBooleanGetTypeID', 'CFNumberGetTypeID', 'CFStringGetTypeID'):
            getattr(cf, name).restype = C.c_ulong
        kind = cf.CFGetTypeID(value)
        if kind == cf.CFBooleanGetTypeID():
            return bool(cf.CFBooleanGetValue(value))
        if kind == cf.CFNumberGetTypeID():
            number = C.c_longlong()
            return number.value if cf.CFNumberGetValue(value, 4, C.byref(number)) else None
        if kind == cf.CFStringGetTypeID():
            buf = C.create_string_buffer(cf.CFStringGetLength(value) * 4 + 1)
            return buf.value.decode(errors='replace') if cf.CFStringGetCString(value, buf, len(buf), 0x08000100) else None
        return None

    def property(self, name):
        key = self.cf.CFStringCreateWithCString(None, name.encode(), 0x08000100)
        prop = None
        try:
            prop = self.io.IORegistryEntryCreateCFProperty(self.domain, key, None, 0)
            return self.decode(prop)
        finally:
            if prop:
                self.cf.CFRelease(prop)
            self.cf.CFRelease(key)

    def snapshot(self):
        caps = self.property('System Capabilities')
        return {'cpu': bool(caps & CPU) if type(caps) is int else None,
                'graphics': bool(caps & GRAPHICS) if type(caps) is int else None,
                'wakeType': self.property('Wake Type'),
                'userTriggeredFullWake': self.property('IOPMUserTriggeredFullWake'),
                'wakeEpoch': wake_epoch()}

    def target_event(self, guard, token, caps, acknowledge):
        """Freeze before acknowledging a disallowed target; ack even on errors."""
        try:
            try:
                snapshot = self.snapshot()
            except Exception:
                snapshot = {'wakeEpoch': wake_epoch()}
            snapshot.update(cpu=bool(caps & CPU), graphics=bool(caps & GRAPHICS))
            guard.transition(snapshot)
        except Exception as exc:
            print('sleep guard power callback: ' + type(exc).__name__, file=sys.stderr, flush=True)
        finally:
            acknowledge(token)

    def legacy_event(self, guard, kind, arg, acknowledge):
        try:
            if kind == WILL_NOT_SLEEP:
                guard.cancel_sleep()
            elif kind == HAS_POWERED_ON:
                guard.woke()
            # Legacy WillSleep includes graphics-only full->dark transitions.
            # It is acknowledged, but only target CPU loss can arm the hold.
        except Exception as exc:
            print('sleep guard power callback: ' + type(exc).__name__, file=sys.stderr, flush=True)
        finally:
            if kind in (0xe0000270, WILL_SLEEP):
                acknowledge(arg)

    def stop(self):
        if self.loop:
            self.cf.CFRunLoopStop(self.loop)

    def run(self, guard):
        self.loop = self.cf.CFRunLoopGetCurrent()
        mode = C.c_void_p.in_dll(self.cf, 'kCFRunLoopDefaultMode').value
        callbacks = []
        # Private but exported connection API supplies *target* capabilities,
        # including graphics-only transitions. Public sleep events are fallback.
        try:
            connection = C.c_void_p()
            cbtype = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p, C.c_uint32, C.c_uint32)
            for name, ret, args in (
                ('IOPMConnectionCreate', C.c_int, [C.c_void_p, C.c_uint32, C.POINTER(C.c_void_p)]),
                ('IOPMConnectionSetNotification', C.c_int, [C.c_void_p, C.c_void_p, cbtype]),
                ('IOPMConnectionScheduleWithRunLoop', C.c_int, [C.c_void_p, C.c_void_p, C.c_void_p]),
                ('IOPMConnectionAcknowledgeEvent', C.c_int, [C.c_void_p, C.c_uint32]),
                ('IOPMConnectionRelease', C.c_int, [C.c_void_p]),
                ('IOPMConnectionUnscheduleFromRunLoop', C.c_int, [C.c_void_p, C.c_void_p, C.c_void_p]),
            ):
                fn = getattr(self.io, name); fn.restype = ret; fn.argtypes = args
            name = self.cf.CFStringCreateWithCString(None, b'coding-agent-sleep-guard', 0x08000100)
            try:
                result = self.io.IOPMConnectionCreate(name, 31, C.byref(connection))
            finally:
                self.cf.CFRelease(name)
            if result != 0 or not connection.value:
                raise RuntimeError('IOPMConnectionCreate failed')
            self.connection = connection
            def event(ref, conn, token, caps):
                self.target_event(guard, token, caps, lambda value: self.io.IOPMConnectionAcknowledgeEvent(conn, value))
            cb = cbtype(event); callbacks.append(cb)
            if self.io.IOPMConnectionSetNotification(connection, None, cb) != 0 or self.io.IOPMConnectionScheduleWithRunLoop(connection, self.loop, mode) != 0:
                raise RuntimeError('IOPMConnection listener setup failed')
            self.backend = 'connection'
            print('Target-capability listener active', flush=True)
        except (AttributeError, RuntimeError) as exc:
            if self.connection:
                self.io.IOPMConnectionRelease(self.connection)
                self.connection = None
            raise RuntimeError('CPU-target observer unavailable; refusing ambiguous legacy sleep policy') from exc
        # Public cancellation/wake notifications supplement CPU target events.
        legacytype = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_void_p)
        port = C.c_void_p(); notifier = C.c_uint(); root_connection = C.c_uint(0)
        def legacy(ref, service, kind, arg):
            self.legacy_event(guard, kind, int(arg or 0), lambda value: self.io.IOAllowPowerChange(root_connection, value))
        legacycb = legacytype(legacy); callbacks.append(legacycb)
        self.io.IORegisterForSystemPower.restype = C.c_uint
        self.io.IORegisterForSystemPower.argtypes = [C.c_void_p, C.POINTER(C.c_void_p), legacytype, C.POINTER(C.c_uint)]
        root_connection.value = self.io.IORegisterForSystemPower(None, C.byref(port), legacycb, C.byref(notifier))
        if not root_connection.value:
            raise RuntimeError('Cannot register system sleep listener')
        self.cf.CFRunLoopAddSource(self.loop, self.io.IONotificationPortGetRunLoopSource(port), mode)
        # General-interest notification reconciles lid/override changes promptly.
        interesttype = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_void_p)
        interest_object = C.c_uint()
        def interest(ref, service, kind, arg):
            try:
                guard.tick()
            except Exception as exc:
                print('sleep guard root notification: ' + type(exc).__name__, file=sys.stderr, flush=True)
        interestcb = interesttype(interest); callbacks.append(interestcb)
        self.io.IOServiceAddInterestNotification.restype = C.c_int
        self.io.IOServiceAddInterestNotification.argtypes = [C.c_void_p, C.c_uint, C.c_char_p, interesttype, C.c_void_p, C.POINTER(C.c_uint)]
        self.io.IOServiceAddInterestNotification(port, self.domain, b'IOGeneralInterest', interestcb, None, C.byref(interest_object))
        timertype = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p)
        def on_timer(timer, info):
            try:
                guard.tick()
            except Exception as exc:
                print('sleep guard tick: ' + type(exc).__name__, file=sys.stderr, flush=True)
        timercb = timertype(on_timer); callbacks.append(timercb)
        self.cf.CFAbsoluteTimeGetCurrent.restype = C.c_double
        self.cf.CFRunLoopTimerCreate.restype = C.c_void_p
        self.cf.CFRunLoopTimerCreate.argtypes = [C.c_void_p, C.c_double, C.c_double, C.c_ulong, C.c_long, timertype, C.c_void_p]
        self.cf.CFRunLoopAddTimer.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p]
        timer = self.cf.CFRunLoopTimerCreate(None, self.cf.CFAbsoluteTimeGetCurrent(), 5, 0, 0, timercb, None)
        self.cf.CFRunLoopAddTimer(self.loop, timer, mode)
        try:
            guard.tick()
            if getattr(guard, 'shutting_down', False):
                return
            ready = STATE.parent / 'ready.json'
            save_json(ready, {'pid': os.getpid(), 'runtime': str(Path(__file__).resolve()), 'backend': self.backend})
            self.cf.CFRunLoopRun()
        finally:
            ready = STATE.parent / 'ready.json'
            try:
                if json.loads(ready.read_text()).get('pid') == os.getpid():
                    ready.unlink()
            except (OSError, ValueError, TypeError):
                pass
            self.cf.CFRunLoopRemoveTimer.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p]
            self.cf.CFRunLoopRemoveTimer(self.loop, timer, mode)
            self.cf.CFRunLoopTimerInvalidate.argtypes = [C.c_void_p]
            self.cf.CFRunLoopTimerInvalidate(timer)
            self.cf.CFRelease(timer)
            if interest_object.value:
                self.io.IOObjectRelease(interest_object)
            if notifier.value:
                self.io.IODeregisterForSystemPower.argtypes = [C.POINTER(C.c_uint)]
                self.io.IODeregisterForSystemPower(C.byref(notifier))
            if self.connection:
                self.io.IOPMConnectionUnscheduleFromRunLoop(self.connection, self.loop, mode)
                self.io.IOPMConnectionRelease(self.connection)
            self.io.IOServiceClose.argtypes = [C.c_uint]
            self.io.IOServiceClose(root_connection)
            self.cf.CFRunLoopRemoveSource.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p]
            self.cf.CFRunLoopRemoveSource(self.loop, self.io.IONotificationPortGetRunLoopSource(port), mode)
            self.io.IONotificationPortDestroy.argtypes = [C.c_void_p]
            self.io.IONotificationPortDestroy(port)
            self.io.IOObjectRelease(self.domain)


class Guard:
    def __init__(self, power, state_path=None, scanner=processes, lookup=process, kill=os.kill):
        self.power = power
        self.journal = Ownership(STATE if state_path is None else state_path)
        self.state = self.journal.value
        self.scanner, self.lookup, self.kill = scanner, lookup, kill
        self.shutting_down = False

    def transition(self, snapshot):
        try:
            current = self.power.snapshot()
        except Exception:
            current = {**snapshot, 'cpu': None, 'graphics': None}
        if snapshot.get('cpu') is False:
            if self.state.get('sleepPhase') != 'armed':
                self.state['preSleepHold'] = policy_state(self.state, current)['sleepHold']
            self.state.update(sleepHold=True, sleepPhase='armed', targetCPU=False, sleepWakeEpoch=current.get('wakeEpoch'))
            self.journal.save()  # Durable before stopping work and acknowledging.
        elif snapshot.get('cpu') is True:
            if self.state.get('targetCPU') is False and self.state.get('sleepHold'):
                self._finish_attempt(current)
            self.state['targetCPU'] = True
            self.journal.save()
        self.tick(snapshot=current)

    def _finish_attempt(self, snapshot):
        epoch, baseline = snapshot.get('wakeEpoch'), self.state.get('sleepWakeEpoch')
        # Native CPU gain without a physical wake is sleep cancellation. It
        # must restore continuous headless work, including dark clients that
        # do not receive the public WillNotSleep notification.
        canceled = valid_epoch(epoch) and valid_epoch(baseline) and epoch == baseline
        prior = self.state.pop('preSleepHold', self.state.get('sleepHold', False))
        hold = prior if canceled else self.state.get('sleepHold', False)
        self.state.update(sleepHold=hold, sleepPhase='waking' if hold else 'awake')

    def cancel_sleep(self):
        if 'preSleepHold' in self.state:
            hold = self.state.pop('preSleepHold')
            self.state.update(sleepHold=hold, sleepPhase='waking' if hold else 'awake', targetCPU=True)
            self.journal.save()
        self.tick()

    def woke(self):
        if self.state.get('sleepHold'):
            self._finish_attempt(self.power.snapshot())
            self.journal.save()
        self.tick()

    def policy(self, snapshot):
        return allowed(snapshot, self.state)

    def _reconcile_phase(self, snapshot):
        new = policy_state(self.state, snapshot)
        epoch = snapshot.get('wakeEpoch')
        if valid_epoch(epoch):
            new['lastWakeEpoch'] = epoch
        if snapshot.get('cpu') is False:
            new.update(sleepHold=True, sleepPhase='sleeping')
        if allowed(snapshot, new) and new.get('sleepHold'):
            new.update(sleepHold=False, sleepPhase='awake')
            new.pop('preSleepHold', None)
        if new != self.state:
            self.state.clear()
            self.state.update(new)
            self.journal.save()

    def _migrate(self, table):
        changed = False
        for pid, entry in list(self.state['held'].items()):
            if 'path' not in entry:
                proc = table.get(pid)
                if proc and agent_kind(proc['path']) and entry['start'] == proc.get('legacyStart'):
                    entry.update(start=proc['start'], path=proc['path'])
                else:
                    del self.state['held'][pid]
                changed = True
        if changed:
            self.journal.save()

    def _pause(self, pid, expected):
        if self.shutting_down or int(pid) == os.getpid():
            return
        fresh = self.lookup(pid)
        if not same_identity(expected, fresh):
            return
        entry = self.state['held'].get(pid)
        if not entry and fresh['stopped']:
            return  # Never claim or thaw a preexisting/manual stop.
        if entry and fresh['stopped']:
            return
        if not entry:
            self.state['held'][pid] = {'start': fresh['start'], 'path': fresh['path']}
        # Also retry durability for an in-memory entry left by a failed write.
        self.journal.save()  # Must succeed before every STOP signal.
        try:
            check = self.lookup(pid)
            if not self.shutting_down and same_identity(self.state['held'].get(pid, {}), check):
                self.kill(int(pid), signal.SIGSTOP)
        except ProcessLookupError:
            pass

    def _release(self):
        changed = False
        for pid, entry in list(self.state['held'].items()):
            fresh = self.lookup(pid)
            if fresh is None:
                try:
                    os.kill(int(pid), 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    continue
                else:
                    continue  # Live but temporarily inaccessible: keep ownership.
            if same_identity(entry, fresh):
                try:
                    # Running entries need no SIGCONT (e.g. crash before STOP).
                    if fresh['stopped']:
                        self.kill(int(pid), signal.SIGCONT)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    continue  # Preserve ownership for explicit recovery.
            del self.state['held'][pid]
            changed = True
        if changed:
            self.journal.save()

    def release(self):
        table = self.scanner()
        self._migrate(table)
        self._release()
        # Explicit recovery also resolves a pending attempt whose cancel event
        # was lost while the daemon was down. Capture the current OS wake epoch
        # so a stale wake label cannot immediately recreate that cleared hold.
        self.state.update(sleepHold=False, sleepPhase='awake', targetCPU=True)
        self.state.pop('preSleepHold', None)
        epoch = wake_epoch() if self.power is None else self.power.snapshot().get('wakeEpoch')
        if valid_epoch(epoch):
            self.state['lastWakeEpoch'] = epoch
        self.journal.save()

    def terminate(self):
        self.shutting_down = True
        if self.policy(self.power.snapshot()):
            self.release()
        else:
            self.journal.save()  # Restart must recover these owned stops.

    def tick(self, snapshot=None):
        if self.shutting_down:
            return
        snapshot = self.power.snapshot() if snapshot is None else snapshot
        self._reconcile_phase(snapshot)
        permit = self.policy(snapshot)
        table = self.scanner()
        self._migrate(table)
        if permit:
            self._release()
        else:
            stale = [pid for pid, entry in self.state['held'].items() if pid in table and not same_identity(entry, table[pid])]
            for pid in stale:
                del self.state['held'][pid]
            if stale:
                self.journal.save()
            roots = {pid for pid, proc in table.items() if agent_kind(proc['path'])}
            roots |= {pid for pid, entry in self.state['held'].items() if same_identity(entry, table.get(pid))}
            # Freeze CLI roots and surviving owned families first, then rescan.
            for pid in sorted(roots):
                self._pause(pid, table[pid])
            # A child can fork before its own STOP arrives. Reconcile until
            # the proven family is stopped; frozen ancestors make it finite.
            for attempt in range(8):
                table = self.scanner()
                anchors = {pid for pid, entry in self.state['held'].items() if same_identity(entry, table.get(pid))}
                anchors |= {pid for pid, proc in table.items() if agent_kind(proc['path'])}
                selected = descendants(table, anchors)
                running = []
                for pid in sorted(selected):
                    proc = table[pid]
                    entry = self.state['held'].get(pid)
                    if entry and not same_identity(entry, proc):
                        del self.state['held'][pid]
                        self.journal.save()
                    if not proc['stopped'] and int(pid) != os.getpid():
                        running.append(pid)
                        self._pause(pid, proc)
                if not running:
                    break
            else:
                raise RuntimeError('Owned process tree did not settle; retrying next reconciliation')
        reason = ('awake-without-system-sleep' if permit else
                  'system-sleep-pending' if self.state.get('sleepPhase') == 'armed' else
                  'cpu-unavailable' if snapshot.get('cpu') is not True else
                  'waiting-for-interactive-wake')
        if self.state.get('allowed') != permit or self.state.get('reason') != reason:
            self.state['allowed'] = permit
            self.state['reason'] = reason
            self.journal.save()
            print('Coding work policy: %s; owned paused processes: %d' % (reason, len(self.state['held'])), flush=True)


def hook():
    try:
        state = Ownership(STATE, repair=False).value
        try:
            snapshot = Power().snapshot()
        except Exception:
            snapshot = {'cpu': None, 'wakeEpoch': wake_epoch()}
        permit = allowed(snapshot, state)
    except Exception:
        permit = False  # Unreadable ownership cannot safely discard a hold.
    if not permit:
        print(json.dumps({'continue': False,
                          'stopReason': 'Coding work is held after system sleep until an interactive wake.'}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['daemon', 'hook', 'status', 'release', 'recover'])
    args = parser.parse_args()
    if args.command == 'hook':
        hook()
        return
    if args.command == 'status':
        snapshot = Power().snapshot()
        table = processes()
        state = policy_state(Ownership(STATE, repair=False).value, snapshot)
        print(json.dumps({'state': state, 'power': snapshot, 'allowed': allowed(snapshot, state),
                          'agentProcesses': {pid: agent_kind(proc['path']) for pid, proc in table.items() if agent_kind(proc['path'])},
                          'limitation': 'Shared tmux tools outside the CLI descendant tree are not suspended.'}, indent=2))
        return
    STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE.parent / 'daemon.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        power = None if args.command in ('release', 'recover') else Power()
        controller = Guard(power)
        if args.command in ('release', 'recover'):
            controller.release()  # Explicit uninstall/recovery intentionally overrides policy.
            return
        def shutdown(signum, frame):
            try:
                controller.terminate()
            finally:
                power.stop()
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        print('Coding-agent sleep guard starting', flush=True)
        power.run(controller)


if __name__ == '__main__':
    main()
