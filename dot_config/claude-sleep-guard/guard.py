"""Hold local Claude/Codex CLI work when the Mac is not intentionally awake.

Lid closed requires a live, verified Awake hold and the kernel clamshell
sleep override. Lid open requires CPU and graphics capability. No GUI control,
settings changes, arguments, credentials, network requests or quota checks.
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


def allowed(snapshot):
    """Shared daemon/hook policy. Unknown native state fails closed."""
    if snapshot.get('cpu') is not True:
        return False
    if snapshot.get('lidClosed') is True:
        return snapshot.get('liveAwakeHold') is True and snapshot.get('clamshellOverride') is True
    return snapshot.get('lidClosed') is False and snapshot.get('graphics') is True


def awake_lid_preferences(enabled, floor):
    # Mirror Awake defaults; malformed enable values fail closed. Floor values
    # outside its settings choices use Awake's own default of 20 percent.
    enabled = True if enabled is None else enabled is True or type(enabled) is int and enabled == 1
    floor = floor if type(floor) is int and floor in (10, 20, 30, 50) else 20
    return enabled, floor


def headless_eligible(enabled, floor, status):
    if enabled is not True:
        return False
    if status.get('onAC') is True:
        return True
    percent = status.get('batteryPercent')
    return status.get('onAC') is False and type(percent) is int and 0 <= percent <= 100 and percent > floor


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


def qualified_awake(assertions, path_for_pid):
    if not isinstance(assertions, dict):
        return False
    for pid, entries in assertions.items():
        try:
            owner = path_for_pid(int(pid))
        except (ValueError, TypeError, OSError):
            continue
        if not owner.endswith('/Awake.app/Contents/MacOS/Awake') or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get('AssertName', '')
            level = entry.get('AssertLevel', 0)
            kind = entry.get('AssertType')
            if (isinstance(name, str) and
                (name == 'com.mackhaymond.Awake: Manual hold' or name.startswith('com.mackhaymond.Awake: Timed hold (')) and
                isinstance(level, (int, float)) and level > 0 and
                kind in ('PreventUserIdleSystemSleep', 'PreventSystemSleep', 'NoIdleSleepAssertion', 'NoSystemSleepAssertion', 'PreventUserIdleDisplaySleep', 'NoDisplaySleepAssertion')):
                return True
    return False


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
            'IOPMCopyAssertionsByProcess': (C.c_int, [C.POINTER(C.c_void_p)]),
            'IOPSCopyPowerSourcesInfo': (C.c_void_p, []),
            'IOPSGetProvidingPowerSourceType': (C.c_void_p, [C.c_void_p]),
            'IOPSCopyPowerSourcesList': (C.c_void_p, [C.c_void_p]),
            'IOPSGetPowerSourceDescription': (C.c_void_p, [C.c_void_p, C.c_void_p]),
        }
        for name, (ret, args) in signatures.items():
            fn = getattr(self.io, name); fn.restype = ret; fn.argtypes = args
        for name, ret, args in (
            ('CFStringCreateWithCString', C.c_void_p, [C.c_void_p, C.c_char_p, C.c_uint]),
            ('CFPreferencesCopyAppValue', C.c_void_p, [C.c_void_p, C.c_void_p]),
            ('CFPreferencesAppSynchronize', C.c_bool, [C.c_void_p]),
            ('CFArrayGetCount', C.c_long, [C.c_void_p]),
            ('CFArrayGetValueAtIndex', C.c_void_p, [C.c_void_p, C.c_long]),
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
        self.backend = "legacy"

    def decode(self, value):
        # Assertion dictionaries have CFNumber PID keys, so they cannot be
        # serialized as a property list (plist dictionary keys must be strings).
        if not value:
            return None
        cf = self.cf
        signatures = {
            'CFGetTypeID': (C.c_ulong, [C.c_void_p]),
            'CFBooleanGetValue': (C.c_bool, [C.c_void_p]),
            'CFNumberGetValue': (C.c_bool, [C.c_void_p, C.c_int, C.c_void_p]),
            'CFStringGetLength': (C.c_long, [C.c_void_p]),
            'CFStringGetCString': (C.c_bool, [C.c_void_p, C.c_void_p, C.c_long, C.c_uint]),
            'CFArrayGetCount': (C.c_long, [C.c_void_p]),
            'CFArrayGetValueAtIndex': (C.c_void_p, [C.c_void_p, C.c_long]),
            'CFDictionaryGetCount': (C.c_long, [C.c_void_p]),
            'CFDictionaryGetKeysAndValues': (None, [C.c_void_p, C.c_void_p, C.c_void_p]),
        }
        for name, (ret, args) in signatures.items():
            fn = getattr(cf, name); fn.restype = ret; fn.argtypes = args
        for name in ('CFBooleanGetTypeID', 'CFNumberGetTypeID', 'CFStringGetTypeID', 'CFArrayGetTypeID', 'CFDictionaryGetTypeID'):
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
        if kind == cf.CFArrayGetTypeID():
            return [self.decode(cf.CFArrayGetValueAtIndex(value, index)) for index in range(cf.CFArrayGetCount(value))]
        if kind == cf.CFDictionaryGetTypeID():
            count = cf.CFDictionaryGetCount(value)
            keys, values = (C.c_void_p * count)(), (C.c_void_p * count)()
            cf.CFDictionaryGetKeysAndValues(value, keys, values)
            return {self.decode(keys[index]): self.decode(values[index]) for index in range(count)}
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

    def awake_preferences(self):
        app = self.cf.CFStringCreateWithCString(None, b'com.mackhaymond.Awake', 0x08000100)
        values = []
        try:
            # Refresh this process's cached preferences after user changes.
            if not self.cf.CFPreferencesAppSynchronize(app):
                return False, 20
            for name in ('awake.lidClosedStaysAwake', 'awake.lidClosedBatteryFloor'):
                key = self.cf.CFStringCreateWithCString(None, name.encode(), 0x08000100)
                value = None
                try:
                    value = self.cf.CFPreferencesCopyAppValue(key, app)
                    values.append(self.decode(value))
                finally:
                    if value:
                        self.cf.CFRelease(value)
                    self.cf.CFRelease(key)
        finally:
            self.cf.CFRelease(app)
        return awake_lid_preferences(*values)

    def power_status(self):
        info = self.io.IOPSCopyPowerSourcesInfo()
        status = {'onAC': None, 'batteryPercent': None}
        if not info:
            return status
        sources = None
        try:
            providing = self.decode(self.io.IOPSGetProvidingPowerSourceType(info))
            if providing == 'AC Power':
                status['onAC'] = True
            elif providing == 'Battery Power':
                status['onAC'] = False
            sources = self.io.IOPSCopyPowerSourcesList(info)
            if sources:
                for index in range(self.cf.CFArrayGetCount(sources)):
                    source = self.cf.CFArrayGetValueAtIndex(sources, index)
                    desc = self.decode(self.io.IOPSGetPowerSourceDescription(info, source))
                    if not isinstance(desc, dict) or desc.get('Type') != 'InternalBattery':
                        continue
                    current, maximum = desc.get('Current Capacity'), desc.get('Max Capacity')
                    if type(current) is int and type(maximum) is int and maximum > 0 and 0 <= current <= maximum:
                        status['batteryPercent'] = current * 100 // maximum
        finally:
            if sources:
                self.cf.CFRelease(sources)
            self.cf.CFRelease(info)
        return status

    def snapshot(self):
        caps = self.property('System Capabilities')
        assertions = C.c_void_p()
        own_hold = False
        try:
            if self.io.IOPMCopyAssertionsByProcess(C.byref(assertions)) == 0 and assertions.value:
                own_hold = qualified_awake(self.decode(assertions), native().path)
        finally:
            if assertions.value:
                self.cf.CFRelease(assertions)
        enabled, floor = self.awake_preferences()
        status = self.power_status()
        eligible = headless_eligible(enabled, floor, status)
        published_override = self.property('AppleClamshellCausesSleep') is False
        return {'cpu': isinstance(caps, int) and bool(caps & CPU),
                'graphics': isinstance(caps, int) and bool(caps & GRAPHICS),
                'lidClosed': self.property('AppleClamshellState'),
                'clamshellOverride': published_override and eligible,
                'clamshellPublishedOverride': published_override,
                'headlessEligible': eligible, 'lidClosedEnabled': enabled,
                'batteryFloor': floor, **status,
                'liveAwakeHold': own_hold}

    def full_wake(self):
        return allowed(self.snapshot())

    def target_event(self, guard, token, caps, acknowledge):
        """Freeze before acknowledging a disallowed target; ack even on errors."""
        try:
            snapshot = self.snapshot()
            snapshot.update(cpu=bool(caps & CPU), graphics=bool(caps & GRAPHICS))
            guard.transition(snapshot)
        except Exception as exc:
            print('sleep guard power callback: ' + type(exc).__name__, file=sys.stderr, flush=True)
        finally:
            acknowledge(token)

    def legacy_event(self, guard, kind, arg, acknowledge):
        try:
            if kind == WILL_SLEEP and getattr(self, 'backend', 'legacy') != 'connection':
                snapshot = self.snapshot()
                snapshot.update(cpu=False, graphics=False)
                guard.transition(snapshot)
            elif kind in (HAS_POWERED_ON, WILL_NOT_SLEEP):
                guard.clear_transition()
                guard.tick()
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
            print('Using public sleep listener fallback: ' + type(exc).__name__, flush=True)
        # Always keep legacy sleep/cancel notifications; no sticky WillSleep flag.
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
    def __init__(self, power, state_path=None, scanner=processes, lookup=process, kill=os.kill, clock=time.monotonic):
        self.power = power
        self.journal = Ownership(STATE if state_path is None else state_path)
        self.state = self.journal.value
        self.scanner, self.lookup, self.kill, self.clock = scanner, lookup, kill, clock
        self.target = None
        self.target_until = 0
        self.shutting_down = False

    def clear_transition(self):
        self.target = None

    def transition(self, snapshot):
        self.target = snapshot
        self.target_until = self.clock() + 10
        self.tick()

    def policy(self, snapshot):
        # Target caps are only a short transition fence. Canceled/missed events
        # cannot leave a permanent sleep_pending lockout.
        if self.target is not None:
            if self.clock() >= self.target_until or (
                    snapshot.get('cpu') == self.target.get('cpu') and snapshot.get('graphics') == self.target.get('graphics')):
                self.target = None
            elif not allowed(self.target):
                return False
        return allowed(snapshot)

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

    def terminate(self):
        self.shutting_down = True
        if self.policy(self.power.snapshot()):
            self.release()
        else:
            self.journal.save()  # Restart must recover these owned stops.

    def tick(self, sleeping=False, snapshot=None):
        if self.shutting_down:
            return
        snapshot = self.power.snapshot() if snapshot is None else snapshot
        permit = not sleeping and self.policy(snapshot)
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
        reason = ('allowed' if permit else 'system-sleep' if sleeping else
                  'cpu-unavailable' if not snapshot.get('cpu') else
                  'closed-lid-without-live-Awake-override' if snapshot.get('lidClosed') is True else
                  'graphics-or-lid-state-unavailable')
        if self.state.get('allowed') != permit or self.state.get('reason') != reason:
            self.state['allowed'] = permit
            self.state['reason'] = reason
            self.journal.save()
            print('Coding work policy: %s; owned paused processes: %d' % (reason, len(self.state['held'])), flush=True)


def hook():
    try:
        permit = allowed(Power().snapshot())
    except Exception:
        permit = False
    if not permit:
        print(json.dumps({'continue': False,
                          'stopReason': 'Coding work is paused until a full wake with the lid open, or an active Awake hold with the closed-lid override.'}))


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
        print(json.dumps({'state': Ownership(STATE, repair=False).value, 'power': snapshot, 'allowed': allowed(snapshot),
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
